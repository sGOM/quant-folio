"""worker/tasks.py::cleanup_old_alerts 보존정책(§21) 검증 — 읽음 90일/미읽음 180일 분리 삭제.

_cleanup_old_alerts_async 는 AsyncSessionLocal 을 함수 내부에서 지역 import 하므로
(app.core.database.AsyncSessionLocal 을 매 호출 재조회) 그 원본을 대역화한다. WHERE
절 평가는 직접 재구현하지 않고 SQLAlchemy 가 synchronize_session="evaluate" 에 쓰는
내부 평가기(sqlalchemy.orm.evaluator._EvaluatorCompiler)를 그대로 재사용해, 실제
delete() 문의 조건을 정확히(재구현 오차 없이) 평가한다.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm.evaluator import _EvaluatorCompiler

from app.models import Alert


def _mk_alert(*, is_read: bool, age_days: float) -> Alert:
    a = Alert(user_id=1, strategy_id=0, severity="warning", code="x", message="m")
    a.is_read = is_read
    a.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    return a


class _FakeDeleteSession:
    """AsyncSessionLocal 대역 — delete() 문의 whereclause 로 실제 삭제를 수행한다."""

    def __init__(self, rows: list[Alert]):
        self.rows = list(rows)

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        evaluator = _EvaluatorCompiler().process(stmt.whereclause)
        matched = [r for r in self.rows if evaluator(r)]
        for r in matched:
            self.rows.remove(r)

        class _Result:
            def __init__(self, n):
                self.rowcount = n

        return _Result(len(matched))

    async def commit(self) -> None:
        return None


async def test_cleanup_deletes_by_separate_read_unread_retention(monkeypatch):
    from app.core import database
    from worker import tasks

    rows = [
        _mk_alert(is_read=True, age_days=100),   # 읽음, 90일 초과 -> 삭제
        _mk_alert(is_read=True, age_days=10),    # 읽음, 90일 이내 -> 보존
        _mk_alert(is_read=False, age_days=200),  # 미읽음, 180일 초과 -> 삭제
        _mk_alert(is_read=False, age_days=100),  # 미읽음, 180일 이내(90일은 넘음) -> 보존
    ]
    session = _FakeDeleteSession(rows)
    monkeypatch.setattr(database, "AsyncSessionLocal", session)

    result = await tasks._cleanup_old_alerts_async()

    assert result["deleted"] == 2
    kept_flags = sorted(r.is_read for r in session.rows)
    assert kept_flags == [False, True]  # 90일 이내 읽음 1건 + 180일 이내 미읽음 1건만 남음


async def test_cleanup_noop_when_nothing_stale(monkeypatch):
    from app.core import database
    from worker import tasks

    rows = [
        _mk_alert(is_read=True, age_days=1),
        _mk_alert(is_read=False, age_days=1),
    ]
    session = _FakeDeleteSession(rows)
    monkeypatch.setattr(database, "AsyncSessionLocal", session)

    result = await tasks._cleanup_old_alerts_async()

    assert result["deleted"] == 0
    assert len(session.rows) == 2
