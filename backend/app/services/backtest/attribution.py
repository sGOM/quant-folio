"""리밸런싱 백테스트 성과귀속·위험조정 지표.

- 팩터 IC/IR·롱숏 귀속(P1-1): 리밸런싱 스냅샷의 팩터 점수와 다음 구간 실현수익으로
  팩터별 예측력(IC)·일관성(IR)·롱숏수익을 산출한다.
- 위험조정·벤치마크 상대 지표: Sharpe/Sortino/beta/alpha/IR/tracking error 등.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.services._num import _safe

# 팩터 IC/IR·성과귀속(P1-1)에서 다루는 팩터 점수 컬럼(개별 5팩터 + 종합).
_FACTOR_SCORE_COLS = (
    "score_momentum", "score_value", "score_lowvol",
    "score_quality", "score_growth", "score",
)


def _factor_attribution(
    snapshots: list[tuple[pd.Timestamp, pd.DataFrame]], panel: pd.DataFrame
) -> dict[str, dict[str, float | int | None]]:
    """리밸런싱 스냅샷(팩터 점수)과 다음 구간 순수 수익률로 팩터별 IC·IR·롱숏수익을 산출한다.

    각 인접 스냅샷 구간 [d_i, d_{i+1}] 에서 후보풀 종목의 forward return(종가 기준)을 구하고,
    팩터별로:
      - IC_i: 팩터 점수와 forward return 의 순위상관(Spearman). 예측력의 부호·강도.
      - LS_i: 팩터 상위 1/3 평균수익 − 하위 1/3 평균수익(팩터 모방 롱숏 포트 수익).
    을 계산한 뒤 구간에 걸쳐 집계한다:
      - ic_mean: 평균 IC(예측력)
      - ic_ir: IC IR = mean(IC)/std(IC) × √(연간 리밸런싱 횟수). 예측의 일관성.
      - ic_hit: IC>0 비율(방향 적중률)
      - ls_return: 롱숏 누적수익(∏(1+LS_i)−1) — 이 구간 팩터가 실제로 값을 했는지(귀속).
      - n: 유효 구간 수.

    미래참조 없음: 각 IC 는 d_i 시점 점수 vs d_i→d_{i+1} 실현수익으로, 결정 시점 정보만 쓴다.
    """
    out: dict[str, dict[str, float | int | None]] = {}
    if len(snapshots) < 3:
        return out  # IR 안정성 위해 최소 3구간(스냅샷 4개) 필요

    idx = panel.index
    # 연율화 계수: 스냅샷 간 평균 거래일 간격 → 연 리밸런싱 횟수(√ 적용).
    pos = []
    for d, _ in snapshots:
        loc = idx.searchsorted(d)
        pos.append(int(loc))
    gaps = [pos[i + 1] - pos[i] for i in range(len(pos) - 1) if pos[i + 1] > pos[i]]
    avg_gap = float(np.mean(gaps)) if gaps else 21.0
    periods_per_year = 252.0 / avg_gap if avg_gap > 0 else 12.0
    ann = math.sqrt(periods_per_year)

    per_factor_ic: dict[str, list[float]] = {c: [] for c in _FACTOR_SCORE_COLS}
    per_factor_ls: dict[str, list[float]] = {c: [] for c in _FACTOR_SCORE_COLS}

    for i in range(len(snapshots) - 1):
        d0, frame = snapshots[i]
        d1, _ = snapshots[i + 1]
        if d0 not in panel.index or d1 not in panel.index:
            continue
        p0 = panel.loc[d0]
        p1 = panel.loc[d1]
        syms = [s for s in frame.index if s in panel.columns]
        if len(syms) < 5:
            continue
        base = p0.reindex(syms).astype(float)
        fwd = (p1.reindex(syms).astype(float) / base) - 1.0
        fwd = fwd.replace([np.inf, -np.inf], np.nan)
        valid = fwd.notna() & (base > 0)
        fwd = fwd[valid]
        if len(fwd) < 5:
            continue
        for col in _FACTOR_SCORE_COLS:
            if col not in frame.columns:
                continue
            sc = frame[col].reindex(fwd.index).astype(float)
            pair = pd.concat([sc, fwd], axis=1).dropna()
            if len(pair) < 5 or pair.iloc[:, 0].nunique() < 3:
                continue
            ic = pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman")
            if ic == ic:  # NaN 아님
                per_factor_ic[col].append(float(ic))
            # 롱숏(터셔일): 팩터 상위 1/3 − 하위 1/3 평균 forward return.
            k = len(pair) // 3
            if k >= 1:
                ranked = pair.sort_values(pair.columns[0], ascending=False)
                top = ranked.iloc[:k, 1].mean()
                bot = ranked.iloc[-k:, 1].mean()
                if top == top and bot == bot:
                    per_factor_ls[col].append(float(top - bot))

    for col in _FACTOR_SCORE_COLS:
        ics = per_factor_ic[col]
        lss = per_factor_ls[col]
        if len(ics) < 3:
            continue
        arr = np.array(ics, dtype=float)
        sd = float(arr.std(ddof=1))
        out[col] = {
            "ic_mean": _safe(float(arr.mean())),
            "ic_ir": _safe(float(arr.mean()) / sd * ann) if sd > 0 else None,
            "ic_hit": _safe(float((arr > 0).mean())),
            "ls_return": _safe(float(np.prod([1.0 + x for x in lss]) - 1.0)) if lss else None,
            "n": len(ics),
        }
    return out


def _risk_adjusted_metrics(
    rets: np.ndarray, bench_rets: np.ndarray | None, rf_annual: float
) -> dict[str, float | None]:
    """일간 수익률로 위험조정·벤치마크 상대 지표를 산출한다(연율화 √252).

    - Sharpe/Sortino: 무위험수익률(rf_annual, 연) 초과분 기준. Sortino 는 하방편차만 쓴다.
    - beta/alpha: 포트 일간수익을 벤치마크 일간수익에 회귀(OLS 기울기=beta, Jensen alpha 연율).
    - information_ratio/tracking_error: 초과수익(포트−벤치) 기준.
    - benchmark_return/excess_return: 구간 누적 벤치마크 수익률·초과.

    NaN 인 벤치마크 일자는 짝지어 제거한다. 벤치마크가 없으면 관련 키는 None.
    """
    out: dict[str, float | None] = {
        "sharpe": None, "sortino": None, "beta": None, "alpha": None,
        "information_ratio": None, "tracking_error": None,
        "benchmark_return": None, "excess_return": None,
    }
    rf_daily = (1.0 + rf_annual) ** (1.0 / 252.0) - 1.0
    ann = math.sqrt(252.0)

    if len(rets) > 1:
        std = rets.std(ddof=1)
        if std > 0:
            out["sharpe"] = _safe((rets.mean() - rf_daily) / std * ann)
        downside = np.minimum(rets - rf_daily, 0.0)
        dstd = math.sqrt(float((downside ** 2).mean()))  # 하방편차(목표수익 rf 기준)
        if dstd > 0:
            out["sortino"] = _safe((rets.mean() - rf_daily) / dstd * ann)

    if bench_rets is None:
        return out

    mask = ~np.isnan(bench_rets)
    p = rets[mask]
    b = bench_rets[mask]
    if len(b) > 1:
        out["benchmark_return"] = _safe(float(np.prod(1.0 + b) - 1.0))
        bvar = b.var(ddof=1)
        if bvar > 0:
            beta = float(np.cov(p, b, ddof=1)[0, 1] / bvar)
            out["beta"] = _safe(beta)
            out["alpha"] = _safe(((p.mean() - rf_daily) - beta * (b.mean() - rf_daily)) * 252.0)
        active = p - b
        astd = active.std(ddof=1)
        if astd > 0:
            out["tracking_error"] = _safe(astd * ann)
            out["information_ratio"] = _safe(active.mean() / astd * ann)
    return out
