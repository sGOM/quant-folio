"""엔진 종단 통합 테스트 — 시세 수신 → 신호(목표비중) → 리스크 레이어 → 주문 →
체결 기록 전체 경로를 모의 브로커·모의 Redis·인메모리 DB 로 구동해 검증한다.

러너 단위 테스트(test_engine_health)와 코어 순수함수 테스트(test_rebalance)는 있으나,
"실행 경로 전체"를 하나로 꿰어 도는 회귀 안전망이 없었다. 이 테스트는 직전 PR들
(실전전환게이트·슬리피지 캘리브레이션·다중전략 포지션분리)이 종단에서 깨지지 않았는지
지킨다.

외부 I/O 는 전부 대역한다(FakeBroker=증권사, FakeRedis=락·상태, FakeDB=DB, 팩터
스코어는 monkeypatch). 네트워크 접근은 없다.

경로: RebalanceRunner._rebalance_once
  → _compute_plan(후보풀→목표비중→_apply_risk_caps 집중 한도)
  → compute_rebalance_orders(드리프트 밴드)
  → _execute_orders(check_daily_loss_limit·evaluate_buy)
  → BaseRunner._place → executor.execute_signal(멱등성키→DB중복확인→broker.place_order
      →체결조회→record_fill: Order/Execution/Position 기록, strategy_id 스코프).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.models import Execution, Order, OrderSide, OrderStatus, Position
from app.services.market import KST
from engine import rebalance_runner
from engine.rebalance_runner import RebalanceRunner
from tests.conftest import FakeBroker, FakeRedis, _Store, make_session_factory

# 3종목 고정 유니버스·시세(원). 팩터 스코어는 아래에서 주입한다.
_UNIVERSE = ["005930", "000660", "035720"]
_PRICES = {"005930": 70_000, "000660": 120_000, "035720": 50_000}
_CAPITAL = 10_000_000
_NOW = datetime(2025, 4, 15, 14, 30, tzinfo=KST)


def _patch_scores(monkeypatch, scores: dict[str, float]) -> None:
    """compute_universe_scores 를 고정 점수 dict 로 대체한다(팩터 조회 네트워크 제거)."""
    def _fake(universe, as_of, factor_weights, neutralize="none", financial_period="annual"):  # noqa: ARG001
        return {sym: scores[sym] for sym in universe if sym in scores}

    monkeypatch.setattr(rebalance_runner, "compute_universe_scores", _fake)


def _make_runner(
    store: _Store,
    redis: FakeRedis,
    broker: FakeBroker,
    *,
    strategy_id: int = 101,
    user_id: int = 1,
    weighting: str = "equal",
    risk_layer: dict | None = None,
    top_n: int = 3,
) -> RebalanceRunner:
    """FakeBroker·FakeRedis·인메모리 DB 로 배선된 리밸런싱 러너를 만든다(적재 생략)."""
    r = RebalanceRunner(strategy_id=strategy_id, redis=redis)
    r._user_id = user_id
    r._broker = broker
    r._cfg = {
        "universe": list(_UNIVERSE),
        "capital": _CAPITAL,
        "cadence": "monthly",
        "drift_band_pct": 0.0,  # 밴드 0 → 목표와 다르면 무조건 체결(신규 편입 검증 단순화)
        "weighting": weighting,
        "selection": {
            "method": "score",  # 스코어 방식 → 종가 히스토리 시딩 불필요(네트워크 제거)
            "top_n": top_n,
            "factor_weights": {"value": 1.0},
        },
    }
    if risk_layer is not None:
        r._cfg["risk_layer"] = risk_layer
    return r


async def test_rebalance_applies_concentration_cap_to_target_weights(monkeypatch):
    """(a) 리스크 레이어: 한 종목에 몰린 목표비중이 집중 한도(max_position_pct) 이하로 클립.

    weighting='score' 순위가중은 3종목에 0.5/0.333/0.167 를 배분한다. max_position_pct=0.4
    를 걸면 _apply_risk_caps(_cap_position_weights)가 최상위 0.5 를 0.4 로 눌러 초과분을
    나머지에 재분배한다. 이 클립이 실제 _compute_plan 파이프라인에서 적용됨을 확인한다.
    """
    store = _Store()
    r = _make_runner(
        store, FakeRedis(), FakeBroker(_PRICES),
        weighting="score", risk_layer={"max_position_pct": 0.4},
    )
    # 점수 순위: 005930 > 000660 > 035720 → 순위가중 0.5/0.333/0.167.
    _patch_scores(monkeypatch, {"005930": 3.0, "000660": 2.0, "035720": 1.0})
    monkeypatch.setattr(rebalance_runner, "AsyncSessionLocal", make_session_factory(store))

    _pool, targets = await r._compute_plan(_NOW.date(), risk_off=False)

    assert set(targets) == set(_UNIVERSE)
    # 캡 적용 전 최상위는 0.5 였으나 0.4 로 클립되어야 한다(집중 한도 하드 적용).
    assert targets["005930"] == pytest.approx(0.4, abs=1e-9)
    assert all(w <= 0.4 + 1e-9 for w in targets.values())
    # 초과분(0.1)이 나머지로 재분배되어 총합은 보존(≈1.0).
    assert sum(targets.values()) == pytest.approx(1.0, abs=1e-9)


async def test_rebalance_end_to_end_creates_order_execution_position(monkeypatch):
    """(b) 산출 주문이 실행되어 Order·Execution·Position 이 정확히 생성(strategy_id 스코프)."""
    store = _Store()
    broker = FakeBroker(_PRICES)
    r = _make_runner(store, FakeRedis(), broker, strategy_id=101, user_id=1)
    _patch_scores(monkeypatch, {"005930": 3.0, "000660": 2.0, "035720": 1.0})
    monkeypatch.setattr(rebalance_runner, "AsyncSessionLocal", make_session_factory(store))

    await r._rebalance_once(_NOW, risk_off=False)

    orders = store.all(Order)
    execs = store.all(Execution)
    positions = store.all(Position)

    # 3종목 신규 매수 → 주문·체결·포지션 각 3건.
    assert len(orders) == 3
    assert len(execs) == 3
    assert len(positions) == 3
    assert {o.symbol for o in orders} == set(_UNIVERSE)
    assert all(o.side == OrderSide.BUY for o in orders)
    assert all(o.status == OrderStatus.FILLED for o in orders)
    # 감사 로그(주문 사유)가 채워져 있어야 한다.
    assert all(o.reason for o in orders)

    # 포지션·체결이 전략 101 로 스코프되어 기록(§3 강화).
    assert all(p.strategy_id == 101 and p.user_id == 1 for p in positions)
    assert all(e.strategy_id == 101 for e in execs)

    # 수량·평단이 목표비중(등비중 1/3)·시세와 정합. qty = floor(0.333*capital/price).
    weight = 1.0 / 3
    for p in positions:
        expected_qty = int(weight * _CAPITAL // _PRICES[p.symbol])
        assert p.qty == Decimal(expected_qty)
        assert p.avg_price == Decimal(_PRICES[p.symbol])  # FakeBroker 는 시세 그대로 체결


async def test_rebalance_is_idempotent_for_same_bar(monkeypatch):
    """(c) 멱등성: 동일 bar_ts(같은 거래일·태그)로 재실행해도 중복 주문이 생기지 않는다.

    첫 실행으로 주문/체결/포지션이 생긴 뒤 포지션만 비워(재-편입 주문을 강제 유발) 같은
    now 로 다시 실행한다. executor 의 멱등성 게이트(idempotency_key DB 중복확인)가 재-편입
    주문을 전부 차단해 Order/Execution 이 늘지 않아야 한다.
    """
    store = _Store()
    r = _make_runner(store, FakeRedis(), FakeBroker(_PRICES), strategy_id=101, user_id=1)
    _patch_scores(monkeypatch, {"005930": 3.0, "000660": 2.0, "035720": 1.0})
    monkeypatch.setattr(rebalance_runner, "AsyncSessionLocal", make_session_factory(store))

    await r._rebalance_once(_NOW, risk_off=False)
    assert len(store.all(Order)) == 3
    assert len(store.all(Execution)) == 3

    # 포지션을 비워 동일 종목의 신규 편입 주문을 강제로 다시 산출하게 만든다.
    store.rows[Position] = []
    await r._rebalance_once(_NOW, risk_off=False)  # 같은 now → 같은 bar_ts → 같은 멱등성키

    # 멱등성 게이트가 중복을 전부 차단 → 주문·체결 수 불변, 포지션도 재생성 안 됨.
    assert len(store.all(Order)) == 3
    assert len(store.all(Execution)) == 3
    assert store.all(Position) == []
    # 멱등성 키가 종목별 유일(중복 없음).
    keys = [o.idempotency_key for o in store.all(Order)]
    assert len(keys) == len(set(keys))


async def test_two_strategies_same_symbol_keep_positions_separate(monkeypatch):
    """(d) §3 종단 재확인: 두 전략이 겹치는 종목을 다뤄도 Position 이 전략별로 분리 기록.

    같은 사용자(user_id=1)의 전략 101·202 가 동일 유니버스를 매수한다. 겹치는 종목
    005930 은 (1,101,005930)·(1,202,005930) 두 행으로 독립 기록되어야 한다(포지션 혼선 없음).
    """
    store = _Store()
    redis = FakeRedis()  # (user, symbol) 포지션 락을 두 러너가 공유(순차 실행이라 경합 없음)
    _patch_scores(monkeypatch, {"005930": 3.0, "000660": 2.0, "035720": 1.0})
    monkeypatch.setattr(rebalance_runner, "AsyncSessionLocal", make_session_factory(store))

    r_a = _make_runner(store, redis, FakeBroker(_PRICES), strategy_id=101, user_id=1)
    r_b = _make_runner(store, redis, FakeBroker(_PRICES), strategy_id=202, user_id=1)

    await r_a._rebalance_once(_NOW, risk_off=False)
    await r_b._rebalance_once(_NOW, risk_off=False)

    positions = store.all(Position)
    # 두 전략 × 3종목 = 6 포지션 행(겹쳐도 분리).
    assert len(positions) == 6

    def _pos(strategy_id, symbol):
        for p in positions:
            if p.strategy_id == strategy_id and p.symbol == symbol:
                return p
        return None

    a = _pos(101, "005930")
    b = _pos(202, "005930")
    assert a is not None and b is not None
    assert a is not b  # 별개 행
    # 두 전략 모두 같은 종목을 독립 수량·평단으로 보유(등비중이라 수량 동일하지만 행은 분리).
    expected_qty = int((1.0 / 3) * _CAPITAL // _PRICES["005930"])
    assert a.qty == Decimal(expected_qty) and b.qty == Decimal(expected_qty)
    assert a.avg_price == b.avg_price == Decimal(_PRICES["005930"])

    # 체결도 전략별 strategy_id 로 비정규화 기록.
    assert {e.strategy_id for e in store.all(Execution)} == {101, 202}
