"""GET /api/engine/strategies/health 의 MDD 킬스위치 상태 노출 검증(§23 후속).

engine.rebalance_runner 가 rebalance:mdd:{id} 에 저장하는 고점(HWM)·발동 여부·발동일이
이전엔 어떤 API 도 읽지 않아, MDD 킬(전량 청산)이 발동해도 그 순간의 alert 토스트
외엔 나중에 "지금 청산 상태로 재가동 대기 중"임을 확인할 방법이 없었다. db.scalars
는 Strategy 목록을 직접 주입하는 최소 대역, redis 는 conftest 의 FakeRedis 를 재사용한다.
"""
import json

from app.api.routes import engine as engine_routes
from app.core.channels import engine_health_key, mdd_state_key
from app.models import Strategy, StrategyStatus, User
from tests.conftest import FakeRedis


def _strategy(id_: int, name: str = "테스트전략") -> Strategy:
    s = Strategy(user_id=1, name=name, config={}, status=StrategyStatus.LIVE)
    s.id = id_
    return s


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeDb:
    def __init__(self, strategies):
        self._strategies = strategies

    async def scalars(self, _stmt):
        return _FakeScalars(self._strategies)


_USER = User(email="t@example.com", password_hash="x")
_USER.id = 1


async def test_mdd_killed_state_exposed(monkeypatch):
    redis = FakeRedis()
    redis.store[mdd_state_key(1)] = json.dumps(
        {"hwm": 12_000_000.0, "killed": True, "kill_date": "2026-06-01"}
    )
    monkeypatch.setattr(engine_routes, "redis_client", redis)
    db = _FakeDb([_strategy(1)])

    result = await engine_routes.strategies_health(current=_USER, db=db)

    row = result["strategies"][0]
    assert row["mdd_killed"] is True
    assert row["mdd_kill_date"] == "2026-06-01"
    assert row["mdd_hwm"] == 12_000_000.0


async def test_mdd_state_defaults_when_no_record(monkeypatch):
    """아직 한 번도 평가되지 않았거나(신규 start) Redis 유실 시 killed=False 로 안전측 기본값."""
    redis = FakeRedis()
    monkeypatch.setattr(engine_routes, "redis_client", redis)
    db = _FakeDb([_strategy(1)])

    result = await engine_routes.strategies_health(current=_USER, db=db)

    row = result["strategies"][0]
    assert row["mdd_killed"] is False
    assert row["mdd_kill_date"] is None
    assert row["mdd_hwm"] is None


async def test_mdd_not_killed_but_hwm_tracked(monkeypatch):
    redis = FakeRedis()
    redis.store[mdd_state_key(1)] = json.dumps(
        {"hwm": 11_000_000.0, "killed": False, "kill_date": None}
    )
    monkeypatch.setattr(engine_routes, "redis_client", redis)
    db = _FakeDb([_strategy(1)])

    result = await engine_routes.strategies_health(current=_USER, db=db)

    row = result["strategies"][0]
    assert row["mdd_killed"] is False
    assert row["mdd_hwm"] == 11_000_000.0


async def test_health_and_mdd_state_are_independent(monkeypatch):
    """러너 헬스(engine:health)와 MDD 상태(rebalance:mdd)는 서로 다른 키 — 한쪽만 있어도 무관하게 동작."""
    redis = FakeRedis()
    redis.store[engine_health_key(1)] = json.dumps(
        {"consecutive_failures": 5, "last_error": "타임아웃"}
    )
    monkeypatch.setattr(engine_routes, "redis_client", redis)
    db = _FakeDb([_strategy(1)])

    result = await engine_routes.strategies_health(current=_USER, db=db)

    row = result["strategies"][0]
    assert row["consecutive_failures"] == 5
    assert row["healthy"] is False
    assert row["mdd_killed"] is False  # MDD 레코드는 없음
