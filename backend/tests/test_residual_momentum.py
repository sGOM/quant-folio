"""잔차(베타·사이즈 조정) 모멘텀 팩터 배선 검증(Blitz·Huij·Martens 2011).

- compute_residual_momentum_panel 이 시장·베타 성분을 회귀로 제거하고 형성창 잔차
  outperformance 를 양(+)으로 반영하는지(상수 드리프트는 alpha 흡수, 최근창 드리프트만 검출).
- 미래참조 차단: as_of 이후 종가를 바꿔도 결과 불변.
- _compute_stock_scores 의 score_residual_momentum 카테고리가 resid_mom↑ 을 상위로
  반영하는지. resid_mom 컬럼 부재 시 score_residual_momentum 전부 0 → 기존 전략 무영향.
- residual_momentum 가중치 0 이면 resid_mom 이 있어도 종합점수 불변(하위호환).
"""
from datetime import date

import numpy as np
import pandas as pd

from app.services import metrics
from app.services.metrics import factors


def _synthetic(seed: int, recent_drift: bool):
    """시장 팩터 + 종목별 베타·잔차로 종가 패널을 만든다.

    recent_drift=True 면 형성창(최근 ~11개월, 최근 1개월 제외)에만 잔차 초과수익을 주입한다.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2017-06-01", "2021-01-15")
    n = len(dates)
    as_of = pd.Timestamp("2021-01-15")
    mkt_ret = rng.normal(0.0004, 0.01, n)
    market = pd.Series(100 * np.cumprod(1 + mkt_ret), index=dates)
    codes = [f"{i:06d}" for i in range(20)]
    betas = rng.uniform(0.5, 1.5, 20)
    strength = rng.normal(0, 0.0015, 20)
    window = (dates <= as_of - pd.DateOffset(months=1)) & (dates > as_of - pd.DateOffset(months=12))
    cols = {}
    for j, c in enumerate(codes):
        eps = rng.normal(0, 0.006, n)
        if recent_drift:
            eps[window] += strength[j]
        cols[c] = pd.Series(100 * np.cumprod(1 + betas[j] * mkt_ret + eps), index=dates)
    return pd.DataFrame(cols), market, codes, pd.Series(strength, index=codes)


def test_residual_momentum_detects_recent_window_outperformance():
    """형성창에만 주입한 잔차 초과수익을 resid_mom 이 강하게(+상관) 검출한다."""
    panel, market, codes, strength = _synthetic(1, recent_drift=True)
    rm = factors.compute_residual_momentum_panel(
        panel, market, date(2021, 1, 15), codes=codes,
        reg_window=36, mom_window=11, skip=1,
    )
    assert len(rm) == len(codes)
    corr = float(rm.corr(strength.reindex(rm.index)))
    assert corr > 0.6  # 시장·베타 제거 후 형성창 잔차 outperformance 를 반영


def test_residual_momentum_absorbs_constant_drift_into_alpha():
    """전 구간 상수 드리프트는 회귀 절편(alpha)이 흡수 → 잔차 모멘텀과 무상관."""
    panel, market, codes, strength = _synthetic(2, recent_drift=False)
    rm = factors.compute_residual_momentum_panel(panel, market, date(2021, 1, 15), codes=codes)
    corr = float(rm.corr(strength.reindex(rm.index)))
    assert abs(corr) < 0.4


def test_residual_momentum_no_lookahead():
    """as_of 이후 종가를 급변시켜도 as_of 컷 이하만 쓰므로 결과가 변하지 않는다."""
    dates = pd.bdate_range("2017-06-01", "2021-03-31")
    n = len(dates)
    rng = np.random.default_rng(3)
    mkt_ret = rng.normal(0.0004, 0.01, n)
    market = pd.Series(100 * np.cumprod(1 + mkt_ret), index=dates)
    codes = [f"{i:06d}" for i in range(10)]
    cols = {c: pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)), index=dates) for c in codes}
    panel = pd.DataFrame(cols)
    as_of = date(2021, 1, 15)
    rm1 = factors.compute_residual_momentum_panel(panel, market, as_of, codes=codes)
    panel2 = panel.copy()
    future = panel2.index > pd.Timestamp(as_of)
    panel2.loc[future] = panel2.loc[future] * 5.0  # as_of 이후만 급변
    rm2 = factors.compute_residual_momentum_panel(panel2, market, as_of, codes=codes)
    a, b = rm1.align(rm2, join="inner")
    assert len(a) == len(rm1)
    assert np.allclose(a.values, b.values, atol=1e-9)


def test_scorer_ranks_high_resid_mom_top():
    """resid_mom 이 클수록 상위 랭킹. 나머지 팩터 중립으로 둬 잔차 모멘텀만 변별."""
    df = pd.DataFrame(index=["A", "B", "C", "D"])
    df["mom_6m"] = [0.1, 0.1, 0.1, 0.1]  # 모멘텀 NaN 게이트 통과용
    df["resid_mom"] = [1.5, 0.4, -0.3, -1.2]
    scored = metrics._compute_stock_scores(df, weights={
        "momentum": 0.0, "value": 0.0, "lowvol": 0.0,
        "quality": 0.0, "growth": 0.0, "flow": 0.0, "residual_momentum": 1.0,
    })
    assert scored.loc["A", "score"] > scored.loc["B", "score"]
    assert scored.loc["B", "score"] > scored.loc["C", "score"]
    assert scored.loc["C", "score"] > scored.loc["D", "score"]


def test_scorer_neutral_without_resid_mom_column():
    """resid_mom 컬럼이 없으면 score_residual_momentum 전부 0 → 기존 전략 무영향."""
    df = pd.DataFrame(index=["A", "B", "C"])
    df["mom_6m"] = [0.2, 0.1, -0.1]
    scored = metrics._compute_stock_scores(df, weights={
        "momentum": 1.0, "value": 0.0, "lowvol": 0.0,
        "quality": 0.0, "growth": 0.0, "flow": 0.0, "residual_momentum": 0.0,
    })
    assert (scored["score_residual_momentum"] == 0.0).all()


def test_scorer_zero_weight_ignores_resid_mom():
    """residual_momentum 가중치 0 이면 resid_mom 이 있어도 종합점수는 원시 모멘텀만 반영."""
    df = pd.DataFrame(index=["A", "B", "C"])
    df["mom_6m"] = [0.2, 0.1, -0.1]
    df["resid_mom"] = [-2.0, 0.0, 2.0]  # 원시 모멘텀과 역방향
    w = {"momentum": 1.0, "value": 0.0, "lowvol": 0.0,
         "quality": 0.0, "growth": 0.0, "flow": 0.0, "residual_momentum": 0.0}
    scored = metrics._compute_stock_scores(df, weights=w)
    # 원시 모멘텀 순위(A>B>C) 그대로 — resid_mom 이 종합점수를 흔들지 않는다.
    assert scored.loc["A", "score"] > scored.loc["B", "score"] > scored.loc["C", "score"]
