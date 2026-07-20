"""app/services/kis/client.py::KisClient._request_json 의 유량제한(EGW00201) 재시도 검증.

기존엔 get_current_price 에만 재시도가 있고 place_order/get_order_execution/
get_balance 는 rt_cd!=0 이면 재시도 없이 바로 실패했다(§신규발굴 3차 3위) — 주문·체결·
잔고 확인이 시세 조회보다 실패 파급이 큰데도 더 약하게 보호돼 있었다. 공통 헬퍼
`_request_json`으로 통합한 뒤, 실제 KIS/Redis 네트워크 없이 httpx.AsyncClient·
redis_client 를 대역화해 재시도/포기 경로를 검증한다(이전까지 KisClient 의 HTTP
경로는 테스트 0건이었음).
"""
from __future__ import annotations

import json as jsonlib

from tests.conftest import FakeRedis as _FakeRedis


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = jsonlib.dumps(payload)

    def json(self):
        return self._payload


class _FakeHttpxClient:
    """httpx.AsyncClient 대역 — .request() 호출마다 큐에서 응답을 꺼내 반환한다."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = responses
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, *, headers=None, params=None, json=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        return self._responses[len(self.calls) - 1]


def _client(monkeypatch, responses: list[_FakeResponse], *, app_key: str):
    """KisClient 를 만들고 httpx/redis 를 대역화한다. app_key 는 테스트마다 달리해
    프로세스 전역 rate limiter(_rate_limiters)가 다른 테스트와 섞이지 않게 한다.
    토큰은 캐시에 미리 채워 둬 _request_json 대상 호출(응답 큐)과 토큰 발급 호출이
    섞이지 않게 한다(토큰 발급 경로는 이 스위트 범위 밖)."""
    from app.core.config import settings
    from app.services.kis import client as kis_client_mod

    fake_http = _FakeHttpxClient(responses)
    monkeypatch.setattr(kis_client_mod, "httpx", type("_H", (), {"AsyncClient": fake_http}))
    fake_redis = _FakeRedis()
    fake_redis.store[f"kis:token:{settings.KIS_ENV}:{app_key}"] = "cached-token"
    monkeypatch.setattr(kis_client_mod, "redis_client", fake_redis)

    client = kis_client_mod.KisClient(app_key, "secret", "50012345-01")
    return client, fake_http


def _ok(output=None):
    return _FakeResponse(200, {"rt_cd": "0", "output": output or {}})


def _rate_limited():
    return _FakeResponse(200, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"})


def _rejected():
    return _FakeResponse(200, {"rt_cd": "1", "msg_cd": "APBK0013", "msg1": "주문 거부 사유"})


async def test_get_current_price_succeeds_without_retry_needed(monkeypatch):
    client, http = _client(
        monkeypatch, [_ok({"stck_prpr": "71500"})], app_key="k-success",
    )

    out = await client.get_current_price("005930")

    assert out["stck_prpr"] == "71500"
    assert len(http.calls) == 1


async def test_get_current_price_retries_on_rate_limit_then_succeeds(monkeypatch):
    client, http = _client(
        monkeypatch, [_rate_limited(), _ok({"stck_prpr": "71500"})], app_key="k-retry-price",
    )

    out = await client.get_current_price("005930")

    assert out["stck_prpr"] == "71500"
    assert len(http.calls) == 2


async def test_get_current_price_gives_up_after_max_retries(monkeypatch):
    import pytest

    from app.services.kis.client import KisError, _RATE_RETRY

    client, http = _client(
        monkeypatch, [_rate_limited()] * _RATE_RETRY, app_key="k-exhausted-price",
    )

    with pytest.raises(KisError, match="EGW00201"):
        await client.get_current_price("005930")

    assert len(http.calls) == _RATE_RETRY


async def test_non_rate_limit_error_raises_immediately_without_retry(monkeypatch):
    import pytest

    from app.services.kis.client import KisError

    client, http = _client(monkeypatch, [_rejected()], app_key="k-rejected")

    with pytest.raises(KisError, match="APBK0013"):
        await client.get_current_price("005930")

    assert len(http.calls) == 1


async def test_get_balance_retries_on_rate_limit_then_succeeds(monkeypatch):
    """place_order/get_order_execution 과 함께 §3위 이전엔 재시도가 없던 잔고조회."""
    client, http = _client(
        monkeypatch,
        [_rate_limited(), _ok({"output1": [], "output2": []})],
        app_key="k-retry-balance",
    )

    balance = await client.get_balance()

    assert balance.positions == []
    assert len(http.calls) == 2
