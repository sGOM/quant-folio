"""전략 삭제 가드 검증 — app.api.routes.strategies.delete_strategy.

가동 중(DB status=LIVE 또는 엔진 활성 집합)이거나 미청산 포지션이 있으면 삭제를 거부해
"유령 포지션"(아무도 관리하지 않는 미청산 포지션)이 남지 않도록 한다. FakeRedis·FakeDB
(conftest.py) 를 재사용해 실제 Redis/DB 없이 세 가지 가드 조건과 정상 삭제 경로를 검증한다.
"""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api.routes import strategies as strategies_routes
from app.models import Position, Strategy, StrategyStatus, User

from .conftest import FakeRedis, _Store, make_session_factory


class _SismemberFakeRedis(FakeRedis):
    """FakeRedis 에 엔진 활성 전략 집합(sismember) 흉내를 추가한 대역."""

    def __init__(self, active_ids: set[int] | None = None):
        super().__init__()
        self._active = set(active_ids or set())

    async def sismember(self, key, value):  # noqa: ARG002 - key 는 상수 하나만 쓰임
        return int(value) in self._active


def _make_user(store: _Store) -> User:
    user = User(email="owner@test.com", password_hash="x")
    user.id = store.next_id()
    store.rows.setdefault(User, []).append(user)
    return user


def _make_strategy(store: _Store, user: User, status_: StrategyStatus) -> Strategy:
    s = Strategy(user_id=user.id, name="테스트 전략", config={}, status=status_)
    s.id = store.next_id()
    store.rows.setdefault(Strategy, []).append(s)
    return s


def _make_position(store: _Store, user: User, strategy: Strategy, qty: str) -> Position:
    pos = Position(
        user_id=user.id,
        strategy_id=strategy.id,
        symbol="005930",
        qty=Decimal(qty),
        avg_price=Decimal("1000"),
    )
    pos.id = store.next_id()
    store.rows.setdefault(Position, []).append(pos)
    return pos


@pytest.fixture
def store():
    return _Store()


async def test_delete_blocked_when_status_live(monkeypatch, store):
    """DB status=LIVE 이면 포지션/Redis 상태와 무관하게 409."""
    user = _make_user(store)
    s = _make_strategy(store, user, StrategyStatus.LIVE)
    db = make_session_factory(store)()
    monkeypatch.setattr(strategies_routes, "redis_client", _SismemberFakeRedis())

    with pytest.raises(HTTPException) as exc:
        await strategies_routes.delete_strategy(s.id, current=user, db=db)
    assert exc.value.status_code == 409
    assert "가동 중" in exc.value.detail
    # 삭제되지 않아야 한다.
    assert s in store.rows[Strategy]


async def test_delete_blocked_when_active_in_redis(monkeypatch, store):
    """DB status 는 안전(BACKTESTED)해도 엔진 활성 집합에 있으면 409."""
    user = _make_user(store)
    s = _make_strategy(store, user, StrategyStatus.BACKTESTED)
    db = make_session_factory(store)()
    monkeypatch.setattr(
        strategies_routes, "redis_client", _SismemberFakeRedis(active_ids={s.id})
    )

    with pytest.raises(HTTPException) as exc:
        await strategies_routes.delete_strategy(s.id, current=user, db=db)
    assert exc.value.status_code == 409
    assert "엔진에서 실행 중" in exc.value.detail
    assert s in store.rows[Strategy]


async def test_delete_blocked_when_open_position(monkeypatch, store):
    """가동 상태는 안전해도 qty>0 인 미청산 포지션이 있으면 409."""
    user = _make_user(store)
    s = _make_strategy(store, user, StrategyStatus.BACKTESTED)
    _make_position(store, user, s, "10")
    db = make_session_factory(store)()
    monkeypatch.setattr(strategies_routes, "redis_client", _SismemberFakeRedis())

    with pytest.raises(HTTPException) as exc:
        await strategies_routes.delete_strategy(s.id, current=user, db=db)
    assert exc.value.status_code == 409
    assert "미청산 포지션" in exc.value.detail
    assert s in store.rows[Strategy]


async def test_delete_succeeds_when_all_safe(monkeypatch, store):
    """가동 중도 아니고(BACKTESTED·Redis 미등록) 미청산 포지션도 없으면 정상 삭제(204)."""
    user = _make_user(store)
    s = _make_strategy(store, user, StrategyStatus.BACKTESTED)
    # qty=0 인 포지션은 미청산이 아니므로 삭제를 막지 않아야 한다.
    _make_position(store, user, s, "0")
    db = make_session_factory(store)()
    monkeypatch.setattr(strategies_routes, "redis_client", _SismemberFakeRedis())

    await strategies_routes.delete_strategy(s.id, current=user, db=db)
    assert s not in store.rows[Strategy]


async def test_delete_other_users_strategy_still_404(store):
    """다른 유저 소유 전략은 기존 _get_owned 가드가 그대로 404 를 낸다(회귀 확인)."""
    owner = _make_user(store)
    other = _make_user(store)
    s = _make_strategy(store, owner, StrategyStatus.BACKTESTED)
    db = make_session_factory(store)()

    with pytest.raises(HTTPException) as exc:
        await strategies_routes.delete_strategy(s.id, current=other, db=db)
    assert exc.value.status_code == 404
