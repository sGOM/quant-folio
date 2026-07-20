"""engine/runner.py::StrategyRunner._tick_once 핵심 매매 로직 검증(§신규발굴 6차 1위).

단일종목 손절·익절·트레일링 실거래 매매 루프는 이제까지 테스트가 전무했다
(RebalanceRunner 는 test_engine_e2e.py 로 종단 검증돼 있으나, 개별 종목을
손절%/익절%/트레일링으로 청산하는 StrategyRunner 만 사각지대였다). 신호
평가보다 손절이 우선하는 순서, 리스크 한도(RiskLimit) 손절과 전략 config
청산(손절%/익절%/트레일링)의 관계, 트레일링 고점의 Redis 캐시 갱신·정리,
포지션 락 경합 시 tick 스킵, 일일 손실 한도 초과 시 신규 진입 차단 — 이 핵심
계약들을 test_engine_e2e.py 와 동일한 인메모리 DB(FakeDB/_Store)·FakeBroker·
FakeRedis 로 실제 risk.py/executor.py 를 그대로 태워 종단으로 검증한다.
신호 자체(sma_crossover 등)의 정확성은 backtest/signals 쪽 테스트 영역이므로,
`latest_signal` 만 고정값으로 대역화해 러너의 오케스트레이션 로직에 집중한다.
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from app.core.channels import position_lock_key
from app.models import Order, OrderSide, Position, RiskLimit
from engine import runner as runner_mod
from engine.runner import StrategyRunner
from tests.conftest import FakeBroker, FakeRedis, _Store, make_session_factory

_SYMBOL = "005930"
_STRATEGY_ID = 101
_USER_ID = 1


def _series() -> pd.Series:
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=25)
    return pd.Series([70_000.0] * 25, index=idx)


def _make_runner(
    store: _Store, redis: FakeRedis, broker: FakeBroker, *, cfg: dict | None = None,
) -> StrategyRunner:
    r = StrategyRunner(strategy_id=_STRATEGY_ID, redis=redis)
    r._user_id = _USER_ID
    r._broker = broker
    r._symbol = _SYMBOL
    r._ohlc = False
    r._series = _series()
    r._cfg = cfg or {"type": "sma_crossover", "fast": 5, "slow": 20, "cash": 10_000_000}
    return r


def _patch_common(monkeypatch, store: _Store, *, signal: str | None):
    monkeypatch.setattr(runner_mod, "AsyncSessionLocal", make_session_factory(store))
    monkeypatch.setattr(runner_mod, "is_market_open_async", _async_true)
    monkeypatch.setattr(runner_mod, "latest_signal", lambda series, cfg: signal)


async def _async_true() -> bool:
    return True


def _position(*, avg_price: Decimal, qty: int = 10, strategy_id: int | None = _STRATEGY_ID) -> Position:
    return Position(
        user_id=_USER_ID, strategy_id=strategy_id, symbol=_SYMBOL,
        qty=Decimal(qty), avg_price=avg_price,
    )


async def test_buy_signal_places_order_when_no_holding(monkeypatch):
    store = _Store()
    _patch_common(monkeypatch, store, signal="buy")
    r = _make_runner(store, FakeRedis(), FakeBroker({_SYMBOL: 70_000}))

    await r._tick_once()

    orders = store.all(Order)
    assert len(orders) == 1
    assert orders[0].side == OrderSide.BUY
    positions = store.all(Position)
    assert len(positions) == 1 and positions[0].qty > 0


async def test_daily_loss_limit_blocks_new_buy_entry(monkeypatch):
    """계좌 공통 한도(strategy_id=NULL) 초과 시 신규 매수 진입이 차단된다."""
    store = _Store()
    _patch_common(monkeypatch, store, signal="buy")
    store.rows[RiskLimit] = [
        RiskLimit(user_id=_USER_ID, strategy_id=None, daily_loss_limit=Decimal("1000000")),
    ]
    # 비귀속(strategy_id=None) 평가손실 -3,000,000 → 계좌 게이트 즉시 초과.
    store.rows[Position] = [_position(avg_price=Decimal("100000"), qty=100, strategy_id=None)]
    r = _make_runner(store, FakeRedis(), FakeBroker({_SYMBOL: 70_000}))

    await r._tick_once()

    assert store.all(Order) == []


async def test_risk_limit_stop_loss_takes_priority_over_buy_signal(monkeypatch):
    """RiskLimit.stop_loss_pct 손절은 이번 틱의 매수 신호보다 우선 처리된다."""
    store = _Store()
    _patch_common(monkeypatch, store, signal="buy")  # 손절이 우선해야 하므로 매수 신호는 무시돼야 함
    store.rows[RiskLimit] = [
        RiskLimit(user_id=_USER_ID, strategy_id=_STRATEGY_ID, stop_loss_pct=Decimal("0.1")),
    ]
    store.rows[Position] = [_position(avg_price=Decimal("100000"), qty=10)]
    r = _make_runner(store, FakeRedis(), FakeBroker({_SYMBOL: 80_000}))  # -20% 하락

    await r._tick_once()

    orders = store.all(Order)
    assert len(orders) == 1
    assert orders[0].side == OrderSide.SELL
    assert "리스크 손절" in orders[0].reason


async def test_config_take_profit_exits_position(monkeypatch):
    store = _Store()
    _patch_common(monkeypatch, store, signal=None)
    store.rows[Position] = [_position(avg_price=Decimal("70000"), qty=10)]
    r = _make_runner(
        store, FakeRedis(), FakeBroker({_SYMBOL: 76_000}),
        cfg={"type": "sma_crossover", "fast": 5, "slow": 20, "take_profit_pct": 0.05},
    )

    await r._tick_once()

    orders = store.all(Order)
    assert len(orders) == 1
    assert orders[0].side == OrderSide.SELL
    assert "익절" in orders[0].reason


async def test_config_stop_loss_exits_position_without_risk_limit(monkeypatch):
    """RiskLimit 이 없어도 전략 config 의 stop_loss_pct 는 독립적으로 청산을 발동한다."""
    store = _Store()
    _patch_common(monkeypatch, store, signal=None)
    store.rows[Position] = [_position(avg_price=Decimal("70000"), qty=10)]
    r = _make_runner(
        store, FakeRedis(), FakeBroker({_SYMBOL: 65_000}),
        cfg={"type": "sma_crossover", "fast": 5, "slow": 20, "stop_loss_pct": 0.05},
    )

    await r._tick_once()

    orders = store.all(Order)
    assert len(orders) == 1
    assert orders[0].side == OrderSide.SELL
    assert "손절" in orders[0].reason


async def test_trailing_stop_tracks_peak_then_exits_on_drawdown(monkeypatch):
    store = _Store()
    _patch_common(monkeypatch, store, signal=None)
    store.rows[Position] = [_position(avg_price=Decimal("70000"), qty=10)]
    redis = FakeRedis()
    broker = FakeBroker({_SYMBOL: 80_000})
    cfg = {"type": "sma_crossover", "fast": 5, "slow": 20, "trailing_stop_pct": 0.05}
    r = _make_runner(store, redis, broker, cfg=cfg)

    # 1틱: 고점 80,000 갱신, 하락폭 0% → 청산 없음.
    await r._tick_once()
    assert store.all(Order) == []
    assert await redis.get(r._trail_key()) == "80000"

    # 2틱: 75,000 은 고점 대비 -6.25% → 트레일링 기준 -5% 초과 → 청산.
    broker._quotes[_SYMBOL] = Decimal("75000")
    await r._tick_once()

    orders = store.all(Order)
    assert len(orders) == 1
    assert orders[0].side == OrderSide.SELL
    assert "트레일링" in orders[0].reason


async def test_trail_cache_cleared_when_no_holding(monkeypatch):
    store = _Store()
    _patch_common(monkeypatch, store, signal=None)
    redis = FakeRedis()
    r = _make_runner(store, redis, FakeBroker({_SYMBOL: 70_000}))
    await redis.set(r._trail_key(), "99999")  # 이전 보유의 잔여 트레일링 캐시

    await r._tick_once()

    assert await redis.get(r._trail_key()) is None


async def test_position_lock_contention_skips_tick(monkeypatch):
    """다른 프로세스가 이미 (user, symbol) 락을 쥐고 있으면 이번 tick 은 아무 것도 하지 않는다."""
    store = _Store()
    _patch_common(monkeypatch, store, signal="buy")
    redis = FakeRedis()
    await redis.set(position_lock_key(_USER_ID, _SYMBOL), "1")
    r = _make_runner(store, redis, FakeBroker({_SYMBOL: 70_000}))

    await r._tick_once()

    assert store.all(Order) == []


async def test_sell_signal_liquidates_full_holding(monkeypatch):
    store = _Store()
    _patch_common(monkeypatch, store, signal="sell")
    store.rows[Position] = [_position(avg_price=Decimal("70000"), qty=10)]
    r = _make_runner(store, FakeRedis(), FakeBroker({_SYMBOL: 71_000}))

    await r._tick_once()

    orders = store.all(Order)
    assert len(orders) == 1
    assert orders[0].side == OrderSide.SELL
