"""전략 ON/OFF 제어 — web 은 상태만 바꾸고 Redis 로 엔진에 명령을 전달한다.

실제 매매는 분리된 엔진 프로세스가 수행한다(웹 핸들러에 매매 로직 금지).
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.strategies import _get_owned
from app.core.channels import (
    BACKUP_LAST_SUCCESS_KEY,
    ENGINE_CONTROL_CHANNEL,
    ENGINE_HEARTBEAT_KEY,
    FAILURE_ALERT_THRESHOLD,
    engine_health_key,
    mdd_state_key,
)
from app.core.database import get_db
from app.core.redis import redis_client
from app.models import Strategy, StrategyStatus, User
from app.services.broker import user_has_credentials

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/engine", tags=["engine"])


@router.post("/strategies/{strategy_id}/start")
async def start_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략을 live 로 전환하고 엔진에 start 명령을 발행한다.

    KIS 자격증명이 없으면 400. 실제 매매는 분리된 엔진 프로세스가 수행한다.
    """
    s = await _get_owned(db, current, strategy_id)
    # 엔진(_load → user_has_credentials)과 동일한 판정을 재사용한다:
    # DB 등록값 우선, 없으면 .env 폴백 — 라우트만 DB 컬럼을 직접 보면 드리프트가 생긴다.
    if not user_has_credentials(current):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "증권사 자격증명을 먼저 등록하세요.")
    s.status = StrategyStatus.LIVE
    await db.commit()
    await redis_client.publish(
        ENGINE_CONTROL_CHANNEL,
        json.dumps({"action": "start", "strategy_id": strategy_id}),
    )
    logger.info("전략 %d start 명령 발행", strategy_id)
    return {"status": "live"}


@router.post("/strategies/{strategy_id}/stop")
async def stop_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략 자동매매를 중지하고(status=backtested) 엔진에 stop 명령을 발행한다."""
    s = await _get_owned(db, current, strategy_id)
    s.status = StrategyStatus.BACKTESTED
    await db.commit()
    await redis_client.publish(
        ENGINE_CONTROL_CHANNEL,
        json.dumps({"action": "stop", "strategy_id": strategy_id}),
    )
    logger.info("전략 %d stop 명령 발행", strategy_id)
    return {"status": "stopped"}


@router.get("/status")
async def engine_status(_: User = Depends(get_current_user)):
    """엔진 생존 여부(heartbeat)·마지막 DB 백업 성공 시각(§9)."""
    alive = await redis_client.get(ENGINE_HEARTBEAT_KEY)
    last_backup = await redis_client.get(BACKUP_LAST_SUCCESS_KEY)
    return {"engine_alive": alive == "alive", "backup_last_success_at": last_backup}


@router.get("/strategies/health")
async def strategies_health(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """로그인 사용자의 운용 중(live) 전략별 러너 헬스 상태.

    프로세스 전역 heartbeat(/status)와 달리 개별 전략 러너 단위로 마지막 성공 틱 시각·
    연속 실패 횟수를 노출한다(엔진이 engine:health:{id} 에 갱신). 러너가 조용히 실패해도
    heartbeat 는 alive 이므로, 이 엔드포인트로 러너별 이상을 드러낸다.

    MDD 킬스위치 상태(고점 HWM·발동 여부·발동일, engine.rebalance_runner 가
    rebalance:mdd:{id} 에 갱신)도 함께 노출한다 — 이전엔 발동 순간의 alert
    토스트/알림함 메시지 외엔 "지금 청산 상태로 재가동 대기 중"임을 나중에
    확인할 방법이 없었다.

    :return: {"threshold": int, "strategies": [{strategy_id, name, status,
        last_success_ts, consecutive_failures, last_error, updated_at, healthy,
        mdd_killed, mdd_kill_date, mdd_hwm}]}.
        healthy 는 연속 실패가 임계 미만인지 여부(임계 이상이면 알림도 발행된 상태).
    """
    rows = await db.scalars(
        select(Strategy)
        .where(
            Strategy.user_id == current.id,
            Strategy.status == StrategyStatus.LIVE,
        )
        .order_by(Strategy.id.desc())
    )
    strategies = list(rows)

    out = []
    for s in strategies:
        raw = await redis_client.get(engine_health_key(s.id))
        health: dict = {}
        if raw:
            try:
                health = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                health = {}
        failures = int(health.get("consecutive_failures", 0) or 0)

        mdd_raw = await redis_client.get(mdd_state_key(s.id))
        mdd: dict = {}
        if mdd_raw:
            try:
                mdd = json.loads(mdd_raw)
            except (json.JSONDecodeError, TypeError):
                mdd = {}

        out.append({
            "strategy_id": s.id,
            "name": s.name,
            "status": str(s.status),
            "last_success_ts": health.get("last_success_ts"),
            "consecutive_failures": failures,
            "last_error": health.get("last_error"),
            "updated_at": health.get("updated_at"),
            # 헬스 레코드가 아직 없으면(막 start) reported=False 로 구분 가능.
            "reported": bool(health),
            "healthy": failures < FAILURE_ALERT_THRESHOLD,
            "mdd_killed": bool(mdd.get("killed", False)),
            "mdd_kill_date": mdd.get("kill_date"),
            "mdd_hwm": mdd.get("hwm"),
        })
    return {"threshold": FAILURE_ALERT_THRESHOLD, "strategies": out}
