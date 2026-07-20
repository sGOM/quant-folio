"""engine/alerts.py::publish_alert 의 dedup_window_hours DB 레벨 dedup 검증(§21).

기존 test_engine_health.py 는 publish_alert 의 WS/텔레그램 발송 경로를 다루지만
dedup_window_hours(같은 (user_id, strategy_id, code)의 미확인 알림이 창 안에 이미
있으면 DB 재적재를 생략)는 커버가 없었다. AsyncSessionLocal 을 대역화해 DB 조회
결과(기존 미확인 중복 유무)만 통제하고, 실제 publish_alert 로직이 add/commit 을
건너뛰는지·WS 발송은 dedup 과 무관하게 항상 나가는지를 검증한다.
"""
from tests.conftest import FakeRedis as _FakeRedis


class _FakeDedupSession:
    """AsyncSessionLocal 대역 — dedup 존재 조회 결과를 고정값으로 반환한다."""

    def __init__(self, existing_dup_id: int | None):
        self._existing_dup_id = existing_dup_id
        self.execute_called = False
        self.added: list = []
        self.committed = False

    def __call__(self):  # AsyncSessionLocal() 처럼 팩토리로도 호출 가능
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        self.execute_called = True

        class _Result:
            def __init__(self, val):
                self._val = val

            def scalar_one_or_none(self):
                return self._val

        return _Result(self._existing_dup_id)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True


async def test_dedup_skips_insert_when_recent_unread_duplicate_exists(monkeypatch):
    from engine import alerts

    session = _FakeDedupSession(existing_dup_id=99)
    monkeypatch.setattr(alerts, "AsyncSessionLocal", session)
    redis = _FakeRedis()

    await alerts.publish_alert(
        redis, user_id=1, strategy_id=5, severity="warning",
        message="창고 온도 이상", code="db_backup_stale", dedup_window_hours=20,
    )

    assert session.execute_called is True
    assert session.added == []
    assert session.committed is False
    # WS 는 dedup 여부와 무관하게 항상 발송돼야 실시간성이 유지된다.
    assert len(redis.alerts()) == 1


async def test_dedup_inserts_when_no_recent_duplicate(monkeypatch):
    from engine import alerts

    session = _FakeDedupSession(existing_dup_id=None)
    monkeypatch.setattr(alerts, "AsyncSessionLocal", session)
    redis = _FakeRedis()

    await alerts.publish_alert(
        redis, user_id=1, strategy_id=5, severity="warning",
        message="창고 온도 이상", code="db_backup_stale", dedup_window_hours=20,
    )

    assert len(session.added) == 1
    assert session.committed is True
    assert len(redis.alerts()) == 1


async def test_dedup_window_none_skips_dup_check_and_always_inserts(monkeypatch):
    """dedup_window_hours 미지정(None)이면 기존 동작(매번 적재)과 동일 — dup 조회 자체를 안 한다."""
    from engine import alerts

    session = _FakeDedupSession(existing_dup_id=99)  # 있어도 무시돼야 함
    monkeypatch.setattr(alerts, "AsyncSessionLocal", session)
    redis = _FakeRedis()

    await alerts.publish_alert(
        redis, user_id=1, strategy_id=5, severity="critical",
        message="MDD 킬스위치 발동", code="mdd_kill",
    )

    assert session.execute_called is False
    assert len(session.added) == 1
    assert session.committed is True
