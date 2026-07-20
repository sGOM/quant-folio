"""engine/main.py::_control_loop 자기 치유(재구독) 검증(백엔드 전체 버그 검사 발견분).

이전엔 `pubsub.get_message()`가 좁게 잡힌 `except (json.JSONDecodeError,
KeyError, ValueError)`에 걸리지 않는 예외(예: Redis 순간 단절의
`ConnectionError`)를 던지면 `_control_loop` 태스크 자체가 아무도 모르게
조용히 종료됐다. `_heartbeat_loop`는 별도 태스크라 계속 살아 있어
`/api/engine/health`는 "정상"으로 보이므로, 이후 전략 start/stop 원격제어
(웹의 "중지" 버튼 등)가 전부 무시돼도 운영자가 알아채기 어려웠다.
`_reconcile_loop`와 동일한 패턴(넓은 except 로 재구독 + 임계-교차 1회 알림)
으로 수정한 뒤, 정상 메시지 처리·예외 재구독·연속 실패 알림·성공 시 카운트
리셋을 검증한다.
"""
from __future__ import annotations

import engine.main as main_mod
from app.core.channels import FAILURE_ALERT_THRESHOLD


class _RaisingPubSub:
    """subscribe 는 성공하지만 get_message 호출마다 예외를 던지는 대역."""

    def __init__(self, counter: dict):
        self.counter = counter

    async def subscribe(self, channel):
        self.counter["subscribes"] += 1

    async def get_message(self, *, ignore_subscribe_messages=True, timeout=1.0):
        raise ConnectionError("redis 순간 단절(시뮬레이션)")

    async def unsubscribe(self, channel):
        pass

    async def aclose(self):
        pass


class _QueuePubSub:
    """subscribe 성공 + 큐에 쌓인 메시지를 순서대로 반환하는 정상 대역."""

    def __init__(self, counter: dict, queued: list[dict]):
        self.counter = counter
        self._queue = list(queued)

    async def subscribe(self, channel):
        self.counter["subscribes"] += 1

    async def get_message(self, *, ignore_subscribe_messages=True, timeout=1.0):
        if self._queue:
            return self._queue.pop(0)
        return None

    async def unsubscribe(self, channel):
        pass

    async def aclose(self):
        pass


class _FakeRedisClient:
    def __init__(self, factory):
        self._factory = factory

    def pubsub(self):
        return self._factory()


def _reset_globals(monkeypatch):
    monkeypatch.setattr(main_mod, "_control_fail_count", 0)
    monkeypatch.setattr(main_mod, "_CONTROL_RETRY_DELAY", 0)
    main_mod._shutdown.clear()


async def test_processes_start_and_stop_messages_then_exits_on_shutdown(monkeypatch):
    _reset_globals(monkeypatch)
    counter = {"subscribes": 0}
    started: list[int] = []

    async def _fake_start(sid):
        started.append(sid)
        main_mod._shutdown.set()

    monkeypatch.setattr(main_mod, "_start_strategy", _fake_start)
    queued = [{"data": '{"action": "start", "strategy_id": 5}'}]
    monkeypatch.setattr(
        main_mod, "redis_client", _FakeRedisClient(lambda: _QueuePubSub(counter, queued)),
    )

    await main_mod._control_loop()

    assert started == [5]
    assert counter["subscribes"] == 1


async def test_ignores_malformed_message_and_continues(monkeypatch):
    _reset_globals(monkeypatch)
    counter = {"subscribes": 0}
    calls: list[int] = []

    async def _fake_start(sid):
        calls.append(sid)
        main_mod._shutdown.set()

    monkeypatch.setattr(main_mod, "_start_strategy", _fake_start)
    queued = [
        {"data": "not-json"},
        {"data": '{"action": "start", "strategy_id": 9}'},
    ]
    monkeypatch.setattr(
        main_mod, "redis_client", _FakeRedisClient(lambda: _QueuePubSub(counter, queued)),
    )

    await main_mod._control_loop()

    assert calls == [9]  # 잘못된 메시지는 건너뛰고 다음 정상 메시지만 처리.


async def test_recovers_from_get_message_exception_and_alerts_at_threshold(monkeypatch):
    """get_message 가 좁은 except 밖의 예외를 던져도 루프가 죽지 않고 재구독하며,
    연속 실패가 임계에 도달하면 1회 critical 알림을 발행한다.
    """
    _reset_globals(monkeypatch)
    counter = {"subscribes": 0}
    alerts: list[dict] = []

    async def _fake_publish_alert(redis, **kwargs):
        alerts.append(kwargs)
        main_mod._shutdown.set()  # 알림 발행을 관측한 뒤 루프를 정지시킨다.

    monkeypatch.setattr(main_mod, "publish_alert", _fake_publish_alert)
    monkeypatch.setattr(
        main_mod, "redis_client", _FakeRedisClient(lambda: _RaisingPubSub(counter)),
    )

    await main_mod._control_loop()

    assert counter["subscribes"] == FAILURE_ALERT_THRESHOLD  # 매번 재구독을 시도
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["code"] == "runner_failures"
    assert f"{FAILURE_ALERT_THRESHOLD}회" in alerts[0]["message"]
    assert main_mod._control_fail_count == FAILURE_ALERT_THRESHOLD


async def test_fail_count_resets_after_recovering_from_failure(monkeypatch):
    """실패 후 재구독에 성공하면 연속 실패 카운트가 0으로 리셋된다."""
    _reset_globals(monkeypatch)
    counter = {"subscribes": 0}
    attempts = {"n": 0}

    async def _fake_start(sid):
        main_mod._shutdown.set()

    monkeypatch.setattr(main_mod, "_start_strategy", _fake_start)

    def _factory():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return _RaisingPubSub(counter)  # 1차 시도는 실패
        return _QueuePubSub(counter, [{"data": '{"action": "start", "strategy_id": 1}'}])

    monkeypatch.setattr(main_mod, "redis_client", _FakeRedisClient(_factory))

    await main_mod._control_loop()

    assert attempts["n"] == 2
    assert main_mod._control_fail_count == 0  # 재구독 성공으로 리셋됨
