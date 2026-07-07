"""id=23 균형 멀티팩터 walk-forward 가중치 튜닝(과최적화 점검).

패널·펀더멘털·레짐·PIT pool_provider 를 한 번만 조립하고, 팩터 가중치 조합과 연도
슬라이스만 바꿔 run_rebalance_backtest 를 반복한다. 각 OOS 연도에 대해 직전 연도(IS)
best 조합의 OOS 성과를 base(id=23) 와 비교해, 특정 분할에 과최적화됐는지 본다.
"""
from __future__ import annotations

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

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

# (value, growth, lowvol, quality, momentum) — 합 1.0
COMBOS = {
    "base(id23) v3q3g2m2": {"value": 0.3, "growth": 0.2, "lowvol": 0.0, "quality": 0.3, "momentum": 0.2},
    "value-heavy v4q3g2m1": {"value": 0.4, "growth": 0.2, "lowvol": 0.0, "quality": 0.3, "momentum": 0.1},
    "quality-heavy v2q4g2m2": {"value": 0.2, "growth": 0.2, "lowvol": 0.0, "quality": 0.4, "momentum": 0.2},
    "growth-heavy v3q2g3m2": {"value": 0.3, "growth": 0.3, "lowvol": 0.0, "quality": 0.2, "momentum": 0.2},
    "equal v25q25g25m25": {"value": 0.25, "growth": 0.25, "lowvol": 0.0, "quality": 0.25, "momentum": 0.25},
    "no-mom v35q35g3": {"value": 0.35, "growth": 0.3, "lowvol": 0.0, "quality": 0.35, "momentum": 0.0},
    "with-lowvol v25q25g2m15lv15": {"value": 0.25, "growth": 0.2, "lowvol": 0.15, "quality": 0.25, "momentum": 0.15},
}

# 2-fold: 각 반기가 약세+회복을 함께 포함하도록 분할(연 단위는 레짐 현금화로 플랫해져 무의미).
WINDOWS = [
    ("H1(21.1-23.6)", date(2021, 1, 1), date(2023, 6, 30)),
    ("H2(23.7-25.6)", date(2023, 7, 1), date(2025, 6, 30)),
]
FULL = ("FULL(21.1-25.6)", date(2021, 1, 1), date(2025, 6, 30))


async def main() -> None:
    async with AsyncSessionLocal() as db:
        base = await db.scalar(select(Strategy).where(Strategy.id == 23))
        config = dict(base.config)

    # PIT 후보풀(월별 멤버십) — 한 번만.
    pit_union, pool_provider = _build_pit_pool(config, WARMUP_START, PERIOD_END)
    universe = pit_union if pit_union is not None else list(config.get("universe", []))
    print(f"PIT union universe: {len(universe)}종목", flush=True)

    # 종가 패널 — 한 번만 적재.
    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(PERIOD_END, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for i, sym in enumerate(universe):
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
            if (i + 1) % 50 == 0:
                print(f"  ...패널 적재 {i + 1}/{len(universe)}", flush=True)
    panel = pd.DataFrame(columns)
    print(f"패널 완성: {panel.shape}", flush=True)

    # 레짐 기준지수 — 한 번만.
    rf = config.get("regime_filter") or {}
    ticker = _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, PERIOD_END, ticker)

    def _n(x):  # None → nan(포맷·비교 안전)
        return float("nan") if x is None else float(x)

    def run(weights, s, e, *, regime=True):
        cfg = {**config, "selection": {**config["selection"], "factor_weights": weights}}
        r = run_rebalance_backtest(
            panel, cfg, _to_dt(s), _to_dt(e, end=True),
            _fundamentals_provider, regime_series if regime else None, pool_provider,
        )
        return _n(r.get("total_return")), _n(r.get("sharpe")), _n(r.get("mdd"))

    def _fmt(t):  # total_return·mdd 는 분수(0.4=40%) → ×100 표기
        return f"ret={t[0] * 100:+.1f}% sharpe={t[1]:.2f} mdd={t[2] * 100:.1f}%"

    def _shp(t):  # Sharpe(nan→-inf) 비교키
        return t[1] if t[1] == t[1] else float("-inf")

    BASE = "base(id23) v3q3g2m2"

    # (A) 실제 config(레짐 ON): 반기 2-fold + 전체 구간.
    print("\n=== (A) 레짐 ON(실제 config) — 반기 2-fold + 전체 ===", flush=True)
    grid: dict[str, dict[str, tuple]] = {}
    for name, weights in COMBOS.items():
        grid[name] = {}
        for wlabel, s, e in [*WINDOWS, FULL]:
            t = await asyncio.to_thread(run, weights, s, e)
            grid[name][wlabel] = t
            print(f"[{name:26}] {wlabel}: {_fmt(t)}", flush=True)

    # (B) 레짐 OFF(팩터 효과 격리): 전체 구간만.
    print("\n=== (B) 레짐 OFF(팩터 격리) — 전체 구간 ===", flush=True)
    off: dict[str, tuple] = {}
    for name, weights in COMBOS.items():
        t = await asyncio.to_thread(run, weights, FULL[1], FULL[2], regime=False)
        off[name] = t
        print(f"[{name:26}] FULL(무레짐): {_fmt(t)}", flush=True)

    # Walk-forward 2-fold: 한 반기 best(Sharpe) → 다른 반기(OOS) 성과 vs base.
    w1, w2 = WINDOWS[0][0], WINDOWS[1][0]
    print("\n=== Walk-forward 2-fold (IS best → OOS vs base) ===", flush=True)
    for is_w, oos_w in [(w1, w2), (w2, w1)]:
        bn = max(COMBOS, key=lambda n: _shp(grid[n][is_w]))
        print(
            f"IS={is_w} best='{bn}' (IS {_fmt(grid[bn][is_w])})\n"
            f"   → OOS={oos_w}: best {_fmt(grid[bn][oos_w])} | base {_fmt(grid[BASE][oos_w])}",
            flush=True,
        )

    # 요약: 전체 구간 Sharpe 순위(레짐 ON) + 무레짐 대조.
    print("\n=== 전체 구간 Sharpe 순위 (레짐 ON / OFF) ===", flush=True)
    fl = FULL[0]
    for n in sorted(COMBOS, key=lambda n: -_shp(grid[n][fl])):
        mark = "  ←base" if n == BASE else ""
        print(f"  ON {n:26} {_fmt(grid[n][fl])} | OFF {_fmt(off[n])}{mark}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
