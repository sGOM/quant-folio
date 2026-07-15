"""패닉 오버레이(P2, 대표안 A/대조군 B) 상태기계 검증 — 합성 데이터.

Arm(자본항복 후보 감지)→Confirm(재진입 확인)→Fill(익일 종가 체결) 상태기계와
노출 스케일링(base_exposure/panic_exposure)·손절(knife_stop)·시간청산(hold_days)이
설계대로 동작하는지 네트워크 없이 검증한다(panic_series 를 직접 합성해 주입).

지수 종가는 명시적 override 지점 외에는 완전히 평평(flat)하게 유지해, 배경 추세로 인한
의도치 않은 Confirm/익절/손절 오탐(우연한 트리거)을 원천 차단한다(결정론적 시나리오).
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.schemas.strategy import RebalanceConfig
from app.services.backtest.portfolio import run_rebalance_backtest


def _panel(n=80, price=100.0):
    """단일 종목 momentum 선정 코어용 완만한 상승 패널(패닉 신호와 무관하게 항상 선정됨)."""
    dates = pd.bdate_range("2024-01-01", periods=n)
    vals = [price * (1.0 + 0.0005 * i) for i in range(n)]
    return pd.DataFrame({"AAAAAA": vals, "BBBBBB": vals}, index=dates)


def _panic_series(dates, *, capit_idx, overrides: dict[int, float] | None = None,
                   r1: float = -0.06, capit_low_offset: float = 5.0):
    """합성 패닉 시계열: 평상시 지수 종가는 1000.0 으로 완전히 평평하다.

    capit_idx 일에 r1 만큼 급락(하드트리거 패닉 라벨)이 발생하고, 그 이후 종가는
    직전 값을 그대로 유지(flat)하다가 overrides={day_idx: close_value} 로 지정한
    지점에서만 값이 바뀐다(그 값이 다음 override 지점까지 그대로 유지됨). 이렇게 하면
    Confirm/익절/손절 조건이 테스트가 의도한 지점에서만 정확히 발생한다.
    """
    n = len(dates)
    overrides = dict(overrides or {})
    base = 1000.0
    if capit_idx not in overrides:
        overrides[capit_idx] = base * (1.0 + r1)

    close = [0.0] * n
    cur = base
    for i in range(n):
        if i in overrides:
            cur = overrides[i]
        close[i] = cur

    low = [c - 2.0 for c in close]
    low[capit_idx] = close[capit_idx] - capit_low_offset

    level = ["normal"] * n
    hard = [False] * n
    level[capit_idx] = "panic"
    hard[capit_idx] = True
    score = [80.0 if lv == "panic" else 10.0 for lv in level]

    return pd.DataFrame(
        {"score": score, "level": level, "gated": hard, "hard_trigger": hard,
         "close": close, "low": low, "dd60": [-0.02] * n},
        index=dates,
    )


def _cfg(**overlay_kwargs):
    base = dict(
        type="rebalance", universe=["AAAAAA", "BBBBBB"],
        selection={"method": "momentum", "top_n": 2, "lookback": 5},
        weighting="equal", cadence="monthly", rebalance_dom=1,
        drift_band_pct=0.01, capital=10_000_000.0, fees=0.0, tax=0.0,
        fill_mode="next_close", slippage_bps=0.0,
    )
    overlay = dict(
        enabled=True, arm_level="warning", arm_window=5, hold_days=10,
        profit_reclaim_pct=0.99, knife_stop_pct=0.05,
        base_exposure=0.5, panic_exposure=1.0, scale_in_confirm=1.0,
        ma_recovery_period=5, event_only=False,
    )
    overlay.update(overlay_kwargs)
    base["panic_overlay"] = overlay
    RebalanceConfig(**base)  # 스키마 검증(실패 시 여기서 예외)
    return base


def test_panic_confirm_scales_exposure_up_from_base():
    """평상시 base_exposure(0.5)로 운용되다가 Confirm 시 panic_exposure(1.0)로 확대된다.

    profit_reclaim/knife_stop 을 느슨하게 두고 confirm 이후 종가를 평평하게 유지해
    윈도우 끝까지 오버레이가 유지되도록 한다 — 마지막 holdings 비중으로 확인한다.
    """
    panel = _panel(60)
    dates = panel.index
    capit_idx, confirm_idx = 20, 21
    ps = _panic_series(
        dates, capit_idx=capit_idx,
        overrides={confirm_idx: 1000.0 * (1 - 0.06) * 1.02},  # capit_close 대비 +2% 반등 → Confirm
    )
    # 10일 워밍업 확보(sim_start=dates[10])로 첫 정기 리밸런싱에 lookback 히스토리가 충분하게.
    # hold_days 를 시뮬레이션 창보다 길게 두어 창 끝까지 오버레이가 유지되게 한다.
    cfg = _cfg(hold_days=200)

    res = run_rebalance_backtest(panel, cfg, dates[10], dates[-1], panic_series=ps)

    assert res["num_panic_events"] >= 1
    assert [m for m in res["markers"] if m["type"] == "panic_confirm"]

    # Confirm 이전 정기 리밸런싱은 base_exposure(0.5) 만 배치되어야 한다.
    pre_confirm_buys = [
        t for t in res["trades"]
        if t["reason"] == "rebalance" and t["side"] == "buy" and t["t"] < ps.index[confirm_idx].isoformat()
    ]
    assert pre_confirm_buys
    pre_total = sum(t["amount"] for t in pre_confirm_buys if t["t"] == pre_confirm_buys[0]["t"])
    assert pre_total / cfg["capital"] == pytest.approx(0.5, abs=0.05)

    # Confirm 거래가 존재해야 하고, 그 이후(윈도우 끝까지 종가 평평 → 청산 없음) 보유비중
    # 합은 panic_exposure(1.0) 근처여야 한다(현금 거의 없음).
    assert [t for t in res["trades"] if t["reason"] == "panic_confirm"]
    assert sum(res["holdings"].values()) == pytest.approx(1.0, abs=0.05)


def test_panic_arm_times_out_without_confirm():
    """Arm 이후 arm_window 내 확인(Confirm) 못 하면 포기하고 idle 로 복귀한다."""
    panel = _panel(60)
    dates = panel.index
    capit_idx = 20
    # override 없음 → 자본항복일 이후 종가가 capit_close 에서 완전히 평평(반등 없음).
    ps = _panic_series(dates, capit_idx=capit_idx)
    cfg = _cfg(arm_window=3)

    res = run_rebalance_backtest(panel, cfg, dates[10], dates[-1], panic_series=ps)

    assert res["num_panic_events"] == 0
    assert [m for m in res["markers"] if m["type"] == "panic_arm"]
    assert [m for m in res["markers"] if m["type"] == "panic_arm_timeout"]
    assert not [m for m in res["markers"] if m["type"] == "panic_confirm"]


def test_panic_knife_stop_exits_overlay():
    """Confirm 이후 지수가 자본항복일 저가 대비 knife_stop 이상 추가 이탈하면 즉시 해제된다."""
    panel = _panel(70)
    dates = panel.index
    capit_idx, confirm_idx, knife_idx = 15, 16, 20
    capit_close = 1000.0 * 0.94
    capit_low = capit_close - 5.0
    ps = _panic_series(
        dates, capit_idx=capit_idx,
        overrides={
            confirm_idx: capit_close * 1.02,        # Confirm: capit_close 대비 +2%
            knife_idx: capit_low * 0.90,             # 손절: 자본항복일 저가 대비 -10% (임계 -5% 이탈)
        },
    )
    cfg = _cfg(knife_stop_pct=0.05, hold_days=60)

    res = run_rebalance_backtest(panel, cfg, dates[10], dates[-1], panic_series=ps)

    assert res["num_panic_events"] >= 1
    assert [m for m in res["markers"] if m["type"] == "panic_confirm"]
    knife_exits = [t for t in res["trades"] if t["reason"] == "panic_exit_knife"]
    assert knife_exits, "손절(panic_exit_knife) 거래가 있어야 한다"


def test_event_only_control_group_no_core_position():
    """대조군 B(event_only=True): 정기 리밸런싱이 없고 Confirm 시에만 편입, 청산 시 전량 현금화."""
    panel = _panel(60)
    dates = panel.index
    capit_idx, confirm_idx = 20, 21
    capit_close = 1000.0 * 0.94
    ps = _panic_series(
        dates, capit_idx=capit_idx,
        overrides={confirm_idx: capit_close * 1.02},
    )
    cfg = _cfg(event_only=True, hold_days=5, base_exposure=0.0, panic_exposure=1.0)

    res = run_rebalance_backtest(panel, cfg, dates[10], dates[-1], panic_series=ps)

    # 정기 리밸런싱(cadence) 유래 거래가 없어야 한다(상시 core 없음).
    assert not [t for t in res["trades"] if t["reason"] == "rebalance"]
    assert res["num_panic_events"] >= 1
    assert [t for t in res["trades"] if t["reason"] == "panic_confirm"]
    # hold_days 경과 후 전량 현금화(청산) 되어야 한다(종가가 평평해 익절/손절 미발동).
    hold_exits = [t for t in res["trades"] if t["reason"] == "panic_exit_hold"]
    assert hold_exits, "시간청산(panic_exit_hold) 거래가 있어야 한다"


def test_panic_overlay_disabled_no_effect():
    """panic_overlay 가 없으면 기존 동작과 완전히 동일하다(회귀 안전)."""
    panel = _panel(40)
    dates = panel.index
    cfg = _cfg()
    del cfg["panic_overlay"]
    res_a = run_rebalance_backtest(panel, cfg, dates[0], dates[-1])
    res_b = run_rebalance_backtest(panel, cfg, dates[0], dates[-1], panic_series=None)
    assert res_a["total_return"] == pytest.approx(res_b["total_return"])
    assert res_a.get("num_panic_events", 0) == 0
