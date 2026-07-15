"""엔진 이상 알림 발행 — 앱 내(WS) 경보 채널로 warning/critical 이벤트를 publish.

무인 자동매매의 "조용한 실패"(러너 연속 실패, PIT 유니버스 폴백, MDD 킬스위치 발동,
팩터 조회 전면 장애 등)를 사용자에게 즉시 알린다. 이메일/푸시 등 외부 발송 채널은
자격증명 부재로 이번 범위에서 제외하고, 기존 엔진 이벤트 채널(engine:events:{user_id})에
`"alert"` 타입으로 실어 web 이 WS 로 그대로 프론트에 중계하게 한다(fills.publish_event 재사용).
"""
from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.services.market import now_kst
from engine.fills import publish_event

logger = logging.getLogger("engine.alerts")


async def publish_alert(
    redis: Redis,
    *,
    user_id: int | None,
    strategy_id: int,
    severity: str,
    message: str,
    code: str | None = None,
) -> None:
    """엔진 이상 알림을 사용자별 이벤트 채널로 발행한다.

    :param severity: "warning" | "critical".
    :param message: 사람이 읽을 한국어 문장(프론트 토스트/배너에 그대로 노출 가능).
    :param code: 알림 종류 식별자(프론트 dedup·필터용, 예 "runner_failures").
    """
    logger.warning(
        "엔진 알림[%s] 전략 %s%s: %s",
        severity, strategy_id, f"({code})" if code else "", message,
    )
    if user_id is None:
        # 사용자를 특정할 수 없으면(적재 전) 전송 대상 채널이 없다 — 로그만 남긴다.
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
