"""GET /api/strategies/{id}/tracking 핸들러 검증(§6).

get_strategy_tracking 를 직접 호출하되, DB 는 엔티티별로 값을 돌려주는 경량 대역으로,
종가 패널은 monkeypatch 로 통제해 핸들러의 매칭·자본 해석·404·warning·응답 조립 로직을
검증한다. 실 DB·네트워크 불필요.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import app.api.routes.tracking as tr
from app.api.routes.tracking import get_strategy_tracking
from app.models import Backtest, Order, Strategy

KST = timezone.utc  # 대역 — executed_at 은 tz-aware 면 된다.
BDAYS = pd.bdate_range("2025-01-02", periods=5)


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)


class _FakeSession:
    """엔티티(column_descriptions[0]['entity'])별로 미리 주입한 값을 돌려주는 대역."""

    def __init__(self, *, strategy=None, orders=None, backtest=None):
        self._strategy = strategy
        self._orders = orders or []
        self._backtest = backtest

    def _entity(self, stmt):
        return stmt.column_descriptions[0]["entity"]

    async def scalar(self, stmt):
        ent = self._entity(stmt)
        if ent is Strategy:
            return self._strategy
        if ent is Backtest:
            return self._backtest
        return None

    async def scalars(self, stmt):
        ent = self._entity(stmt)
        if ent is Order:
            return _Scalars(self._orders)
        return _Scalars([])


def _order(side, symbol, execs):
    return SimpleNamespace(id=1, side=side, symbol=symbol, executions=execs)


def _exec(qty, price, day_idx, fee=0.0):
    return SimpleNamespace(
        filled_qty=qty,
        filled_price=price,
        fee=fee,
        executed_at=datetime(2025, 1, 2 + day_idx, 6, 0, tzinfo=timezone.utc),
    )


def _patch_panel(monkeypatch, panel):
    async def _fake(db, symbols, start, end):  # noqa: ARG001
        return panel

    monkeypatch.setattr(tr, "_build_close_panel", _fake)


def _bt(equity_curve, bt_id=7):
    return SimpleNamespace(
        id=bt_id, strategy_id=23, result={"equity_curve": equity_curve}
    )


def _eq_curve(vals):
    return [{"t": BDAYS[i].date().isoformat(), "v": v} for i, v in enumerate(vals)]


async def test_tracking_happy_path(monkeypatch):
    strat = SimpleNamespace(id=23, user_id=1, config={"cash": 1_000_000})
    order = _order("buy", "A", [_exec(10.0, 100.0, 0)])
    bt = _bt(_eq_curve([1_000_000.0, 1_010_000.0, 1_020_000.0]))
    db = _FakeSession(strategy=strat, orders=[order], backtest=bt)
    _patch_panel(monkeypatch, pd.DataFrame({"A": [100.0, 110.0, 120.0, 120.0, 120.0]}, index=BDAYS))
    current = SimpleNamespace(id=1, email="t@e.com")

    res = await get_strategy_tracking(23, current=current, db=db)

    assert res.strategy_id == 23
    assert res.backtest_id == 7
    assert res.initial_capital == 1_000_000.0
    assert len(res.realized_curve) >= 1
    assert res.realized_curve[0].v == pytest.approx(1_000_000.0)
    assert len(res.backtest_curve) == 3
    # 100 정규화 인덱스 첫 값.
    assert res.realized_index[0].v == pytest.approx(100.0)
    assert res.backtest_index[0].v == pytest.approx(100.0)
    assert res.metrics.n_common_days >= 2
    assert res.sample.n_executions == 1
    assert res.sample.n_symbols == 1
    assert res.warning is None


async def test_tracking_no_executions_404(monkeypatch):
    strat = SimpleNamespace(id=23, user_id=1, config={"cash": 1_000_000})
    db = _FakeSession(strategy=strat, orders=[], backtest=None)
    _patch_panel(monkeypatch, pd.DataFrame())
    current = SimpleNamespace(id=1, email="t@e.com")

    with pytest.raises(Exception) as exc:
        await get_strategy_tracking(23, current=current, db=db)
    assert getattr(exc.value, "status_code", None) == 404


async def test_tracking_no_backtest_returns_realized_only(monkeypatch):
    strat = SimpleNamespace(id=23, user_id=1, config={"cash": 1_000_000})
    order = _order("buy", "A", [_exec(10.0, 100.0, 0)])
    db = _FakeSession(strategy=strat, orders=[order], backtest=None)
    _patch_panel(monkeypatch, pd.DataFrame({"A": [100.0, 110.0]}, index=BDAYS[:2]))
    current = SimpleNamespace(id=1, email="t@e.com")

    res = await get_strategy_tracking(23, current=current, db=db)

    assert res.backtest_id is None
    assert res.backtest_curve == []
    assert res.warning is not None
    assert res.metrics.tracking_error is None
    assert len(res.realized_curve) >= 1


async def test_tracking_explicit_backtest_id_not_found_404(monkeypatch):
    strat = SimpleNamespace(id=23, user_id=1, config={"cash": 1_000_000})
    order = _order("buy", "A", [_exec(10.0, 100.0, 0)])
    # backtest=None 대역 → 지정 id 조회 실패.
    db = _FakeSession(strategy=strat, orders=[order], backtest=None)
    _patch_panel(monkeypatch, pd.DataFrame({"A": [100.0, 110.0]}, index=BDAYS[:2]))
    current = SimpleNamespace(id=1, email="t@e.com")

    with pytest.raises(Exception) as exc:
        await get_strategy_tracking(23, backtest_id=999, current=current, db=db)
    assert getattr(exc.value, "status_code", None) == 404


async def test_tracking_initial_capital_override(monkeypatch):
    strat = SimpleNamespace(id=23, user_id=1, config={"cash": 1_000_000})
    order = _order("buy", "A", [_exec(10.0, 100.0, 0)])
    db = _FakeSession(strategy=strat, orders=[order], backtest=None)
    _patch_panel(monkeypatch, pd.DataFrame({"A": [100.0, 110.0]}, index=BDAYS[:2]))
    current = SimpleNamespace(id=1, email="t@e.com")

    res = await get_strategy_tracking(23, initial_capital=5_000_000, current=current, db=db)
    assert res.initial_capital == 5_000_000.0
