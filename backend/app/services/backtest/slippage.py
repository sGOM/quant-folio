"""리밸런싱 백테스트 슬리피지 모델.

슬리피지(slippage_bps)는 편도 기준이며, slippage_vol_scale>0 이면 종목별 최근
변동성/중앙값 비로 조정한다. 고정 bps 경로는 호출부가 직접 처리하고, 여기서는
변동성 스케일 맵만 계산한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _vol_slippage_map(
    hist: pd.DataFrame, symbols: list[str], base_frac: float, vol_scale: float
) -> dict[str, float]:
    """종목별 슬리피지(분수) 맵. 최근 20봉 수익률 표준편차/중앙값 비로 base 를 조정한다.

    slip = max(0, base × (1 + vol_scale × (vol/median − 1))). base 나 vol_scale 이 0 이면
    빈 맵을 돌려 호출부가 일괄 base_frac(고정 bps)을 쓰게 한다. 미래참조 방지를 위해
    hist(=panel.loc[:체결일]) 만 사용한다.
    """
    if base_frac <= 0 or vol_scale <= 0:
        return {}
    vols: dict[str, float] = {}
    for s in symbols:
        if s not in hist.columns:
            continue
        r = hist[s].dropna().pct_change().tail(20)
        if len(r) >= 10:
            sd = float(r.std())
            if sd == sd and sd > 0:  # NaN 이 아니고 양수
                vols[s] = sd
    if not vols:
        return {}
    med = float(np.median(list(vols.values())))
    if not (med > 0):
        return {}
    return {
        s: max(0.0, base_frac * (1.0 + vol_scale * (v / med - 1.0)))
        for s, v in vols.items()
    }
