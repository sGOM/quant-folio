"""실측(모의투자) NAV 재구성·백테스트 괴리 지표(§6) 순수함수 검증.

reconstruct_realized_curve 의 현금·보유수량 재생과 시가평가, compute_tracking 의 정규화
인덱스·트래킹 에러·누적 괴리 계산을 mock 체결/종가 패널로 검증한다. DB·네트워크 불필요.
"""
from __future__ import annotations

import math

import pandas as pd

from app.services.backtest.tracking import (
    compute_tracking,
    reconstruct_realized_curve,
)

BDAYS = pd.bdate_range("2025-01-02", periods=10)


def _panel(seq: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(seq, index=BDAYS)


def _exec(symbol, side, qty, price, day_idx, fee=0.0):
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "fee": fee,
        "date": BDAYS[day_idx],
    }


# ─────────────────────────── NAV 재구성 ───────────────────────────


def test_reconstruct_buy_and_hold_nav():
    # 초기자본 100만, day0 에 A 10주 @100 매수(수수료 0). 종가 100→110.
    panel = _panel({"A": [100.0, 110.0] + [110.0] * 8})
    execs = [_exec("A", "buy", 10.0, 100.0, 0)]
    curve, notes = reconstruct_realized_curve(execs, panel, initial_capital=1_000_000.0)

    assert notes == []
    assert curve[0]["t"] == "2025-01-02"
    # day0: cash=1,000,000-1,000=999,000 + 10*100=1,000 → 1,000,000
    assert math.isclose(curve[0]["v"], 1_000_000.0, rel_tol=0, abs_tol=1e-6)
    # day1: 999,000 + 10*110=1,100 → 1,000,100 (평가익 +100)
    assert math.isclose(curve[1]["v"], 1_000_100.0, rel_tol=0, abs_tol=1e-6)


def test_reconstruct_fee_reduces_cash():
    panel = _panel({"A": [100.0] * 10})
    execs = [_exec("A", "buy", 10.0, 100.0, 0, fee=150.0)]
    curve, _ = reconstruct_realized_curve(execs, panel, initial_capital=1_000_000.0)
    # 수수료 150 만큼 NAV 손실.
    assert math.isclose(curve[0]["v"], 999_850.0, abs_tol=1e-6)


def test_reconstruct_sell_realizes_cash():
    # day0 매수 10주 @100, day2 매도 10주 @120. 이후 전액 현금.
    panel = _panel({"A": [100.0, 100.0, 120.0] + [120.0] * 7})
    execs = [
        _exec("A", "buy", 10.0, 100.0, 0),
        _exec("A", "sell", 10.0, 120.0, 2),
    ]
    curve, _ = reconstruct_realized_curve(execs, panel, initial_capital=1_000_000.0)
    # day2 매도 후: cash = 999,000 + 1,200 = 1,000,200, 보유 0 → NAV 1,000,200.
    assert math.isclose(curve[2]["v"], 1_000_200.0, abs_tol=1e-6)
    # day3 이후에도 전액 현금 유지.
    assert math.isclose(curve[3]["v"], 1_000_200.0, abs_tol=1e-6)


def test_reconstruct_missing_price_symbol_notes():
    # 패널에 B 종가 없음 → 경고, B 보유는 0 평가.
    panel = _panel({"A": [100.0] * 10})
    execs = [_exec("B", "buy", 10.0, 100.0, 0)]
    curve, notes = reconstruct_realized_curve(execs, panel, initial_capital=1_000_000.0)
    assert any("B" in n for n in notes)
    # 현금만: 1,000,000 - 1,000 = 999,000 (B 는 0 평가).
    assert math.isclose(curve[0]["v"], 999_000.0, abs_tol=1e-6)


def test_reconstruct_empty_executions():
    curve, notes = reconstruct_realized_curve([], _panel({"A": [100.0] * 10}), initial_capital=1e6)
    assert curve == []
    assert notes == []


# ─────────────────────────── 비교 지표 ───────────────────────────


def _curve(vals: list[float], start_idx=0) -> list[dict]:
    return [
        {"t": BDAYS[start_idx + i].date().isoformat(), "v": v}
        for i, v in enumerate(vals)
    ]


def test_index100_normalization():
    realized = _curve([200.0, 220.0, 210.0])
    out = compute_tracking(realized, [])
    idx = out["realized_index"]
    assert math.isclose(idx[0]["v"], 100.0, abs_tol=1e-9)
    assert math.isclose(idx[1]["v"], 110.0, abs_tol=1e-9)
    assert math.isclose(idx[2]["v"], 105.0, abs_tol=1e-9)


def test_identical_curves_zero_tracking_error():
    vals = [100.0, 101.0, 103.0, 102.0, 105.0]
    c = _curve(vals)
    out = compute_tracking(c, list(c))
    m = out["metrics"]
    assert m["n_common_days"] == 5
    assert math.isclose(m["tracking_error"], 0.0, abs_tol=1e-12)
    assert math.isclose(m["cumulative_divergence_pct"], 0.0, abs_tol=1e-9)
    assert math.isclose(m["realized_return_pct"], m["backtest_return_pct"], abs_tol=1e-9)
    assert math.isclose(m["correlation"], 1.0, abs_tol=1e-9)


def test_divergence_positive_when_realized_outperforms():
    realized = _curve([100.0, 110.0, 120.0])   # +20%
    backtest = _curve([100.0, 105.0, 110.0])    # +10%
    m = compute_tracking(realized, backtest)["metrics"]
    assert math.isclose(m["realized_return_pct"], 20.0, abs_tol=1e-9)
    assert math.isclose(m["backtest_return_pct"], 10.0, abs_tol=1e-9)
    # (1.20/1.10 - 1)*100 ≈ 9.09%
    assert math.isclose(m["cumulative_divergence_pct"], (1.20 / 1.10 - 1) * 100, abs_tol=1e-9)
    assert m["tracking_error"] is not None and m["tracking_error"] > 0


def test_no_backtest_curve_null_metrics():
    out = compute_tracking(_curve([100.0, 110.0]), [])
    m = out["metrics"]
    assert m["tracking_error"] is None
    assert m["cumulative_divergence_pct"] is None
    assert m["n_common_days"] == 0
    assert out["backtest_index"] == []


def test_partial_overlap_uses_common_days():
    # 실측 day0~4, 백테스트 day2~6 → 공통 day2~4(3일).
    realized = _curve([100.0, 101.0, 102.0, 103.0, 104.0], start_idx=0)
    backtest = _curve([200.0, 201.0, 202.0, 203.0, 204.0], start_idx=2)
    m = compute_tracking(realized, backtest)["metrics"]
    assert m["n_common_days"] == 3
