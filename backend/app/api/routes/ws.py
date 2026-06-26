"""실시간 푸시 WebSocket — 엔진 이벤트를 사용자에게 중계.

엔진(분리 프로세스)이 Redis 사용자별 채널로 발행한 체결/주문/포지션
이벤트를 구독해 해당 사용자 연결로 푸시한다.
인증은 HttpOnly 세션 쿠키(qf_session)로 처리한다 — 세션 ID 를 URL/JS 에 노출하지 않는다.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.deps import SESSION_COOKIE
from app.core.channels import engine_events_channel
from app.core.redis import redis_client
from app.core.session import get_session_user_id

logger = logging.getLogger(__name__)
router = APIRouter()


async def _authenticate(websocket: WebSocket) -> int | None:
    """세션 쿠키(qf_session)를 검증해 user_id 를 반환한다. 무효면 None."""
    sid = websocket.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    return await get_session_user_id(sid)


@router.websocket("/ws")
async def ws_events(websocket: WebSocket):
    """실시간 이벤트 WebSocket 엔드포인트.

    쿠키로 인증한 뒤 사용자 전용 Redis 채널을 구독해 엔진 이벤트를 푸시한다.
    인증 실패 시 close code 4401 로 닫는다. 중계와 수신(끊김 감지)을 병행하다
    어느 한쪽이 끝나면 정리한다.
    """
    user_id = await _authenticate(websocket)
    if user_id is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    pubsub = redis_client.pubsub()
    channel = engine_events_channel(user_id)
    await pubsub.subscribe(channel)
    await websocket.send_json({"type": "connected", "user_id": user_id})

    async def _relay() -> None:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                continue
            try:
                payload = json.loads(msg["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            await websocket.send_json(payload)

    async def _drain() -> None:
        # 클라이언트가 보내는 메시지를 소비하며 끊김(close)을 즉시 감지한다.
        while True:
            await websocket.receive_text()

    relay = asyncio.create_task(_relay())
    drain = asyncio.create_task(_drain())
    try:
        # 둘 중 하나라도(끊김/오류) 끝나면 종료.
        done, pending = await asyncio.wait(
            {relay, drain}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        # 예외 전파(WebSocketDisconnect 등)는 finally 이후 무시.
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                logger.exception("WS 중계 오류", exc_info=exc)
    finally:
        relay.cancel()
        drain.cancel()
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
