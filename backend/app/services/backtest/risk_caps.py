"""리밸런싱 백테스트 리스크 레이어 — 집중 한도(종목·섹터)·변동성 타겟팅(P1-2).

목표비중에 단일 종목 집중 한도, 섹터(업종) 집중 한도, 포트폴리오 변동성 타겟팅을 적용한다.
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


def _cap_sector_weights(
    targets: dict[str, float], sector_map: dict[str, str], cap: float
) -> dict[str, float]:
    """섹터(업종) 합산 목표비중을 cap 으로 제한하고 초과분을 여력 있는 종목에 재분배한다.

    _cap_position_weights 의 섹터판. 종목이 아니라 '업종' 단위로 비중을 집계·캡핑한다.
    한도를 넘긴 섹터의 종목들은 섹터 내 원비중에 비례해 등비율 축소하고, 잘라낸 초과분은
    '한도에 여유가 있는 섹터'의 종목들에 (그 종목의 현재 비중에 비례해) 재분배한다.
    재분배로 다른 섹터가 다시 상한을 넘으면 반복하며(n+1 회 상한으로 종료 보장), 받아줄
    여력이 없으면 남는 비중은 현금으로 둔다(합<1).

    섹터를 알 수 없는 종목(sector_map 에 없음)은 각자 독립 섹터(코드 자체를 키로)로 취급해
    캡의 영향을 받지 않되(단일 종목이라 대개 cap 미만), 재분배 대상(여력 섹터)에는 포함된다.
    """
    if cap >= 1.0 or not targets:
        return targets

    def sec_of(code: str) -> str:
        # 미분류 종목은 자기 자신을 섹터로 둬 다른 종목과 합산되지 않게 한다.
        return sector_map.get(code) or f"__unclassified__{code}"

    w = dict(targets)
    for _ in range(len(w) + 1):
        # 섹터별 합산 비중.
        sector_tot: dict[str, float] = {}
        for code, v in w.items():
            sector_tot[sec_of(code)] = sector_tot.get(sec_of(code), 0.0) + v
        excess = sum(t - cap for t in sector_tot.values() if t > cap + 1e-12)
        if excess <= 1e-12:
            break
        # 1) 상한 초과 섹터의 종목들을 섹터 내 비례로 등비율 축소(섹터 합을 cap 으로).
        for code in list(w):
            s = sec_of(code)
            tot = sector_tot[s]
            if tot > cap + 1e-12 and tot > 0:
                w[code] *= cap / tot
        # 2) 초과분을 여력 섹터(합<cap)의 종목들에 그 종목 현재 비중에 비례해 재분배.
        under_codes = {
            code: w[code]
            for code in w
            if sector_tot[sec_of(code)] < cap - 1e-12 and w[code] > 0
        }
        pool = sum(under_codes.values())
        if pool <= 1e-12:
            break  # 재분배 여력 없음 → 초과분은 현금
        # 재분배는 섹터가 cap 을 넘지 않는 선까지만 흡수 가능하므로, 넘치면 다음 반복이
        # 그 섹터를 다시 캡핑한다(수렴). 여기서는 단순 비례 분배 후 반복에 맡긴다.
        for code, cw in under_codes.items():
            w[code] += excess * cw / pool
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
    targets: dict[str, float],
    hist: pd.DataFrame,
    risk: dict,
    sector_map: dict[str, str] | None = None,
) -> dict[str, float]:
    """목표비중에 리스크 레이어(종목 집중 한도→섹터 집중 한도→변동성 타겟팅)를 적용한다.

    집중 한도 계열(종목·섹터)을 먼저 적용해 쏠림을 눌러 놓은 뒤 변동성 타겟팅으로 총투자
    비중을 스케일한다. MDD 킬스위치는 일별 자산가치가 필요해 시뮬레이션 루프에서 처리한다.

    :param sector_map: {종목코드: 업종명}. max_sector_pct 적용에 필요(섹터 한도가 설정돼도
        맵이 비면 섹터 캡은 건너뛴다 — 소스 미확보 시 조용히 미적용 폴백).
    """
    if not targets or not risk:
        return targets
    cap = risk.get("max_position_pct")
    if cap:
        targets = _cap_position_weights(targets, float(cap))
    scap = risk.get("max_sector_pct")
    if scap and sector_map:
        targets = _cap_sector_weights(targets, sector_map, float(scap))
    tv = risk.get("target_vol")
    if tv:
        vol = _portfolio_vol_ann(hist, targets, int(risk.get("vol_lookback", 20) or 20))
        if vol and vol > 0:
            max_lev = float(risk.get("max_leverage", 1.0) or 1.0)
            scale = min(max_lev, float(tv) / vol)
            if scale < 1.0 - 1e-9:  # 디레버리징(비중 축소)만 적용, 확대는 max_lev 로 캡
                targets = {s: w * scale for s, w in targets.items()}
    return targets
