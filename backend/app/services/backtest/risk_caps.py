"""리밸런싱 백테스트 리스크 레이어 — 집중 한도·변동성 타겟팅(P1-2).

목표비중에 단일 종목 집중 한도와 포트폴리오 변동성 타겟팅을 적용한다.
MDD 킬스위치는 일별 자산가치가 필요하므로 여기가 아니라 시뮬레이션 루프에서 처리한다.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _cap_position_weights(targets: dict[str, float], cap: float) -> dict[str, float]:
    """단일 종목 목표비중을 cap 으로 제한하고 초과분을 상한 미만 종목에 비례 재분배한다.

    재분배로 다시 상한을 넘으면 반복하며, 재분배 여력(상한 미만 종목)이 없으면 남는
    비중은 현금으로 둔다(합<1). n+1 회로 반복을 제한해 종료를 보장한다.
    """
    if cap >= 1.0 or not targets:
        return targets
    w = dict(targets)
    for _ in range(len(w) + 1):
        excess = sum(v - cap for v in w.values() if v > cap + 1e-12)
        if excess <= 1e-12:
            break
        for s in list(w):
            if w[s] > cap:
                w[s] = cap
        under = {s: v for s, v in w.items() if v < cap - 1e-12}
        pool = sum(under.values())
        if pool <= 1e-12:
            break  # 재분배 여력 없음 → 초과분은 현금
        for s in under:
            w[s] += excess * under[s] / pool
    return w


def _portfolio_vol_ann(
    hist: pd.DataFrame, weights: dict[str, float], lookback: int
) -> float | None:
    """목표비중 포트폴리오의 최근 lookback 봉 실현변동성(연율)을 공분산 기반으로 추정한다.

    σ_p = sqrt(wᵀ Σ w × 252). Σ 는 일간수익 공분산(pairwise 결측 대응). 유효 관측이
    부족하거나 분산이 비양수면 None. 미래참조 방지를 위해 hist(=panel.loc[:d]) 만 쓴다.
    """
    syms = [s for s in weights if s in hist.columns and weights[s] > 0]
    if not syms:
        return None
    rets = hist[syms].pct_change().tail(lookback)
    if len(rets) < max(10, lookback // 2):
        return None
    cov = rets.cov().to_numpy(dtype=float)
    w = np.array([weights[s] for s in syms], dtype=float)
    if np.isnan(cov).any():
        cov = np.nan_to_num(cov)  # 잔여 결측(짝없음)은 0 공분산으로
    var_daily = float(w @ cov @ w)
    if not (var_daily > 0):
        return None
    return math.sqrt(var_daily * 252.0)


def _apply_risk_caps(
    targets: dict[str, float], hist: pd.DataFrame, risk: dict
) -> dict[str, float]:
    """목표비중에 리스크 레이어(집중 한도→변동성 타겟팅)를 적용한다(P1-2).

    MDD 킬스위치는 일별 자산가치가 필요하므로 여기가 아니라 시뮬레이션 루프에서 처리한다.
    """
    if not targets or not risk:
        return targets
    cap = risk.get("max_position_pct")
    if cap:
        targets = _cap_position_weights(targets, float(cap))
    tv = risk.get("target_vol")
    if tv:
        vol = _portfolio_vol_ann(hist, targets, int(risk.get("vol_lookback", 20) or 20))
        if vol and vol > 0:
            max_lev = float(risk.get("max_leverage", 1.0) or 1.0)
            scale = min(max_lev, float(tv) / vol)
            if scale < 1.0 - 1e-9:  # 디레버리징(비중 축소)만 적용, 확대는 max_lev 로 캡
                targets = {s: w * scale for s, w in targets.items()}
    return targets
