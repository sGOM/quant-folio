"""Deflated Sharpe Ratio(DSR, 과최적화 방어) 순수함수 검증.

financial-expert 지정 필수 항목:
  (1) 연율→일별 샤프비 변환 정확성(빠뜨리면 DSR≈1 치명버그)
  (2) N=1(또는 N_eff<2) 시 DSR 미정의 처리
  (3) 정규분포 합성 데이터로 DSR 이 이론값에 근접(시드 고정 sanity check)
  (4) N_eff ≤ N 불변식
추가: 등급 라벨 경계값, 표본 부족·비유한 게이트, 상관 보정, 오케스트레이터.
DB·네트워크 불필요(순수함수).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import norm

from app.services.backtest.deflated_sharpe import (
    EULER,
    MIN_OBS,
    analyze_backtest_dsr,
    compute_deflated_sharpe,
    estimate_trial_stats,
    grade_dsr,
    returns_from_equity_curve,
)


def _normal_returns(n=750, mean=0.0008, std=0.01, seed=0):
    """시드 고정 정규분포 일별수익률."""
    rng = np.random.default_rng(seed)
    return rng.normal(mean, std, n)


def _equity_from_returns(returns, start=1_000_000.0):
    """수익률 → equity_curve([{"t","v"}]) 리스트(초기값 포함)."""
    vals = [start]
    for r in returns:
        vals.append(vals[-1] * (1.0 + r))
    dates = np.arange(len(vals))
    return [{"t": f"2020-01-{i:02d}", "v": v} for i, v in zip(dates, vals)]


# ─────────────────── (1) 연율→일별 변환 정확성 ───────────────────
def test_annual_to_daily_conversion():
    """SR_daily 는 정확히 sharpe_annual/√ann 여야 한다(변환 누락=DSR≈1 버그)."""
    r = _normal_returns(seed=1)
    out = compute_deflated_sharpe(r, sharpe_annual=1.5, N=10, V_daily=0.001, ann=252)
    assert math.isclose(out["SR_daily"], 1.5 / math.sqrt(252), rel_tol=1e-12)


def test_missing_conversion_would_inflate_dsr():
    """변환을 빠뜨리면(연율값을 그대로 일별 취급) DSR 이 사실상 1로 수렴함을 증명.

    같은 데이터로 연율 SR 을 '일별'로 잘못 넣으면(√252 미적용) √(n-1) 증폭으로
    DSR→1. 올바른 변환은 훨씬 낮은 값을 준다.
    """
    r = _normal_returns(n=750, mean=0.0008, std=0.01, seed=2)
    # 실제 연율 샤프비를 데이터에서 산출.
    sr_daily_true = r.mean() / r.std(ddof=1)
    sharpe_annual = sr_daily_true * math.sqrt(252)

    correct = compute_deflated_sharpe(r, sharpe_annual, N=5, V_daily=1e-4, ann=252)
    # 버그 시뮬: 연율값을 ann=1 로 넣으면 SR_daily=연율값(변환 누락과 등가).
    buggy = compute_deflated_sharpe(r, sharpe_annual, N=5, V_daily=1e-4, ann=1)
    assert buggy["PSR0"] > 0.999  # 변환 누락 시 사실상 1
    assert correct["PSR0"] < buggy["PSR0"]  # 올바른 변환은 덜 극단적


# ─────────────────── (2) N_eff<2 → DSR 미정의 ───────────────────
def test_dsr_undefined_when_N_below_2():
    r = _normal_returns(seed=3)
    out = compute_deflated_sharpe(r, sharpe_annual=1.0, N=1, V_daily=0.001)
    assert out["DSR"] is None
    assert out["SR0_daily"] is None
    assert out["PSR0"] is not None  # PSR0 는 여전히 제공


def test_grade_insufficient_trials_for_undefined():
    assert grade_dsr(None) == "insufficient_trials"


# ─────────────────── (3) 정규분포 sanity: DSR ≈ 이론값 ───────────────────
def test_dsr_matches_closed_form_for_normal_returns():
    """정규분포(왜도≈0·첨도≈3)면 PSR 분모 se≈1 이라 DSR 이 폐형식에 근접해야 한다.

    se=√(1−γ3·SR+((γ4−1)/4)·SR²) ≈ 1, SR0 은 결정적 공식이므로
    DSR ≈ Φ((SR−SR0)·√(n−1)) 를 직접 검증한다.
    """
    r = _normal_returns(n=1000, mean=0.0006, std=0.012, seed=7)
    sr_daily = r.mean() / r.std(ddof=1)
    sharpe_annual = sr_daily * math.sqrt(252)
    N, V = 8, 2e-4
    out = compute_deflated_sharpe(r, sharpe_annual, N=N, V_daily=V, ann=252)

    # 폐형식 재계산.
    SR0 = math.sqrt(V) * (
        (1 - EULER) * norm.ppf(1 - 1.0 / N) + EULER * norm.ppf(1 - 1.0 / (N * math.e))
    )
    se = math.sqrt(1 - out["skew"] * out["SR_daily"]
                   + ((out["kurt"] - 1) / 4) * out["SR_daily"] ** 2)
    expected = norm.cdf((out["SR_daily"] - SR0) * math.sqrt(len(r) - 1) / se)
    assert math.isclose(out["DSR"], expected, rel_tol=1e-9)
    # 정규성 확인: se 가 1 근방.
    assert abs(se - 1.0) < 0.05


# ─────────────────── (4) N_eff ≤ N 불변식 ───────────────────
def test_n_eff_le_n_invariant_positive_corr():
    """중간 양의 상관 시행들: 2 ≤ N_eff < N."""
    base = _normal_returns(n=300, seed=10)
    # 공통성분(base) + 종목별 잡음(std 0.02)으로 ρ̄≈0.2 수준의 중간 상관.
    returns_list = [base + _normal_returns(n=300, std=0.02, seed=100 + i) for i in range(5)]
    sharpes = [1.0 + 0.1 * i for i in range(5)]
    stats = estimate_trial_stats(sharpes, returns_list)
    assert stats["N"] == 5
    assert 2 <= stats["N_eff"] < stats["N"]
    assert stats["rho_bar"] > 0.0  # 양의 상관 확인


def test_n_eff_collapses_under_extreme_corr():
    """거의 동일(ρ̄→1)한 시행들은 N_eff 가 1로 붕괴 → DSR 미정의 신호(<2)."""
    base = _normal_returns(n=300, seed=11)
    returns_list = [base + _normal_returns(n=300, std=0.001, seed=300 + i) for i in range(5)]
    sharpes = [1.0] * 5
    stats = estimate_trial_stats(sharpes, returns_list)
    assert stats["rho_bar"] > 0.9
    assert stats["N_eff"] < 2


def test_n_eff_le_n_invariant_negative_corr_clamped():
    """음의 상관이라도 ρ̄ 를 0 클램프해 N_eff ≤ N 을 보장(불변식)."""
    a = _normal_returns(n=300, seed=20)
    returns_list = [a, -a, a * 0.5, -a * 0.5]  # 강한 음/양 혼합
    sharpes = [1.0, 0.9, 1.1, 0.8]
    stats = estimate_trial_stats(sharpes, returns_list)
    assert stats["N_eff"] <= stats["N"]


def test_n_eff_uncorrelated_equals_n():
    """무상관(ρ̄≈0) 시행들은 N_eff ≈ N(보정 없음, 가장 보수적)."""
    returns_list = [_normal_returns(n=400, seed=200 + i) for i in range(6)]
    sharpes = [1.0] * 6
    stats = estimate_trial_stats(sharpes, returns_list)
    assert abs(stats["rho_bar"]) < 0.1
    assert stats["N_eff"] == stats["N"]


# ─────────────────── 등급 라벨 경계값 ───────────────────
def test_grade_boundaries():
    assert grade_dsr(0.95) == "strong"
    assert grade_dsr(0.9999) == "strong"
    assert grade_dsr(0.94999) == "marginal"
    assert grade_dsr(0.90) == "marginal"
    assert grade_dsr(0.89999) == "inconclusive"
    assert grade_dsr(0.50) == "inconclusive"
    assert grade_dsr(0.49999) == "overfit_suspected"
    assert grade_dsr(0.0) == "overfit_suspected"


# ─────────────────── V_daily 추정·저신뢰 플래그 ───────────────────
def test_v_daily_sample_variance_and_low_confidence():
    sharpes = [0.5, 1.0, 1.5]  # 연율
    returns_list = [_normal_returns(n=200, seed=i) for i in range(3)]
    stats = estimate_trial_stats(sharpes, returns_list, ann=252)
    daily = [s / math.sqrt(252) for s in sharpes]
    assert math.isclose(stats["V_daily"], np.var(daily, ddof=1), rel_tol=1e-12)
    assert stats["v_low_confidence"] is True  # N=3 < 5


def test_v_low_confidence_false_when_enough_trials():
    sharpes = [1.0] * 6
    returns_list = [_normal_returns(n=200, seed=i) for i in range(6)]
    stats = estimate_trial_stats(sharpes, returns_list)
    assert stats["v_low_confidence"] is False


# ─────────────────── 표본 부족·비유한 게이트 ───────────────────
def test_min_obs_gate_raises():
    r = _normal_returns(n=MIN_OBS - 1, seed=5)
    try:
        compute_deflated_sharpe(r, 1.0, N=5, V_daily=1e-4)
        assert False, "표본 부족인데 예외가 없음"
    except ValueError:
        pass


def test_returns_from_equity_curve():
    eq = [{"t": "d0", "v": 100.0}, {"t": "d1", "v": 110.0}, {"t": "d2", "v": 99.0}]
    r = returns_from_equity_curve(eq)
    assert np.allclose(r, [0.10, (99.0 - 110.0) / 110.0])


def test_returns_from_short_curve_empty():
    assert returns_from_equity_curve([{"t": "d0", "v": 100.0}]).size == 0
    assert returns_from_equity_curve([]).size == 0


# ─────────────────── 오케스트레이터(analyze_backtest_dsr) ───────────────────
def test_analyze_ok_with_homogeneous_set():
    """정상 경로: 여러 동질 시행 → DSR 산출·등급 부여."""
    target_r = _normal_returns(n=600, mean=0.001, std=0.01, seed=42)
    target = {"sharpe": (target_r.mean() / target_r.std(ddof=1)) * math.sqrt(252),
              "equity_curve": _equity_from_returns(target_r)}
    homo = [target]
    for i in range(4):
        ri = _normal_returns(n=600, mean=0.0005, std=0.011, seed=50 + i)
        homo.append({"sharpe": (ri.mean() / ri.std(ddof=1)) * math.sqrt(252),
                     "equity_curve": _equity_from_returns(ri)})
    out = analyze_backtest_dsr(target, homo)
    assert out["status"] == "ok"
    assert out["dsr"] is not None
    assert 0.0 <= out["dsr"] <= 1.0
    assert out["N"] == 5
    assert out["N_eff"] <= out["N"]
    assert out["grade"] in {"strong", "marginal", "inconclusive", "overfit_suspected"}


def test_analyze_single_trial_insufficient():
    """N=1 동질 집합 → insufficient_trials, PSR0 만."""
    target_r = _normal_returns(n=600, seed=99)
    target = {"sharpe": (target_r.mean() / target_r.std(ddof=1)) * math.sqrt(252),
              "equity_curve": _equity_from_returns(target_r)}
    out = analyze_backtest_dsr(target, [target])
    assert out["status"] == "insufficient_trials"
    assert out["dsr"] is None
    assert out["psr0"] is not None
    assert out["grade"] == "insufficient_trials"
    assert "reason" in out


def test_analyze_sample_insufficient():
    """관측치 < MIN_OBS → unavailable."""
    short_r = _normal_returns(n=10, seed=3)
    target = {"sharpe": 1.0, "equity_curve": _equity_from_returns(short_r)}
    out = analyze_backtest_dsr(target, [target])
    assert out["status"] == "unavailable"
    assert out["grade"] == "insufficient_trials"


def test_analyze_missing_sharpe():
    target = {"sharpe": None, "equity_curve": _equity_from_returns(_normal_returns())}
    out = analyze_backtest_dsr(target, [target])
    assert out["status"] == "unavailable"
