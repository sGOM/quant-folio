"""Deflated Sharpe Ratio (DSR) — 과최적화(다중검정) 통계 방어 사후 분석 레이어.

Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Correcting for
Selection Bias, Backtest Overfitting, and Non-Normality" 방법론을 구현한다.
기존 백테스트 엔진(engine.py/portfolio.py)의 샤프 계산은 건드리지 않고, 저장된
백테스트 이력(Backtest.result)을 사후 분석하는 순수 계산 레이어다.

────────────────────────── 수식 ──────────────────────────
같은 전략에 파라미터를 바꿔가며 N번 백테스트하면, 순전히 우연으로도 가장 높은
샤프비가 부풀려진다(다중검정 selection bias). DSR 은 이 기대 최대치를 벤치마크로
삼아 관측 샤프비의 통계적 유의성을 보정한다.

일별 통계(equity_curve 의 v 배열에서 단순수익률 r = diff(v)/v[:-1]):
  n  = len(r)
  γ3 = skew(r)              (표본 왜도)
  γ4 = kurtosis(r, fisher=False)  (비초과 첨도, 정규분포=3)

PSR(SR*) = Φ( (SR_hat − SR*)·√(n−1) / √(1 − γ3·SR_hat + ((γ4−1)/4)·SR_hat²) )
  Φ = 표준정규 CDF. SR_hat/SR* 는 모두 **일별** 샤프비.

기대 최대 샤프비(다중시행 벤치마크):
  SR*_0 = √V · [ (1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)) ]
  γ = 오일러-마스케로니 상수, N = 시행 횟수(유효), V = 시행 간 일별 샤프비 표본분산.

  DSR = PSR(SR*_0)
  PSR0 = PSR(0)  (다중검정 미보정 유의성, 참고용)

⚠️ 단위 함정: Backtest.result["sharpe"] 는 **연율화**(freq="1D", 연 252거래일)된
값이다. DSR 은 반드시 일별 샤프비로 계산해야 한다: SR_daily = sharpe_annual/√252.
이 변환을 빠뜨리면 √(n−1) 항 때문에 DSR 이 사실상 항상 1로 수렴하는 치명적
버그가 된다(test_deflated_sharpe 로 못박음).

────────────────────── N·V 추정(동질 시행 집합) ──────────────────────
- 동질 집합: 같은 strategy_id + 같은 period_start/period_end 의 Backtest 이력을
  1차 후보로 잡고, 대상 result 에 유니버스 지문(§22, universe_fingerprint —
  실행 시점 유니버스+universe_rule 파라미터의 해시)이 있으면 지문까지 일치하는
  행으로 좁힌다. 지문이 없는 과거 이력(§22 도입 이전 실행)은 대상 자체에 지문이
  없을 때만 기간 필터로 폴백한다(하위호환). 이 집합이 "파라미터 탐색 시행들"이다.
  조회·필터는 API 라우트(backtests.py)에서 수행하고, 이 모듈은 각 시행의
  (sharpe_annual, daily_returns) 만 받아 통계를 낸다.
- N = 동질 집합 행 수(현재 대상 자신 포함). N<2 이면 DSR 미정의.
- V_daily = 각 행 sharpe_annual/√252 의 표본분산(ddof=1). N<5 면 불안정
  (v_low_confidence=True).
- N_eff(유효 시행 수): 파라미터 변형은 서로 상관되어 있어 N 을 그대로 쓰면
  과대보정된다. 시행 간 일별수익률의 평균 쌍상관 ρ̄ 로 보정:
      N_eff ≈ N / (1 + (N−1)·ρ̄)
  ρ̄ 는 [0, 1) 로 클램프(음수는 N_eff>N 을 유발하므로 0 처리 — N_eff≤N 불변식
  보장). N_eff 는 보수적으로 내림(floor), 최소 2. N_eff<2 면 DSR 미정의.

──────────────────────────── 한계 ────────────────────────────
- equity_curve 의 일별 간격을 그대로 가정(현 프로젝트 곡선은 일별). 리밸런싱
  전략이 저빈도로 값이 찍히면 연율화 252 가정과 어긋날 수 있으나, 과도한 정교화
  대신 관측치 수를 그대로 n 에 쓴다(방법론 신뢰도 "낮음" 한계로 문서화).
- N_eff 보정은 participation-ratio(고윳값) 대신 평균 쌍상관 간이식이다.
- 유니버스 동일성은 §22(universe_fingerprint)로 해소됐으나, 지문이 없는 과거
  이력이 섞인 동질 집합(대상 자체가 구지문 이전 실행인 경우)은 여전히 기간만으로
  필터되어 서로 다른 유니버스가 섞일 수 있다 — 신규 백테스트가 쌓일수록 자연 해소.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.stats import kurtosis, norm, skew

# 오일러-마스케로니 상수.
EULER = 0.5772156649015329

# 계산에 필요한 최소 관측치 수(그 미만이면 표본 부족으로 계산하지 않는다).
MIN_OBS = 30

# 판정 등급 라벨 경계. DSR 이 미정의(N_eff<2)면 "insufficient_trials".
GRADE_STRONG = 0.95
GRADE_MARGINAL = 0.90
GRADE_INCONCLUSIVE = 0.50


def grade_dsr(dsr: float | None) -> str:
    """DSR 값을 판정 등급 라벨로 매핑한다.

    ≥0.95 strong · 0.90~0.95 marginal · 0.50~0.90 inconclusive ·
    <0.50 overfit_suspected · None insufficient_trials.
    """
    if dsr is None:
        return "insufficient_trials"
    if dsr >= GRADE_STRONG:
        return "strong"
    if dsr >= GRADE_MARGINAL:
        return "marginal"
    if dsr >= GRADE_INCONCLUSIVE:
        return "inconclusive"
    return "overfit_suspected"


def returns_from_equity_curve(equity_curve: list[dict]) -> np.ndarray:
    """equity_curve([{"t","v"},...])의 자산가치 배열에서 단순 일별수익률을 만든다.

    r = diff(v)/v[:-1]. v 가 0/음수·비유한이면 그 지점 수익률은 계산 불가라 결과가
    비유한이 되므로 호출부에서 표본 유효성(길이·유한성)을 함께 검증한다.
    """
    vals = np.asarray(
        [pt["v"] for pt in (equity_curve or []) if pt.get("v") is not None],
        dtype=float,
    )
    if vals.size < 2:
        return np.asarray([], dtype=float)
    return np.diff(vals) / vals[:-1]


def compute_deflated_sharpe(
    daily_returns,
    sharpe_annual: float,
    N: float,
    V_daily: float,
    ann: int = 252,
) -> dict:
    """DSR·PSR0 을 계산하는 순수함수(방법론 의사코드 그대로).

    :param daily_returns: 일별 단순수익률 시퀀스(왜도·첨도·n 산출용).
    :param sharpe_annual: 연율화 샤프비(Backtest.result["sharpe"]). 내부에서 √ann 로
        일별 환산한다.
    :param N: **유효** 시행 횟수(N_eff). N<2 면 DSR 미정의(dsr=None) 로 반환하되
        PSR0 는 계산한다.
    :param V_daily: 시행 간 일별 샤프비의 표본분산. 0 이면 SR0=0 → DSR=PSR0.
    :param ann: 연율화 거래일수(기본 252).
    :return: {DSR, PSR0, SR_daily, SR0_daily, N, skew, kurt, n}. DSR 은 None 가능.
    :raises ValueError: 관측치가 MIN_OBS 미만이거나 수익률이 비유한/무변동이라
        통계를 낼 수 없을 때.
    """
    r = np.asarray(daily_returns, dtype=float)
    n = r.size
    if n < MIN_OBS:
        raise ValueError(f"표본 부족: 관측치 {n} < 최소 {MIN_OBS}")
    if not np.all(np.isfinite(r)):
        raise ValueError("수익률에 비유한 값(NaN/inf)이 있어 계산할 수 없습니다.")

    SR = sharpe_annual / math.sqrt(ann)
    g3 = float(skew(r, bias=False))
    g4 = float(kurtosis(r, fisher=False, bias=False))

    # PSR 분모(비정규성 보정 표준오차). 음수면 왜도·첨도 조합이 극단이라 계산 불가.
    se_sq = 1.0 - g3 * SR + ((g4 - 1.0) / 4.0) * SR * SR
    if se_sq <= 0:
        raise ValueError(
            f"PSR 표준오차 제곱이 비양수({se_sq:.4g}) — 극단적 왜도/첨도로 계산 불가."
        )
    se = math.sqrt(se_sq)

    root_nm1 = math.sqrt(n - 1)
    psr0 = float(norm.cdf(SR * root_nm1 / se))

    # 기대 최대 샤프비(다중시행 벤치마크). N<2 면 Φ⁻¹(1−1/N) 이 발산 → DSR 미정의.
    if N is None or N < 2:
        return {
            "DSR": None,
            "PSR0": psr0,
            "SR_daily": SR,
            "SR0_daily": None,
            "N": None if N is None else float(N),
            "skew": g3,
            "kurt": g4,
            "n": n,
        }

    SR0 = math.sqrt(max(V_daily, 0.0)) * (
        (1.0 - EULER) * float(norm.ppf(1.0 - 1.0 / N))
        + EULER * float(norm.ppf(1.0 - 1.0 / (N * math.e)))
    )
    dsr = float(norm.cdf((SR - SR0) * root_nm1 / se))

    return {
        "DSR": dsr,
        "PSR0": psr0,
        "SR_daily": SR,
        "SR0_daily": SR0,
        "N": float(N),
        "skew": g3,
        "kurt": g4,
        "n": n,
    }


def _mean_pairwise_corr(returns_list: list[np.ndarray]) -> float:
    """동질 시행들의 일별수익률 시계열 간 평균 쌍상관 ρ̄ 를 구한다(N_eff 보정용).

    길이가 다르면 앞에서부터 공통 길이로 절단해 비교한다(동일 기간이면 길이가
    같다 — 간이식). 분산 0·비유한 상관은 제외한다. 유효 쌍이 없으면 0.0(보정
    없음 → N_eff=N, 가장 보수적).
    """
    arrs = [
        np.asarray(r, dtype=float)
        for r in returns_list
        if r is not None and np.asarray(r).size >= 2
    ]
    corrs: list[float] = []
    for i in range(len(arrs)):
        for j in range(i + 1, len(arrs)):
            a, b = arrs[i], arrs[j]
            m = min(a.size, b.size)
            if m < 2:
                continue
            a2, b2 = a[:m], b[:m]
            if not (np.all(np.isfinite(a2)) and np.all(np.isfinite(b2))):
                continue
            if a2.std() == 0 or b2.std() == 0:
                continue
            c = float(np.corrcoef(a2, b2)[0, 1])
            if math.isfinite(c):
                corrs.append(c)
    return float(np.mean(corrs)) if corrs else 0.0


def estimate_trial_stats(
    sharpes_annual: list[float],
    returns_list: list[np.ndarray],
    ann: int = 252,
) -> dict:
    """동질 시행 집합에서 N·V_daily·ρ̄·N_eff 를 추정한다.

    :param sharpes_annual: 동질 집합 각 행의 연율화 샤프비(현재 대상 포함).
    :param returns_list: 동질 집합 각 행의 일별수익률 배열(N_eff 상관 보정용).
    :param ann: 연율화 거래일수.
    :return: {N, V_daily, rho_bar, N_eff, v_low_confidence}.
        N_eff 는 floor·[2,N] 클램프. N<2 면 N_eff 도 <2(DSR 미정의 신호).
    """
    N = len(sharpes_annual)
    sr_daily = np.asarray(
        [s / math.sqrt(ann) for s in sharpes_annual if s is not None],
        dtype=float,
    )
    # 표본분산(ddof=1). 표본이 1개 이하면 정의 불가 → 0.0.
    V_daily = float(sr_daily.var(ddof=1)) if sr_daily.size >= 2 else 0.0

    rho_bar = _mean_pairwise_corr(returns_list)
    # 음의 상관은 N_eff>N(불변식 위반)을 유발 → 0 클램프. 1 근접은 N_eff→1.
    rho_c = min(max(rho_bar, 0.0), 0.9999)

    if N >= 2:
        n_eff_raw = N / (1.0 + (N - 1) * rho_c)
        n_eff = int(math.floor(n_eff_raw))
        n_eff = min(n_eff, N)  # N_eff ≤ N 불변식(수치 안전).
    else:
        n_eff = N  # 0 또는 1 → DSR 미정의로 흘러간다.

    return {
        "N": N,
        "V_daily": V_daily,
        "rho_bar": rho_bar,
        "N_eff": n_eff,
        "v_low_confidence": N < 5,
    }


def analyze_backtest_dsr(
    target_result: dict,
    homogeneous_results: list[dict],
    ann: int = 252,
) -> dict:
    """대상 백테스트 1건에 대해 DSR 전체 분석을 수행한다(동질 집합 기반).

    :param target_result: 분석 대상 Backtest.result(JSONB). equity_curve·sharpe 필요.
    :param homogeneous_results: 동질 집합 전체의 Backtest.result 리스트(대상 포함).
        기간 동일성으로 필터된 상태여야 한다(필터는 라우트 책임).
    :param ann: 연율화 거래일수.
    :return: DSR·PSR0·등급·N/N_eff/V·진단 플래그를 담은 응답 dict.
    """
    target_returns = returns_from_equity_curve(target_result.get("equity_curve") or [])
    sharpe_annual = target_result.get("sharpe")

    # 표본/데이터 품질 게이트.
    if sharpe_annual is None:
        return {
            "status": "unavailable",
            "reason": "대상 백테스트에 샤프비가 없습니다.",
            "grade": "insufficient_trials",
        }
    if target_returns.size < MIN_OBS or not np.all(np.isfinite(target_returns)):
        return {
            "status": "unavailable",
            "reason": (
                f"표본 부족 또는 비유한 수익률(관측치 {int(target_returns.size)}, "
                f"최소 {MIN_OBS})."
            ),
            "grade": "insufficient_trials",
            "n": int(target_returns.size),
        }

    sharpe_annual = float(sharpe_annual)

    # 동질 집합에서 N·V·N_eff 추정.
    sharpes = [
        r.get("sharpe") for r in homogeneous_results if r.get("sharpe") is not None
    ]
    returns_list = [
        returns_from_equity_curve(r.get("equity_curve") or [])
        for r in homogeneous_results
    ]
    stats = estimate_trial_stats(sharpes, returns_list, ann=ann)

    core = compute_deflated_sharpe(
        target_returns,
        sharpe_annual,
        N=stats["N_eff"],
        V_daily=stats["V_daily"],
        ann=ann,
    )

    dsr = core["DSR"]
    resp = {
        "status": "ok" if dsr is not None else "insufficient_trials",
        "grade": grade_dsr(dsr),
        "dsr": dsr,
        "psr0": core["PSR0"],
        "sharpe_annual": sharpe_annual,
        "sharpe_daily": core["SR_daily"],
        "sr0_daily": core["SR0_daily"],
        "skew": core["skew"],
        "kurt": core["kurt"],
        "n_obs": core["n"],
        "N": stats["N"],
        "N_eff": stats["N_eff"],
        "rho_bar": stats["rho_bar"],
        "V_daily": stats["V_daily"],
        "v_low_confidence": stats["v_low_confidence"],
        "ann": ann,
    }
    if dsr is None:
        resp["reason"] = (
            f"유효 시행 수 N_eff={stats['N_eff']}<2 (동질 백테스트 이력 부족) — "
            "DSR 미정의. PSR0(다중검정 미보정 유의성)만 제공합니다."
        )
    return resp
