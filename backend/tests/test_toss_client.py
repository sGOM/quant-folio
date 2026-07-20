"""app/services/broker/toss.py::TossClient 의 HTTP 레벨(재시도·토큰 락) 검증.

기존 test_broker.py 는 TossClient._request 를 통째로 몽키패치해 대체하므로,
_request 내부(429 시 Retry-After 재시도)와 get_access_token 의 Redis 분산락
single-flight, _headers 의 계좌 필요 검증은 이제까지 한 번도 실행된 적 없었다
(§신규발굴 4차 1위). 토스는 모의투자 환경이 없어 자격증명이 항상 실거래로
동작하므로, 락 경합 시 이중 토큰 발급이나 429 처리 버그가 실제 주문 실패로
직결될 수 있다. 실 네트워크·Redis 없이 httpx.AsyncClient·redis_client·asyncio.sleep
을 대역화해 검증한다.
"""
from __future__ import annotations

import json as jsonlib

from tests.conftest import FakeRedis as _FakeRedis


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = jsonlib.dumps(self._payload)
        self.content = jsonlib.dumps(self._payload).encode() if payload is not None else b""
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeHttpxClient:
    """httpx.AsyncClient 대역 — .request()/.post() 호출마다 큐에서 응답을 꺼내 반환한다."""

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

    async def post(self, url, *, data=None):
        self.calls.append({"method": "POST", "url": url, "data": data})
        return self._responses[len(self.calls) - 1]


class _InstantAsyncio:
    """asyncio 대역 — sleep 을 즉시 반환시켜 429 백오프·락 대기의 real sleep 을 건너뛴다."""

    def __init__(self, on_sleep=None):
        self._on_sleep = on_sleep
        self.sleeps: list[float] = []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self._on_sleep:
            self._on_sleep()


def _client(monkeypatch, responses: list[_FakeResponse], *, client_id: str, on_sleep=None, account="acc-1"):
    from app.services.broker import toss as toss_mod

    fake_http = _FakeHttpxClient(responses)
    monkeypatch.setattr(toss_mod, "httpx", type("_H", (), {"AsyncClient": fake_http}))
    fake_redis = _FakeRedis()
    monkeypatch.setattr(toss_mod, "redis_client", fake_redis)
    fake_asyncio = _InstantAsyncio(on_sleep)
    monkeypatch.setattr(toss_mod, "asyncio", fake_asyncio)

    client = toss_mod.TossClient(client_id, "secret", account)
    return client, fake_http, fake_redis, fake_asyncio


def _token_ok(token="tok-1", expires_in=3600):
    return _FakeResponse(200, {"access_token": token, "expires_in": expires_in})


def _ok(payload=None):
    return _FakeResponse(200, payload or {})


def _rate_limited(retry_after="1"):
    return _FakeResponse(429, {}, headers={"Retry-After": retry_after})


async def test_get_quote_succeeds_issuing_token_once(monkeypatch):
    client, http, redis, _ = _client(
        monkeypatch,
        [_token_ok(), _ok({"result": [{"symbol": "005930", "lastPrice": "71500", "currency": "KRW"}]})],
        client_id="c-quote",
    )

    quote = await client.get_quote("005930")

    assert str(quote.price) == "71500"
    assert len(http.calls) == 2
    assert await redis.get("toss:token:c-quote") == "tok-1"


async def test_request_uses_cached_token_without_reissuing(monkeypatch):
    client, http, redis, _ = _client(
        monkeypatch,
        [_ok({"result": [{"symbol": "005930", "lastPrice": "71500"}]})],
        client_id="c-cached",
    )
    redis.store["toss:token:c-cached"] = "cached-tok"

    await client.get_quote("005930")

    assert len(http.calls) == 1


async def test_request_retries_once_on_429_then_succeeds(monkeypatch):
    client, http, redis, aio = _client(
        monkeypatch,
        [_rate_limited("2"), _ok({"result": [{"symbol": "005930", "lastPrice": "71500"}]})],
        client_id="c-retry",
    )
    redis.store["toss:token:c-retry"] = "cached-tok"

    quote = await client.get_quote("005930")

    assert str(quote.price) == "71500"
    assert len(http.calls) == 2
    assert aio.sleeps == [2.0]


async def test_request_gives_up_after_one_retry_still_429(monkeypatch):
    import pytest

    from app.services.broker.toss import TossError

    client, http, redis, _ = _client(
        monkeypatch,
        [_rate_limited("1"), _rate_limited("1")],
        client_id="c-exhausted",
    )
    redis.store["toss:token:c-exhausted"] = "cached-tok"

    # 두 번째 시도도 429 면 attempt==0 조건을 벗어나 "HTTP 429" 로 즉시 실패한다
    # (attempt 루프 마지막의 전용 메시지는 현재 로직상 도달하지 않는 경로).
    with pytest.raises(TossError, match="HTTP 429"):
        await client.get_quote("005930")

    assert len(http.calls) == 2


async def test_non_retryable_error_raises_immediately(monkeypatch):
    import pytest

    from app.services.broker.toss import TossError

    client, http, redis, _ = _client(
        monkeypatch,
        [_FakeResponse(500, {"error": "boom"})],
        client_id="c-500",
    )
    redis.store["toss:token:c-500"] = "cached-tok"

    with pytest.raises(TossError, match="HTTP 500"):
        await client.get_quote("005930")

    assert len(http.calls) == 1


async def test_place_order_without_account_raises_before_any_http_call(monkeypatch):
    import pytest

    from app.services.broker.toss import TossError

    client, http, redis, _ = _client(
        monkeypatch, [], client_id="c-noacct", account=None,
    )

    with pytest.raises(TossError, match="계좌"):
        await client.place_order("005930", "buy", 1)

    assert len(http.calls) == 0


async def test_get_access_token_waits_for_lock_holder_then_reuses_their_token(monkeypatch):
    """다른 프로세스가 락을 쥔 동안엔 재발급하지 않고 캐시가 뜨길 기다린다."""
    cache_key = "toss:token:c-lockwait"
    lock_key = f"{cache_key}:lock"

    def _on_sleep():
        # 락 보유자가 발급을 마치고 캐시를 채운 상황을 흉내낸다.
        redis.store[cache_key] = "holder-token"

    client, http, redis, aio = _client(
        monkeypatch, [], client_id="c-lockwait", on_sleep=_on_sleep,
    )
    redis.store[lock_key] = "1"  # 이미 다른 프로세스가 락 보유 중

    token = await client.get_access_token()

    assert token == "holder-token"
    assert len(http.calls) == 0
    assert len(aio.sleeps) == 1
