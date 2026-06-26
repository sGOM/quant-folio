"""전략 ON/OFF 제어 — web 은 상태만 바꾸고 Redis 로 엔진에 명령을 전달한다.

실제 매매는 분리된 엔진 프로세스가 수행한다(웹 핸들러에 매매 로직 금지).
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.strategies import _get_owned
from app.core.channels import ENGINE_CONTROL_CHANNEL, ENGINE_HEARTBEAT_KEY
from app.core.database import get_db
from app.core.redis import redis_client
from app.models import StrategyStatus, User

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
    if not current.kis_app_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "KIS 자격증명을 먼저 등록하세요.")
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
    """엔진 생존 여부(heartbeat)."""
    alive = await redis_client.get(ENGINE_HEARTBEAT_KEY)
    return {"engine_alive": alive == "alive"}
