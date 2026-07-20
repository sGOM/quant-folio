"""app/api/routes/alerts.py 핸들러 단위 검증 — has_more 계산·확인 처리 소유권 체크(§17/§21).

라우트 핸들러 함수를 직접 호출하고(HTTP 계층 없이), db.execute 가 반환하는 로우를
그대로 통제하는 최소 대역만 쓴다(실제 WHERE 절 해석은 SQLAlchemy 자체를 신뢰하고
테스트하지 않음 — test_fill_quality_route.py 와 같은 관례). limit+1건 조회로
has_more 를 계산하는 로직(방금 §21 후속으로 추가)과, mark_alert_read 의 소유권
체크(본인 알림 또는 전역 알림만 허용)를 중심으로 검증한다.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes.alerts import list_alerts, mark_alert_read, mark_all_alerts_read
from app.models import Alert


def _alert(id_, *, user_id=1, is_read=False):
    a = Alert(user_id=user_id, strategy_id=0, severity="warning", code="x", message="m")
    a.id = id_
    a.is_read = is_read
    a.created_at = datetime.now(timezone.utc)
    return a


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._rows


class _FakeListSession:
    """list_alerts 전용 — 호출 순서(항목 조회 -> unread_count 조회)를 그대로 흉내낸다."""

    def __init__(self, item_rows: list[Alert], unread_count: int):
        self._item_rows = item_rows
        self._unread_count = unread_count
        self.calls = 0

    async def execute(self, _stmt):
        self.calls += 1
        if self.calls == 1:
            return _ScalarResult(self._item_rows)
        return _ScalarResult(self._unread_count)


_USER = SimpleNamespace(id=1, email="tester@example.com")


async def test_has_more_true_when_extra_row_fetched():
    # limit=2 인데 라우트가 limit+1=3 건 조회 로직을 쓰므로, DB 가 3건을 돌려주면
    # has_more=True 이고 응답 items 는 앞의 2건만 남아야 한다.
    rows = [_alert(1), _alert(2), _alert(3)]
    session = _FakeListSession(rows, unread_count=3)

    result = await list_alerts(limit=2, offset=0, current=_USER, db=session)

    assert result.has_more is True
    assert [i.id for i in result.items] == [1, 2]


async def test_has_more_false_when_exactly_at_limit():
    rows = [_alert(1), _alert(2)]
    session = _FakeListSession(rows, unread_count=0)

    result = await list_alerts(limit=2, offset=0, current=_USER, db=session)

    assert result.has_more is False
    assert len(result.items) == 2


async def test_has_more_false_when_fewer_than_limit():
    rows = [_alert(1)]
    session = _FakeListSession(rows, unread_count=1)

    result = await list_alerts(limit=50, offset=0, current=_USER, db=session)

    assert result.has_more is False
    assert len(result.items) == 1


class _FakeGetSession:
    def __init__(self, alert: Alert | None):
        self._alert = alert
        self.committed = False

    async def get(self, _model, _id):
        return self._alert

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


async def test_mark_read_allows_own_alert():
    a = _alert(1, user_id=1, is_read=False)
    session = _FakeGetSession(a)

    result = await mark_alert_read(1, current=_USER, db=session)

    assert result.is_read is True
    assert session.committed is True


async def test_mark_read_allows_global_alert():
    a = _alert(1, user_id=None, is_read=False)
    session = _FakeGetSession(a)

    result = await mark_alert_read(1, current=_USER, db=session)

    assert result.is_read is True


async def test_mark_read_rejects_other_users_alert():
    a = _alert(1, user_id=2, is_read=False)  # 다른 사용자 소유
    session = _FakeGetSession(a)

    with pytest.raises(HTTPException) as exc_info:
        await mark_alert_read(1, current=_USER, db=session)
    assert exc_info.value.status_code == 404


async def test_mark_read_rejects_missing_alert():
    session = _FakeGetSession(None)

    with pytest.raises(HTTPException) as exc_info:
        await mark_alert_read(999, current=_USER, db=session)
    assert exc_info.value.status_code == 404


class _FakeUpdateSession:
    def __init__(self, rowcount: int):
        self._rowcount = rowcount

    async def execute(self, _stmt):
        return SimpleNamespace(rowcount=self._rowcount)

    async def commit(self):
        return None


async def test_mark_all_read_returns_updated_count():
    session = _FakeUpdateSession(rowcount=7)

    result = await mark_all_alerts_read(current=_USER, db=session)

    assert result == {"updated": 7}
