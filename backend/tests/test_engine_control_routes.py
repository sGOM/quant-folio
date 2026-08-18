"""app/api/routes/engine.py start_strategy/stop_strategy 검증(§신규발굴 12차 2위).

실거래 진입/중지를 트리거하는 라우트인데 참조 테스트가 없었다(test_engine_health_route.py
는 strategies_health 만 다룸). 핵심 계약: (1) 자격증명 체크가 DB 상태 변경·Redis
ENGINE_CONTROL_CHANNEL 발행보다 먼저 실행되어 자격증명 없이는 아무 부수효과도
남기지 않는지(순서 회귀 방지), (2) _get_owned 의 소유권 체크로 타인 전략에
대한 start/stop 이 차단되는지(IDOR 방지), (3) 정상 케이스에서 DB 갱신 + Redis
발행이 함께 일어나는지. DB 는 conftest.FakeDB/_Store, Redis 는 conftest.FakeRedis 재사용.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import app.api.routes.engine as engine_routes
from app.core.channels import ENGINE_CONTROL_CHANNEL
from app.models import Strategy, StrategyStatus, User
from tests.conftest import FakeDB, FakeRedis, _Store


def _make_db(*strategies: Strategy) -> FakeDB:
    store = _Store()
    for s in strategies:
        store.rows.setdefault(Strategy, []).append(s)
    return FakeDB(store)


def _strategy(id_: int, user_id: int, status: StrategyStatus = StrategyStatus.BACKTESTED) -> Strategy:
    s = Strategy(user_id=user_id, name="테스트전략", config={}, status=status)
    s.id = id_
    return s


def _user(id_: int = 1) -> User:
    u = User(email="owner@example.com", password_hash="x")
    u.id = id_
    return u


# ─────────────────────────── 자격증명 없음 — 순서 회귀 방지 ───────────────────────────


async def test_start_without_credentials_returns_400_and_touches_nothing(monkeypatch):
    """자격증명 체크가 DB 커밋·Redis 발행보다 먼저 실행돼야 한다(회귀 시 무자격 start 발행 위험).

    .env 폴백 자격증명이 실제로 설정된 환경(secrets 마운트)에서도 "자격증명 없음"을
    확정적으로 재현하기 위해 user_has_credentials 자체를 False 로 고정한다.
    """
    redis = FakeRedis()
    monkeypatch.setattr(engine_routes, "redis_client", redis)
    monkeypatch.setattr(engine_routes, "user_has_credentials", lambda u: False)
    strategy = _strategy(1, user_id=1, status=StrategyStatus.BACKTESTED)
    db = _make_db(strategy)
    user = _user(1)

    with pytest.raises(HTTPException) as exc_info:
        await engine_routes.start_strategy(1, current=user, db=db)

    assert exc_info.value.status_code == 400
    assert strategy.status == StrategyStatus.BACKTESTED  # DB 상태 변경 없음
    assert redis.published == []  # Redis 발행 없음


# ─────────────────────────── 소유권(IDOR) 방지 ───────────────────────────


async def test_start_rejects_strategy_owned_by_other_user(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(engine_routes, "redis_client", redis)
    monkeypatch.setattr(engine_routes, "user_has_credentials", lambda u: True)
    strategy = _strategy(1, user_id=2)  # 다른 사용자 소유
    db = _make_db(strategy)
    user = _user(1)

    with pytest.raises(HTTPException) as exc_info:
        await engine_routes.start_strategy(1, current=user, db=db)

    assert exc_info.value.status_code == 404
    assert strategy.status == StrategyStatus.BACKTESTED
    assert redis.published == []


async def test_stop_rejects_strategy_owned_by_other_user(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(engine_routes, "redis_client", redis)
    strategy = _strategy(1, user_id=2, status=StrategyStatus.LIVE)
    db = _make_db(strategy)
    user = _user(1)

    with pytest.raises(HTTPException) as exc_info:
        await engine_routes.stop_strategy(1, current=user, db=db)

    assert exc_info.value.status_code == 404
    assert strategy.status == StrategyStatus.LIVE
    assert redis.published == []


# ─────────────────────────── 정상 케이스 ───────────────────────────


async def test_start_updates_db_and_publishes_control_message(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(engine_routes, "redis_client", redis)
    monkeypatch.setattr(engine_routes, "user_has_credentials", lambda u: True)
    strategy = _strategy(1, user_id=1, status=StrategyStatus.BACKTESTED)
    db = _make_db(strategy)
    user = _user(1)

    result = await engine_routes.start_strategy(1, current=user, db=db)

    assert result == {"status": "live"}
    assert strategy.status == StrategyStatus.LIVE
    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    assert channel == ENGINE_CONTROL_CHANNEL
    assert json.loads(payload) == {"action": "start", "strategy_id": 1}


async def test_stop_updates_db_and_publishes_control_message(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(engine_routes, "redis_client", redis)
    strategy = _strategy(1, user_id=1, status=StrategyStatus.LIVE)
    db = _make_db(strategy)
    user = _user(1)

    result = await engine_routes.stop_strategy(1, current=user, db=db)

    assert result == {"status": "stopped"}
    assert strategy.status == StrategyStatus.BACKTESTED
    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    assert channel == ENGINE_CONTROL_CHANNEL
    assert json.loads(payload) == {"action": "stop", "strategy_id": 1}
