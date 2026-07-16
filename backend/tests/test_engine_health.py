"""엔진 관측성 — 러너 헬스 상태 갱신·임계 초과 알림 발행 검증.

BaseRunner 의 헬스 추적(마지막 성공 틱·연속 실패)과 알림 dedup(임계 처음 넘을 때만 1회
발행, 성공 리셋 후 재발화 가능)을 mock Redis 로 검증한다. 외부 I/O(브로커·DB·pykrx)는
타지 않고 상태기계만 본다.
"""
import json

import pytest

from app.core.channels import FAILURE_ALERT_THRESHOLD, engine_health_key
from engine.base_runner import BaseRunner


class _FakeRedis:
    """set/get/publish 만 지원하는 인메모리 Redis 대역(발행 기록 보관)."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.store:
            return None
        self.store[k] = v
        return True

    async def delete(self, k):
        self.store.pop(k, None)

    async def publish(self, channel, data):
        self.published.append((channel, data))
        return 1

    def alerts(self) -> list[dict]:
        # publish_event 는 사용자별 채널 + 공용 채널 양쪽에 발행하므로, 중복 카운트를
        # 피하려 공용 채널("engine:events")만 집계한다.
        from app.core.channels import ENGINE_EVENTS_CHANNEL

        out = []
        for ch, data in self.published:
            if ch != ENGINE_EVENTS_CHANNEL:
                continue
            try:
                payload = json.loads(data)
            except (ValueError, TypeError):
                continue
            if payload.get("type") == "alert":
                out.append(payload)
        return out


class _Runner(BaseRunner):
    _label = "테스트전략"

    def _log_start(self) -> None:  # pragma: no cover - 미사용
        pass

    async def _tick_once(self) -> None:  # pragma: no cover - 미사용
        pass


def _make(redis=None, user_id=7):
    r = _Runner(strategy_id=42, redis=redis or _FakeRedis())
    r._user_id = user_id
    return r


async def test_tick_success_records_health_and_resets_failures():
    r = _make()
    r._consecutive_failures = 2  # 직전에 실패가 쌓여 있었다고 가정
    await r._record_tick_success()

    assert r._consecutive_failures == 0
    state = json.loads(r.redis.store[engine_health_key(42)])
    assert state["consecutive_failures"] == 0
    assert state["last_error"] is None
    assert state["last_success_ts"] is not None
    assert state["user_id"] == 7


async def test_tick_failure_increments_and_records_last_error():
    r = _make()
    await r._record_tick_failure("tick 오류: boom")

    assert r._consecutive_failures == 1
    state = json.loads(r.redis.store[engine_health_key(42)])
    assert state["consecutive_failures"] == 1
    assert "boom" in state["last_error"]
    # 임계 미만이므로 알림은 아직 없다.
    assert r.redis.alerts() == []


async def test_alert_fires_once_exactly_at_threshold():
    r = _make()
    for _ in range(FAILURE_ALERT_THRESHOLD - 1):
        await r._record_tick_failure("타임아웃")
    assert r.redis.alerts() == []  # 임계 직전까지는 무발행

    await r._record_tick_failure("타임아웃")  # 임계 도달
    alerts = r.redis.alerts()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["type"] == "alert"
    assert a["severity"] == "critical"
    assert a["code"] == "runner_failures"
    assert a["strategy_id"] == 42
    assert a["user_id"] == 7
    assert "ts" in a and "message" in a

    # 임계 초과 지속 — 재발송하지 않는다(dedup).
    await r._record_tick_failure("타임아웃")
    assert len(r.redis.alerts()) == 1


async def test_alert_refires_after_success_reset():
    r = _make()
    for _ in range(FAILURE_ALERT_THRESHOLD):
        await r._record_tick_failure("타임아웃")
    assert len(r.redis.alerts()) == 1

    await r._record_tick_success()  # 회복 → 카운트 0
    for _ in range(FAILURE_ALERT_THRESHOLD):
        await r._record_tick_failure("타임아웃")
    assert len(r.redis.alerts()) == 2  # 다시 임계 도달 → 재발화


async def test_alert_skipped_when_user_unknown():
    """적재 전(user_id None)이면 전송 채널이 없어 발행하지 않는다(로그만)."""
    r = _make(user_id=None)
    for _ in range(FAILURE_ALERT_THRESHOLD):
        await r._record_tick_failure("타임아웃")
    assert r.redis.alerts() == []


async def test_pit_fallback_alert_dedups_until_recovery():
    """PIT 폴백 알림은 폴백 진입 시 1회만, 성공 회복 후 재진입 시 재발화."""
    from engine.rebalance_runner import RebalanceRunner

    redis = _FakeRedis()
    r = RebalanceRunner(strategy_id=5, redis=redis)
    r._user_id = 3

    await r._alert_pit_fallback("KOSPI200", "조회 실패")
    await r._alert_pit_fallback("KOSPI200", "조회 실패")  # 같은 구간 재발송 안 함
    assert len(redis.alerts()) == 1
    assert redis.alerts()[0]["code"] == "pit_fallback"
    assert redis.alerts()[0]["severity"] == "warning"

    r._pit_fallback_active = False  # PIT 조회 성공으로 해제됐다고 가정
    await r._alert_pit_fallback("KOSPI200", "조회 실패")
    assert len(redis.alerts()) == 2  # 재진입 → 재발화


async def test_publish_alert_targets_user_channel():
    """publish_alert 은 사용자별 채널 + 공용 채널 양쪽에 발행한다(fills.publish_event 재사용)."""
    from app.core.channels import ENGINE_EVENTS_CHANNEL, engine_events_channel
    from engine.alerts import publish_alert

    redis = _FakeRedis()
    await publish_alert(
        redis, user_id=9, strategy_id=1, severity="warning",
        message="점검 필요", code="pit_fallback",
    )
    channels = {ch for ch, _ in redis.published}
    assert engine_events_channel(9) in channels
    assert ENGINE_EVENTS_CHANNEL in channels


class _FakeTelegramResponse:
    def raise_for_status(self):
        pass


class _FakeTelegramClient:
    """httpx.AsyncClient 대역 — sendMessage 호출을 기록한다."""

    calls: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        _FakeTelegramClient.calls.append((url, json))
        return _FakeTelegramResponse()


async def test_publish_alert_sends_telegram_for_critical_when_configured(monkeypatch):
    """critical 알림이고 텔레그램이 설정돼 있으면 봇 API 로 발송한다."""
    from app.core.config import settings
    from engine import alerts

    _FakeTelegramClient.calls = []
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(alerts.httpx, "AsyncClient", _FakeTelegramClient)

    redis = _FakeRedis()
    await alerts.publish_alert(
        redis, user_id=9, strategy_id=1, severity="critical",
        message="MDD 킬스위치 발동", code="mdd_kill",
    )

    assert len(_FakeTelegramClient.calls) == 1
    url, payload = _FakeTelegramClient.calls[0]
    assert url == "https://api.telegram.org/bottest-token/sendMessage"
    assert payload["chat_id"] == "12345"
    assert "MDD 킬스위치 발동" in payload["text"]


async def test_publish_alert_skips_telegram_when_not_critical(monkeypatch):
    from app.core.config import settings
    from engine import alerts

    _FakeTelegramClient.calls = []
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(alerts.httpx, "AsyncClient", _FakeTelegramClient)

    redis = _FakeRedis()
    await alerts.publish_alert(
        redis, user_id=9, strategy_id=1, severity="warning",
        message="점검 필요", code="pit_fallback",
    )

    assert _FakeTelegramClient.calls == []


async def test_publish_alert_skips_telegram_when_unconfigured(monkeypatch):
    from app.core.config import settings
    from engine import alerts

    _FakeTelegramClient.calls = []
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(alerts.httpx, "AsyncClient", _FakeTelegramClient)

    redis = _FakeRedis()
    await alerts.publish_alert(
        redis, user_id=None, strategy_id=1, severity="critical",
        message="MDD 킬스위치 발동", code="mdd_kill",
    )

    assert _FakeTelegramClient.calls == []


async def test_publish_alert_swallows_telegram_send_failure(monkeypatch):
    """텔레그램 발송이 실패해도 예외를 전파하지 않는다(앱 내 알림 흐름 보호)."""
    from app.core.config import settings
    from engine import alerts

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            raise RuntimeError("network boom")

    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(alerts.httpx, "AsyncClient", _BoomClient)

    redis = _FakeRedis()
    await alerts.publish_alert(
        redis, user_id=9, strategy_id=1, severity="critical",
        message="MDD 킬스위치 발동", code="mdd_kill",
    )  # 예외 없이 완료돼야 함

    assert len(redis.alerts()) == 1  # WS 알림은 정상 발행됨
