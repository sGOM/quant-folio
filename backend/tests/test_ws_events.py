"""app/api/routes/ws.py::ws_events 검증(§신규발굴 4차 3위, 낮은 확신도).

이제까지 이 엔드포인트는 테스트가 전무했다. 코드 자체는 짧고 이미 방어적으로
작성돼 있어(FIRST_COMPLETED 로 relay/drain 양쪽 정리, unsubscribe/aclose 로
pubsub 리소스 해제) 실제 버그 가능성은 낮지만, 인증 실패 close(4401)·정상
중계·연결 종료 시 리소스 정리라는 핵심 계약은 검증해 둘 가치가 있다. 실
Redis 없이 redis_client.pubsub()·get_session_user_id 를 대역화해 검증한다.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.routes.ws as ws_mod


class _FakePubSub:
    """redis.asyncio.client.PubSub 대역 — 큐잉된 메시지를 순서대로 반환한다."""

    def __init__(self, queued: list[dict] | None = None):
        self._queue = list(queued or [])
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel):
        self.subscribed.append(channel)

    async def get_message(self, *, ignore_subscribe_messages=True, timeout=1.0):
        if self._queue:
            return self._queue.pop(0)
        await asyncio.sleep(0)  # 실제 1초 타임아웃 없이 제어권만 양보
        return None

    async def unsubscribe(self, channel):
        self.unsubscribed.append(channel)

    async def aclose(self):
        self.closed = True


class _FakeRedisClient:
    def __init__(self, pubsub: _FakePubSub):
        self._pubsub = pubsub

    def pubsub(self):
        return self._pubsub


def _build_app(monkeypatch, *, user_id: int | None, queued: list[dict] | None = None):
    fake_pubsub = _FakePubSub(queued)
    monkeypatch.setattr(ws_mod, "redis_client", _FakeRedisClient(fake_pubsub))

    async def _fake_authenticate(websocket):
        return user_id

    monkeypatch.setattr(ws_mod, "_authenticate", _fake_authenticate)

    app = FastAPI()
    app.include_router(ws_mod.router)
    return app, fake_pubsub


def test_unauthenticated_connection_closed_with_4401(monkeypatch):
    app, _ = _build_app(monkeypatch, user_id=None)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 4401


def test_authenticated_connection_sends_connected_then_relays_event(monkeypatch):
    event = {"type": "fill", "order_id": "abc123"}
    app, pubsub = _build_app(
        monkeypatch, user_id=42, queued=[{"data": json.dumps(event)}],
    )
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first == {"type": "connected", "user_id": 42}
        relayed = ws.receive_json()
        assert relayed == event

    # 연결 종료 후 pubsub 구독 해제·자원 정리가 이뤄졌는지 확인.
    assert pubsub.subscribed == ["engine:events:42"]
    assert pubsub.unsubscribed == ["engine:events:42"]
    assert pubsub.closed is True


def test_malformed_pubsub_message_is_skipped_not_relayed(monkeypatch):
    """JSON 파싱 실패 메시지는 무시되고, 그 다음 정상 메시지만 전달된다."""
    good = {"type": "order", "id": 1}
    app, _ = _build_app(
        monkeypatch,
        user_id=7,
        queued=[{"data": "not-json"}, {"data": json.dumps(good)}],
    )
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"type": "connected", "user_id": 7}
        assert ws.receive_json() == good
