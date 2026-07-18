"""엔진 이상 알림 발행 — 앱 내(WS) 경보 채널 + 텔레그램(critical) + DB 영속화로 이벤트를 publish.

무인 자동매매의 "조용한 실패"(러너 연속 실패, PIT 유니버스 폴백, MDD 킬스위치 발동,
팩터 조회 전면 장애 등)를 사용자에게 즉시 알린다. 기존 엔진 이벤트 채널
(engine:events:{user_id})에 `"alert"` 타입으로 실어 web 이 WS 로 프론트에 중계하고
(fills.publish_event 재사용), critical 심각도는 앱 미접속 중에도 놓치지 않도록 텔레그램
봇으로 추가 발송한다(TELEGRAM_BOT_TOKEN/CHAT_ID 미설정 시 비활성 — 기존 동작 그대로).

WS·텔레그램은 순간 전송이라 미접속 중(WS)이거나 warning(텔레그램)이면 유실되므로,
모든 알림을 `alerts` 테이블에도 적재한다(docs/improvements.md §17) — GET /api/alerts 로
나중에 조회 가능. 영속화 실패는 로그만 남기고 기존 WS·텔레그램 흐름을 막지 않는다.
"""
from __future__ import annotations

import logging

import httpx
from redis.asyncio import Redis

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import Alert
from app.services.market import now_kst
from engine.fills import publish_event

logger = logging.getLogger("engine.alerts")

_TELEGRAM_TIMEOUT_SECONDS = 5.0


async def _send_telegram(text: str) -> None:
    """텔레그램 봇으로 알림 메시지를 발송한다. 실패해도 앱 내 알림 흐름을 막지 않는다."""
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=_TELEGRAM_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url, json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text}
            )
            resp.raise_for_status()
    except Exception:  # noqa: BLE001 — 외부 발송 실패는 로그만, 알림 자체는 계속돼야 한다.
        logger.warning("텔레그램 알림 발송 실패", exc_info=True)


async def publish_alert(
    redis: Redis,
    *,
    user_id: int | None,
    strategy_id: int,
    severity: str,
    message: str,
    code: str | None = None,
    dedup_window_hours: float | None = None,
) -> None:
    """엔진 이상 알림을 사용자별 이벤트 채널로 발행한다.

    :param severity: "warning" | "critical".
    :param message: 사람이 읽을 한국어 문장(프론트 토스트/배너에 그대로 노출 가능).
    :param code: 알림 종류 식별자(프론트 dedup·필터용, 예 "runner_failures").
    :param dedup_window_hours: 지정 시(§21), 같은 (user_id, strategy_id, code)의
        미확인(is_read=False) 알림이 이 시간(시간 단위) 안에 이미 있으면 DB 적재·
        텔레그램 재발송을 건너뛴다. `db_backup_stale`처럼 원인이 해소될 때까지 매
        beat 주기마다 재발행되는 상태형 배치 경보의 테이블 증식·알림 피로를 막기
        위함 — WS 토스트는 dedup 여부와 무관하게 항상 통과시켜 실시간성은 유지한다.
        None(기본)이면 기존 동작(매번 적재+발송)과 동일.
    """
    logger.warning(
        "엔진 알림[%s] 전략 %s%s: %s",
        severity, strategy_id, f"({code})" if code else "", message,
    )
    is_dup = False
    try:
        async with AsyncSessionLocal() as db:
            if dedup_window_hours is not None and code is not None:
                from datetime import timedelta

                from sqlalchemy import select

                cutoff = now_kst() - timedelta(hours=dedup_window_hours)
                existing = await db.execute(
                    select(Alert.id).where(
                        Alert.user_id == user_id,
                        Alert.strategy_id == strategy_id,
                        Alert.code == code,
                        Alert.is_read.is_(False),
                        Alert.created_at >= cutoff,
                    ).limit(1)
                )
                is_dup = existing.scalar_one_or_none() is not None
            if not is_dup:
                db.add(
                    Alert(
                        user_id=user_id,
                        strategy_id=strategy_id,
                        severity=severity,
                        code=code,
                        message=message,
                    )
                )
                await db.commit()
    except Exception:  # noqa: BLE001 — 영속화 실패가 WS·텔레그램 알림을 막으면 안 된다.
        logger.warning("알림 DB 영속화 실패", exc_info=True)
    if is_dup:
        # 창 안에 미확인 알림이 이미 있음 — DB 재적재·텔레그램 재발송 생략(WS는 계속 발송).
        logger.info("알림[%s] dedup 창 내 미확인 알림 존재 — DB/텔레그램 생략", code)
    elif severity == "critical" and settings.has_telegram:
        # 심각도 필터: critical 만 외부 발송해 소음을 막는다. WS 채널 유무(user_id)와
        # 무관하게 발송 — 앱 미접속 상황을 위한 채널이므로 여기서 걸러지면 의미가 없다.
        await _send_telegram(
            f"[QuantFolio] 전략 {strategy_id}{f'({code})' if code else ''}\n{message}"
        )
    if user_id is None:
        # 사용자를 특정할 수 없으면(적재 전) WS 전송 대상 채널이 없다 — 로그만 남긴다.
        return
    payload = {
        "type": "alert",
        "user_id": int(user_id),
        "strategy_id": int(strategy_id),
        "severity": severity,
        "message": message,
        "ts": now_kst().isoformat(),
    }
    if code:
        payload["code"] = code
    await publish_event(redis, payload)
