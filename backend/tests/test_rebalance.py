"""리밸런싱 코어 순수함수 검증 — 목표비중·주문생성·발화시점."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from engine import rebalance
from engine.rebalance import (
    compute_rebalance_orders,
    compute_target_weights,
    is_rebalance_due,
)

KST = timezone(timedelta(hours=9))


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype="float64")


# ───────────────────── compute_target_weights ─────────────────────


def test_momentum_selects_top_n_equal_weight():
    cfg = {
        "universe": ["A", "B", "C"],
        "selection": {"method": "momentum", "lookback": 2, "top_n": 2},
    }
    history = {
        "A": _series([100, 100, 130]),  # +30%
        "B": _series([100, 100, 110]),  # +10%
        "C": _series([100, 100, 90]),   # -10%
        "X": _series([100, 100, 200]),  # universe 밖 — 무시
    }
    weights = compute_target_weights(history, cfg)
    assert set(weights) == {"A", "B"}
    assert weights["A"] == pytest.approx(0.5)
    assert weights["B"] == pytest.approx(0.5)


def test_momentum_excludes_insufficient_data():
    cfg = {
        "universe": ["A", "B"],
        "selection": {"method": "momentum", "lookback": 5, "top_n": 2},
    }
    history = {"A": _series([1, 2, 3, 4, 5, 6]), "B": _series([1, 2])}  # B 데이터 부족
    weights = compute_target_weights(history, cfg)
    assert set(weights) == {"A"}
    assert weights["A"] == pytest.approx(1.0)


def test_all_method_equal_weight():
    cfg = {"universe": ["A", "B", "C"], "selection": {"method": "all"}}
    history = {s: _series([1, 2, 3]) for s in ["A", "B", "C"]}
    weights = compute_target_weights(history, cfg)
    assert set(weights) == {"A", "B", "C"}
    assert all(w == pytest.approx(1 / 3) for w in weights.values())


# ───────────────────── compute_rebalance_orders ─────────────────────


def test_orders_from_empty_positions():
    orders = compute_rebalance_orders(
        targets={"A": 0.5, "B": 0.5},
        positions={},
        prices={"A": 100.0, "B": 200.0},
        capital=1000.0,
        drift_band=0.0,
    )
    assert ("A", "buy", 5) in orders   # floor(0.5*1000/100)
    assert ("B", "buy", 2) in orders   # floor(0.5*1000/200)
    assert all(side == "buy" for _, side, _ in orders)


def test_drift_band_skips_within_tolerance():
    # A 가 이미 정확히 목표 비중(0.5) → 매매 없음
    orders = compute_rebalance_orders(
        targets={"A": 0.5},
        positions={"A": 5},
        prices={"A": 100.0},
        capital=1000.0,
        drift_band=0.05,
    )
    assert orders == []


def test_dropped_symbol_fully_sold_and_sells_precede_buys():
    orders = compute_rebalance_orders(
        targets={"A": 1.0},          # B 는 선정 제외 → 목표 0
        positions={"B": 5},
        prices={"A": 100.0, "B": 100.0},
        capital=1000.0,
        drift_band=0.0,
    )
    assert orders[0] == ("B", "sell", 5)   # 매도가 먼저
    assert ("A", "buy", 10) in orders


def test_missing_price_symbol_skipped():
    orders = compute_rebalance_orders(
        targets={"A": 1.0},
        positions={},
        prices={},  # 현재가 없음
        capital=1000.0,
        drift_band=0.0,
    )
    assert orders == []


# ───────────────────── is_rebalance_due ─────────────────────


@pytest.fixture
def market_open(monkeypatch):
    """is_market_open 을 True 로 고정해 시간·주기 로직만 검증한다."""
    monkeypatch.setattr(rebalance, "is_market_open", lambda now=None: True)


def test_not_due_when_market_closed(monkeypatch):
    monkeypatch.setattr(rebalance, "is_market_open", lambda now=None: False)
    cfg = {"cadence": "daily", "rebalance_time": "09:00"}
    now = datetime(2026, 6, 24, 14, 0, tzinfo=KST)
    assert is_rebalance_due(cfg, None, now) is False


def test_not_due_before_time(market_open):
    cfg = {"cadence": "daily", "rebalance_time": "14:30"}
    now = datetime(2026, 6, 24, 14, 0, tzinfo=KST)
    assert is_rebalance_due(cfg, None, now) is False


def test_daily_due_after_time(market_open):
    cfg = {"cadence": "daily", "rebalance_time": "14:30"}
    now = datetime(2026, 6, 24, 14, 30, tzinfo=KST)
    assert is_rebalance_due(cfg, None, now) is True


def test_daily_not_due_same_day_twice(market_open):
    cfg = {"cadence": "daily", "rebalance_time": "14:30"}
    last = datetime(2026, 6, 24, 14, 31, tzinfo=KST)
    now = datetime(2026, 6, 24, 15, 0, tzinfo=KST)
    assert is_rebalance_due(cfg, last, now) is False


def test_weekly_waits_for_target_weekday(market_open):
    now = datetime(2026, 6, 24, 14, 30, tzinfo=KST)  # weekday 계산은 표준 라이브러리
    target = (now.weekday() + 1) % 5  # 오늘보다 뒤 요일로 설정
    cfg = {"cadence": "weekly", "rebalance_time": "14:30", "rebalance_weekday": target}
    if target > now.weekday():
        assert is_rebalance_due(cfg, None, now) is False


def test_monthly_due_on_new_month(market_open):
    cfg = {"cadence": "monthly", "rebalance_time": "14:30", "rebalance_dom": 1}
    last = datetime(2026, 5, 4, 14, 30, tzinfo=KST)
    now = datetime(2026, 6, 24, 14, 30, tzinfo=KST)
    assert is_rebalance_due(cfg, last, now) is True


def test_monthly_not_due_before_dom(market_open):
    cfg = {"cadence": "monthly", "rebalance_time": "14:30", "rebalance_dom": 15}
    now = datetime(2026, 6, 10, 14, 30, tzinfo=KST)
    assert is_rebalance_due(cfg, None, now) is False
