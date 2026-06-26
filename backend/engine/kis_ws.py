"""KIS 실시간 체결가 WebSocket 클라이언트 (H0STCNT0).

구독한 종목의 실시간 체결가를 받아 콜백(symbol, price)으로 전달한다.
PINGPONG 제어 프레임에 응답하고, 연결 끊김 시 호출자가 재연결한다(5단계 강화).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx
import websockets

from app.core.config import settings

logger = logging.getLogger("engine.kis_ws")

# 실시간 체결가 수신 콜백: (종목코드, 가격) → awaitable
PriceCallback = Callable[[str, Decimal], Awaitable[None]]


async def issue_approval_key(app_key: str, app_secret: str) -> str:
    """실시간 시세용 approval_key 발급."""
    url = f"{settings.kis_base_url}/oauth2/Approval"
    body = {"grant_type": "client_credentials", "appkey": app_key, "secretkey": app_secret}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body)
    resp.raise_for_status()
    return resp.json()["approval_key"]


def _subscribe_msg(approval_key: str, symbol: str, subscribe: bool = True) -> str:
    """KIS 실시간 체결가(H0STCNT0) 구독/해지 메시지(JSON 문자열)를 만든다.

    :param subscribe: True 면 구독(tr_type=1), False 면 해지(tr_type=2)
    """
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1" if subscribe else "2",
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": "H0STCNT0", "tr_key": symbol}},
    })


class KisWebSocketClient:
    """KIS 실시간 체결가 WebSocket 클라이언트.

    :param app_key: KIS App Key
    :param app_secret: KIS App Secret
    :param on_price: 체결가 수신 시 호출할 콜백(symbol, price)
    """

    def __init__(self, app_key: str, app_secret: str, on_price: PriceCallback):
        self._app_key = app_key
        self._app_secret = app_secret
        self._on_price = on_price
        self._symbols: set[str] = set()
        self._ws = None

    async def run(self, symbols: list[str], stop_event) -> None:
        """WS 연결 → 구독 → 수신 루프. stop_event 가 set 되면 종료."""
        approval_key = await issue_approval_key(self._app_key, self._app_secret)
        self._symbols = set(symbols)

        async with websockets.connect(
            settings.kis_ws_url, ping_interval=None, max_size=None
        ) as ws:
            self._ws = ws
            for sym in self._symbols:
                await ws.send(_subscribe_msg(approval_key, sym))
            logger.info("KIS WS 구독: %s (env=%s)", sorted(self._symbols), settings.KIS_ENV)

            while not stop_event.is_set():
                try:
                    raw = await ws.recv()
                except websockets.ConnectionClosed:
                    logger.warning("KIS WS 연결 종료")
                    break
                await self._handle(ws, raw)

    async def _handle(self, ws, raw: str) -> None:
        """수신 프레임 1건을 처리한다. PINGPONG 엔 pong 응답하고,
        실시간 체결 데이터면 종목·가격을 파싱해 콜백을 호출한다."""
        # 제어 프레임(JSON): PINGPONG 등
        if raw.startswith("{"):
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                return
            if msg.get("header", {}).get("tr_id") == "PINGPONG":
                await ws.pong(raw)
            return

        # 실시간 데이터: "0|H0STCNT0|001|005930^HHMMSS^체결가^..."
        parts = raw.split("|")
        if len(parts) < 4 or parts[1] != "H0STCNT0":
            return
        fields = parts[3].split("^")
        if len(fields) < 3:
            return
        symbol, price_str = fields[0], fields[2]
        try:
            price = Decimal(price_str)
        except Exception:  # noqa: BLE001
            return
        if price > 0:
            await self._on_price(symbol, price)
