"""실거래–백테스트 체결 정합 실측(P2-3) 순수함수 검증.

compute_fill_quality 의 M1/M2/M3 계산·부호 규약·검산 항등식·등급 판정·표본 부족
(판정 보류) 경로를 mock Order/Execution/종가 패널로 검증한다. DB·네트워크 불필요.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.backtest.fill_quality import (
    DEFAULT_SLIP_BPS,
    _g,
    compute_fill_quality,
)

BDAYS = pd.bdate_range("2025-01-02", periods=60)


def _panel(prices: dict[str, list[float]] | None = None, symbols=("A",)) -> pd.DataFrame:
    """평평(=변동성 0) 기본 패널. prices 로 종목별 종가 시퀀스를 덮어쓸 수 있다."""
    data = {}
    for s in symbols:
        data[s] = [100.0] * len(BDAYS)
    if prices:
        for s, seq in prices.items():
            data[s] = seq
    return pd.DataFrame(data, index=BDAYS)


def _order(order_id, symbol, side, fill_idx, p_live, decision_price=None):
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "decision_price": decision_price,
        "fill_date": BDAYS[fill_idx],
        "p_live": p_live,
        "filled_qty": 10.0,
    }


# ─────────────────────── M1/M2/M3 정확값 ───────────────────────


def test_m1_m2_m3_exact_buy():
    # A: t(idx10) 종가 100, t+1(idx11) 종가 101. 매수 결정가 99.5, 실제 체결 100.3.
    seq = [100.0] * len(BDAYS)
    seq[11] = 101.0
    panel = _panel({"A": seq})
    orders = [_order(1, "A", "buy", 10, p_live=100.3, decision_price=99.5)]

    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0
    )
    po = res["per_order"][0]

    # M1 = (100.3-99.5)/99.5*1e4, 매수 d=+1
    assert po["m1_exec_bp"] == pytest.approx(_g(100.3, 99.5, 1))
    # M2 = (C_t - C_t1)/C_t1*1e4 = (100-101)/101*1e4
    assert po["m2_time_bp"] == pytest.approx(_g(100.0, 101.0, 1))
    # s=5bp → P_bt = 101*(1+0.0005)
    assert po["s_bp"] == pytest.approx(5.0)
    assert po["p_bt"] == pytest.approx(101.0 * 1.0005)
    assert po["m3_total_bp"] == pytest.approx(_g(100.3, 101.0 * 1.0005, 1))


def test_sell_direction_sign():
    # 매도: 체결가 < 결정가면 불리 → M1 양수(비용). d=-1.
    panel = _panel()  # 평평 100
    orders = [_order(1, "A", "sell", 10, p_live=99.7, decision_price=100.0)]
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0
    )
    po = res["per_order"][0]
    # 매도를 결정가보다 낮게 체결 → 불리 → 양수
    assert po["m1_exec_bp"] > 0
    assert po["m1_exec_bp"] == pytest.approx(_g(99.7, 100.0, -1))
    # 매도 s: P_bt = C_t1*(1 - s)
    assert po["p_bt"] == pytest.approx(100.0 * (1 - 0.0005))


def test_checksum_identity_small_residual():
    # 결정가 = 신호일 종가(C_t)일 때 검산 항등식 잔차가 작다(2차 오차만).
    seq = [100.0] * len(BDAYS)
    seq[11] = 101.0  # 1% 이동
    panel = _panel({"A": seq})
    orders = [_order(1, "A", "buy", 10, p_live=100.3, decision_price=100.0)]
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0
    )
    assert abs(res["per_order"][0]["checksum_residual_bp"]) < 1.0
    assert res["checksum"]["mean_abs_residual_bp"] < 1.0


# ─────────────────────── arrival 폴백 ───────────────────────


def test_arrival_fallback_uses_close():
    panel = _panel()
    orders = [_order(1, "A", "buy", 10, p_live=100.5, decision_price=None)]
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0
    )
    po = res["per_order"][0]
    assert po["arrival_fallback"] is True
    assert po["r_arrival"] == pytest.approx(100.0)  # C_t
    assert res["sample"]["n_arrival_fallback"] == 1


# ─────────────────────── 스킵 경로 ───────────────────────


def test_skip_no_next_close():
    # 체결일이 패널의 마지막 날 → t+1 없음 → 스킵.
    panel = _panel()
    orders = [_order(1, "A", "buy", len(BDAYS) - 1, p_live=100.5, decision_price=100.0)]
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0
    )
    assert res["sample"]["n_orders"] == 0
    assert res["sample"]["n_skipped"] == 1
    assert res["skipped"][0]["reason"] == "no_next_close"


def test_skip_no_price_symbol():
    panel = _panel(symbols=("A",))
    orders = [_order(1, "ZZZ", "buy", 10, p_live=100.0, decision_price=100.0)]
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0
    )
    assert res["sample"]["n_orders"] == 0
    assert res["skipped"][0]["reason"] == "no_price_symbol"


# ─────────────────────── 등급 판정 ───────────────────────


def _make_orders_fixed_m1(n, m1_bp, symbol="A", fill_idx=10):
    """M1 = m1_bp 로 고정된 매수 주문 n개(결정가 100, 체결가 = 100*(1+m1/1e4))."""
    p_live = 100.0 * (1 + m1_bp / 1e4)
    return [
        _order(i, symbol, "buy", fill_idx, p_live=p_live, decision_price=100.0)
        for i in range(n)
    ]


def test_insufficient_sample_holds_judgment():
    panel = _panel()
    orders = _make_orders_fixed_m1(5, 3.0)  # min_sample(기본 30) 미만
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0,
        annual_turnover=2.0,
    )
    assert res["sample"]["n_orders"] == 5
    assert res["grades"]["m1_exec"] == "INSUFFICIENT"
    assert res["grades"]["m3_total"] == "INSUFFICIENT"


def test_green_grade():
    panel = _panel()
    orders = _make_orders_fixed_m1(30, 3.0)  # μ=3bp ≤5, σ=0
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0,
        annual_turnover=2.0,
    )
    assert res["m1_exec"]["all"]["mean"] == pytest.approx(3.0)
    assert res["grades"]["m1_exec"] == "GREEN"
    # 드래그 차이 = 2*(3-5)/1e4*100 = -0.04% ≤ 0.5 → GREEN
    assert res["grades"]["m3_total"] == "GREEN"


def test_red_grade_m1():
    panel = _panel()
    orders = _make_orders_fixed_m1(30, 20.0)  # μ=20bp > 15 → RED
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0,
        annual_turnover=12.0,
    )
    assert res["grades"]["m1_exec"] == "RED"
    # 드래그 차이 = 12*(20-5)/1e4*100 = 1.8% > 1.5 → RED
    assert res["annualized_drag"]["drag_diff_pct_per_yr"] == pytest.approx(1.8)
    assert res["grades"]["m3_total"] == "RED"


def test_m3_insufficient_without_turnover():
    panel = _panel()
    orders = _make_orders_fixed_m1(30, 3.0)
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0,
        annual_turnover=None,  # 회전율 미상
    )
    assert res["annualized_drag"]["drag_diff_pct_per_yr"] is None
    assert res["grades"]["m3_total"] == "INSUFFICIENT"
    assert res["grades"]["m1_exec"] == "GREEN"  # M1 은 회전율과 무관


# ─────────────────────── 빈 입력 ───────────────────────


def test_empty_orders():
    res = compute_fill_quality(
        [], pd.DataFrame(), slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0
    )
    assert res["sample"]["n_orders"] == 0
    assert res["grades"]["m1_exec"] == "INSUFFICIENT"
    assert res["grades"]["m3_total"] == "INSUFFICIENT"
    assert res["m1_exec"]["all"]["mean"] is None


# ─────────────────────── 방향 분리 집계 ───────────────────────


def test_direction_split():
    panel = _panel()
    orders = [
        _order(1, "A", "buy", 10, p_live=100.3, decision_price=100.0),
        _order(2, "A", "sell", 10, p_live=99.7, decision_price=100.0),
    ]
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=0.0
    )
    assert res["m1_exec"]["buy"]["n"] == 1
    assert res["m1_exec"]["sell"]["n"] == 1
    assert res["sample"]["n_clusters"] == 1  # 같은 체결일


# ─────────────────────── 변동성 슬리피지 재현 ───────────────────────


def test_vol_slippage_map_applied():
    # 변동성이 다른 두 종목을 같은 날 체결 → _vol_slippage_map 이 종목별 s 를 다르게 준다.
    rng = np.random.default_rng(0)
    lowvol = list(100 + np.cumsum(rng.normal(0, 0.1, len(BDAYS))))
    highvol = list(100 + np.cumsum(rng.normal(0, 2.0, len(BDAYS))))
    panel = _panel({"A": lowvol, "B": highvol}, symbols=("A", "B"))
    orders = [
        _order(1, "A", "buy", 30, p_live=panel["A"].iloc[30], decision_price=panel["A"].iloc[30]),
        _order(2, "B", "buy", 30, p_live=panel["B"].iloc[30], decision_price=panel["B"].iloc[30]),
    ]
    res = compute_fill_quality(
        orders, panel, slip_base=DEFAULT_SLIP_BPS / 1e4, slip_vol_scale=1.0
    )
    s_by_sym = {po["symbol"]: po["s_bp"] for po in res["per_order"]}
    # 고변동 종목 B 의 슬리피지가 저변동 A 보다 크다(중앙값 대비 스케일).
    assert s_by_sym["B"] > s_by_sym["A"]
