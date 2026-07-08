"""신규 전략 후보(P1~P4) PIT KOSPI200 walk-forward 검증 + id=23 대비 상관·결합 분석.

- 반기 2-fold(H1/H2) + 전체 구간 성과를 id=23(균형·레짐튜닝 완료)과 비교.
- FULL 일간수익 상관 + id=23과 50/50 일간리밸런싱 결합 Sharpe/MDD(분산 효과).
- 판정: 후보가 위험조정(Sharpe) 기준으로 두 반기 모두 id=23을 이기면 '독립 채택 후보',
  못 이겨도 상관이 낮고 결합 Sharpe가 개선되면 '분산 편입 후보'.

패널·펀더멘털·레짐·PIT 풀은 한 번만 조립. total_return·mdd 는 분수 → ×100 표기.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import numpy as np
import pandas as pd

from app.api.routes.backtests import (
    _REGIME_INDEX_TICKER,
    _build_pit_pool,
    _fundamentals_provider,
    _load_regime_series,
    _to_dt,
)
from app.core.database import AsyncSessionLocal
from app.models import Strategy
from app.services.backtest.portfolio import run_rebalance_backtest
from app.services.data.loader import get_close_series, load_ohlcv, upsert_price_ticks
from sqlalchemy import select

WARMUP_START = date(2019, 8, 1)
PERIOD_END = date(2025, 6, 30)
WINDOWS = [
    ("H1(21.1-23.6)", date(2021, 1, 1), date(2023, 6, 30)),
    ("H2(23.7-25.6)", date(2023, 7, 1), date(2025, 6, 30)),
]
FULL = ("FULL(21.1-25.6)", date(2021, 1, 1), date(2025, 6, 30))
BASE = "id23(균형·기준)"


def _candidates(cfg23: dict) -> dict[str, dict]:
    """id=23 을 베이스로 후보 config 를 만든다(financial-expert P1~P4)."""
    def base():
        return copy.deepcopy(cfg23)

    cands: dict[str, dict] = {BASE: base()}

    # P1 저변동·퀄리티 방어형 — inverse_vol 비중, pick 60/lookback 120, drift 0.02.
    p1 = base()
    p1["selection"]["factor_weights"] = {"lowvol": 0.4, "quality": 0.3, "value": 0.2, "momentum": 0.1, "growth": 0.0}
    p1["selection"]["universe_rule"] = {"source": "KOSPI200", "type": "momentum", "lookback": 120, "pick": 60}
    p1["selection"]["top_n"] = 20
    p1["weighting"] = "inverse_vol"
    p1["drift_band_pct"] = 0.02
    cands["P1 저변동·퀄리티(invvol)"] = p1

    # P2 밸류·퀄리티 컨트래리언 — 모멘텀 제거, equal, drift 0.02(20종목 5%>2%라 체결).
    p2 = base()
    p2["selection"]["factor_weights"] = {"value": 0.45, "quality": 0.35, "lowvol": 0.2, "momentum": 0.0, "growth": 0.0}
    p2["selection"]["top_n"] = 20
    p2["weighting"] = "equal"
    p2["drift_band_pct"] = 0.02
    cands["P2 밸류·퀄리티 컨트래리언"] = p2

    # P3 퀄리티·성장 GARP — equal, drift 0.02.
    p3 = base()
    p3["selection"]["factor_weights"] = {"quality": 0.35, "growth": 0.3, "value": 0.2, "momentum": 0.15, "lowvol": 0.0}
    p3["selection"]["top_n"] = 20
    p3["weighting"] = "equal"
    p3["drift_band_pct"] = 0.02
    cands["P3 퀄리티·성장 GARP"] = p3

    # P4 월간 리밸런싱 — id=23 팩터/가중치 유지, cadence만 monthly(케이던스 A/B).
    p4 = base()
    p4["cadence"] = "monthly"
    cands["P4 월간(케이던스 A/B)"] = p4

    return cands


async def main() -> None:
    async with AsyncSessionLocal() as db:
        s = await db.scalar(select(Strategy).where(Strategy.id == 23))
        cfg23 = dict(s.config)

    pit_union, pool_provider = _build_pit_pool(cfg23, WARMUP_START, PERIOD_END)
    universe = pit_union if pit_union is not None else list(cfg23.get("universe", []))
    print(f"PIT union universe: {len(universe)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(PERIOD_END, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for sym in universe:
            series = await get_close_series(db, sym, warmup_dt, end_dt)
            if series.empty:
                try:
                    df = await asyncio.to_thread(load_ohlcv, sym, WARMUP_START, PERIOD_END)
                    await upsert_price_ticks(db, sym, df)
                    series = await get_close_series(db, sym, warmup_dt, end_dt)
                except Exception as e:  # noqa: BLE001
                    print(f"  {sym} 적재 실패: {e}", flush=True)
            if not series.empty:
                columns[sym] = series
    panel = pd.DataFrame(columns)
    print(f"패널 완성: {panel.shape}", flush=True)

    rf0 = cfg23.get("regime_filter") or {}
    ticker = _REGIME_INDEX_TICKER.get(rf0.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, PERIOD_END, ticker)

    cands = _candidates(cfg23)

    def run(cfg, s, e):
        r = run_rebalance_backtest(
            panel, cfg, _to_dt(s), _to_dt(e, end=True),
            _fundamentals_provider, regime_series, pool_provider,
        )

        def _n(x):
            return float("nan") if x is None else float(x)

        return (_n(r.get("total_return")), _n(r.get("sharpe")), _n(r.get("mdd"))), r.get("equity_curve") or []

    def _fmt(t):
        return f"ret={t[0] * 100:+7.1f}% sharpe={t[1]:5.2f} mdd={t[2] * 100:6.1f}%"

    def _shp(t):
        return t[1] if t[1] == t[1] else float("-inf")

    # 성과 그리드 + FULL 일간수익 시리즈.
    grid: dict[str, dict[str, tuple]] = {}
    rets_full: dict[str, pd.Series] = {}
    print("\n=== 후보별 성과 (반기 2-fold + 전체, 레짐 ON) ===", flush=True)
    for name, cfg in cands.items():
        grid[name] = {}
        for wlabel, s, e in [*WINDOWS, FULL]:
            t, eq = run(cfg, s, e)
            grid[name][wlabel] = t
            print(f"[{name:26}] {wlabel}: {_fmt(t)}", flush=True)
            if wlabel == FULL[0] and eq:
                ser = pd.Series({pd.Timestamp(p["t"]): float(p["v"]) for p in eq})
                rets_full[name] = ser.pct_change().dropna()

    # id=23 대비 상관 + 50/50 일간리밸런싱 결합 Sharpe/MDD.
    print("\n=== id=23 대비 상관 · 50/50 결합(분산 효과) — FULL ===", flush=True)
    base_r = rets_full.get(BASE)
    for name in cands:
        if name == BASE or name not in rets_full or base_r is None:
            continue
        a, b = rets_full[name].align(base_r, join="inner")
        if len(a) < 30:
            print(f"  {name:26} (구간 부족)", flush=True)
            continue
        corr = float(a.corr(b))
        comb = 0.5 * a + 0.5 * b
        csh = float(comb.mean() / comb.std(ddof=1) * np.sqrt(252)) if comb.std(ddof=1) > 0 else float("nan")
        ceq = (1 + comb).cumprod()
        cmdd = float((ceq / ceq.cummax() - 1).min())
        solo = _shp(grid[name][FULL[0]])
        print(
            f"  {name:26} corr={corr:+.2f} | 결합Sharpe={csh:.2f} (단독 {solo:.2f}, id23 {_shp(grid[BASE][FULL[0]]):.2f}) 결합MDD={cmdd * 100:.1f}%",
            flush=True,
        )

    # Walk-forward 판정: 각 후보가 두 반기 모두 id=23을 Sharpe로 이기는가.
    print("\n=== 판정: 두 반기 모두 id=23 Sharpe 초과? (독립 채택 기준) ===", flush=True)
    w1, w2 = WINDOWS[0][0], WINDOWS[1][0]
    for name in cands:
        if name == BASE:
            continue
        beat1 = _shp(grid[name][w1]) > _shp(grid[BASE][w1])
        beat2 = _shp(grid[name][w2]) > _shp(grid[BASE][w2])
        verdict = "채택후보(양 반기 우위)" if (beat1 and beat2) else ("일부우위" if (beat1 or beat2) else "열위")
        print(
            f"  {name:26} H1 {_shp(grid[name][w1]):.2f}v{_shp(grid[BASE][w1]):.2f} "
            f"H2 {_shp(grid[name][w2]):.2f}v{_shp(grid[BASE][w2]):.2f} → {verdict}",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
