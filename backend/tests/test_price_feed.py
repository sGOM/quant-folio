"""engine/price_feed.py::PriceFeedManager._supervise 의 연속 실패 알림 로직 검증.

REST 폴백이 있어 WS 재연결 실패가 매매를 막지는 않지만, 조용히 방치되면 실시간성이
저하된 채 오래갈 수 있어 §신규발굴 3차 1위로 임계-교차 1회 알림(price_feed_outage)을
추가했다. 여기서는 실제 KIS WS 연결 없이 KisWebSocketClient.run 을 대역화해
FAILURE_ALERT_THRESHOLD 교차 시점에만 정확히 1회 발행되는지, 성공 시 리셋되는지를
검증한다(이전까지 이 모듈은 테스트 0건이었음).
"""
from __future__ import annotations

from tests.conftest import FakeRedis as _FakeRedis


class _InstantStop:
    """asyncio.Event 대역 — wait()가 실제로 블로킹하지 않아 백오프 대기 없이 테스트가 빠르다."""

    def __init__(self):
        self._flag = False

    def is_set(self) -> bool:
        return self._flag

    def set(self) -> None:
        self._flag = True

    async def wait(self) -> bool:
        return self._flag


class _FakeDedupSession:
    """AsyncSessionLocal 대역 — dedup 없음 고정, add/commit 만 기록."""

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        class _Result:
            def scalar_one_or_none(self):
                return None
        return _Result()

    def add(self, obj) -> None:
        pass

    async def commit(self) -> None:
        pass


def _patch_alerts_session(monkeypatch):
    from engine import alerts
    monkeypatch.setattr(alerts, "AsyncSessionLocal", _FakeDedupSession())


class _FakeClient:
    """client.run 호출마다 지정된 결과(예외 또는 정상 반환)를 순서대로 재생한다."""

    def __init__(self, outcomes: list[Exception | None], stop: _InstantStop):
        self._outcomes = outcomes
        self._stop = stop
        self.calls = 0

    async def run(self, symbols, stop_event):
        outcome = self._outcomes[self.calls]
        self.calls += 1
        if self.calls >= len(self._outcomes):
            self._stop.set()
        if outcome is not None:
            raise outcome
        return None


async def test_alert_fires_exactly_once_at_threshold_crossing(monkeypatch):
    from engine.price_feed import PriceFeedManager

    _patch_alerts_session(monkeypatch)
    redis = _FakeRedis()
    mgr = PriceFeedManager(redis)
    stop = _InstantStop()
    client = _FakeClient([RuntimeError("conn1"), RuntimeError("conn2"), RuntimeError("conn3")], stop)

    await mgr._supervise(42, client, ["005930"], stop)

    assert client.calls == 3
    alerts = redis.alerts()
    assert len(alerts) == 1
    assert alerts[0]["code"] == "price_feed_outage"
    assert alerts[0]["user_id"] == 42
    assert alerts[0]["severity"] == "warning"
    assert mgr._fail_counts[42] == 3


async def test_no_alert_below_threshold(monkeypatch):
    from engine.price_feed import PriceFeedManager

    _patch_alerts_session(monkeypatch)
    redis = _FakeRedis()
    mgr = PriceFeedManager(redis)
    stop = _InstantStop()
    client = _FakeClient([RuntimeError("conn1"), RuntimeError("conn2")], stop)

    await mgr._supervise(42, client, ["005930"], stop)

    assert client.calls == 2
    assert redis.alerts() == []


async def test_success_resets_fail_count_before_reaching_threshold(monkeypatch):
    from engine.price_feed import PriceFeedManager

    _patch_alerts_session(monkeypatch)
    redis = _FakeRedis()
    mgr = PriceFeedManager(redis)
    stop = _InstantStop()
    # 실패 2회(임계 미달) → 성공(리셋) → 실패 1회에서 멈춤(총 4콜)
    client = _FakeClient(
        [RuntimeError("conn1"), RuntimeError("conn2"), None, RuntimeError("conn3")], stop
    )

    await mgr._supervise(42, client, ["005930"], stop)

    assert client.calls == 4
    # 성공으로 리셋됐으니 마지막 실패 1건만 누적 — 임계(3) 미달로 알림 없음.
    assert redis.alerts() == []
    assert mgr._fail_counts[42] == 1
