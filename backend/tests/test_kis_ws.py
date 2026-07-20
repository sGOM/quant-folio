"""engine/kis_ws.py 순수 로직 검증 — 구독 메시지 생성·프레임 파싱(PINGPONG/체결가).

실제 WS 연결 없이도 검증 가능한 부분(_subscribe_msg, KisWebSocketClient._handle)만
다룬다. run()/issue_approval_key()는 실 네트워크(websockets.connect/httpx)가 필요해
이 스위트의 범위 밖(§신규발굴 3차 2위 — 이전까지 이 모듈은 테스트 0건이었음).
"""
from __future__ import annotations

import json
from decimal import Decimal

from engine.kis_ws import KisWebSocketClient, _subscribe_msg


def test_subscribe_msg_sets_tr_type_1_for_subscribe():
    raw = _subscribe_msg("appkey123", "005930", subscribe=True)
    msg = json.loads(raw)
    assert msg["header"]["tr_type"] == "1"
    assert msg["header"]["approval_key"] == "appkey123"
    assert msg["body"]["input"]["tr_id"] == "H0STCNT0"
    assert msg["body"]["input"]["tr_key"] == "005930"


def test_subscribe_msg_sets_tr_type_2_for_unsubscribe():
    raw = _subscribe_msg("appkey123", "005930", subscribe=False)
    msg = json.loads(raw)
    assert msg["header"]["tr_type"] == "2"


class _FakeWs:
    def __init__(self):
        self.ponged: list[str] = []

    async def pong(self, raw):
        self.ponged.append(raw)


async def test_handle_pingpong_frame_responds_with_pong():
    received: list[tuple[str, Decimal]] = []

    async def on_price(sym, price):
        received.append((sym, price))

    client = KisWebSocketClient("k", "s", on_price)
    ws = _FakeWs()
    raw = json.dumps({"header": {"tr_id": "PINGPONG"}})

    await client._handle(ws, raw)

    assert ws.ponged == [raw]
    assert received == []


async def test_handle_other_json_control_frame_ignored():
    received: list[tuple[str, Decimal]] = []

    async def on_price(sym, price):
        received.append((sym, price))

    client = KisWebSocketClient("k", "s", on_price)
    ws = _FakeWs()

    await client._handle(ws, json.dumps({"header": {"tr_id": "SOMETHING_ELSE"}}))

    assert ws.ponged == []
    assert received == []


async def test_handle_price_frame_parses_symbol_and_price():
    received: list[tuple[str, Decimal]] = []

    async def on_price(sym, price):
        received.append((sym, price))

    client = KisWebSocketClient("k", "s", on_price)
    ws = _FakeWs()
    raw = "0|H0STCNT0|001|005930^093015^71500^..."

    await client._handle(ws, raw)

    assert received == [("005930", Decimal("71500"))]


async def test_handle_price_frame_wrong_tr_id_ignored():
    received: list[tuple[str, Decimal]] = []

    async def on_price(sym, price):
        received.append((sym, price))

    client = KisWebSocketClient("k", "s", on_price)
    ws = _FakeWs()

    await client._handle(ws, "0|H0STASP0|001|005930^093015^71500^...")

    assert received == []


async def test_handle_price_frame_zero_price_ignored():
    received: list[tuple[str, Decimal]] = []

    async def on_price(sym, price):
        received.append((sym, price))

    client = KisWebSocketClient("k", "s", on_price)
    ws = _FakeWs()

    await client._handle(ws, "0|H0STCNT0|001|005930^093015^0^...")

    assert received == []


async def test_handle_price_frame_malformed_price_ignored():
    received: list[tuple[str, Decimal]] = []

    async def on_price(sym, price):
        received.append((sym, price))

    client = KisWebSocketClient("k", "s", on_price)
    ws = _FakeWs()

    await client._handle(ws, "0|H0STCNT0|001|005930^093015^N/A^...")

    assert received == []
