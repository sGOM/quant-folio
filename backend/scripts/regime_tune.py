"""id=23 레짐필터 파라미터 튜닝 — 상승 회수 vs MDD 트레이드오프 탐색.

팩터 가중치는 id=23(v3q3g2m2) 고정. regime_filter 파라미터(ma_period/exit_buffer/
reentry_buffer)만 바꿔가며 반기 2-fold + 전체 구간 성과를 비교한다. 패널·지수시리즈·
PIT 풀은 한 번만 조립한다. total_return·mdd 는 분수 → ×100 표기.
"""
from __future__ import annotations

import asyncio
import os
import sys

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

# regime_filter 후보(팩터 가중치는 id=23 고정). None=오버레이 없음(상승 상한 참조).
REGIMES = {
    "OFF(무레짐)": None,
    "cur ma200 x08 r02": {"enabled": True, "index": "KOSPI", "ma_period": 200, "exit_buffer_pct": 0.08, "reentry_buffer_pct": 0.02},
    "ma200 x08 r00": {"enabled": True, "index": "KOSPI", "ma_period": 200, "exit_buffer_pct": 0.08, "reentry_buffer_pct": 0.00},
    "ma200 x10 r00": {"enabled": True, "index": "KOSPI", "ma_period": 200, "exit_buffer_pct": 0.10, "reentry_buffer_pct": 0.00},
    "ma200 x12 r02": {"enabled": True, "index": "KOSPI", "ma_period": 200, "exit_buffer_pct": 0.12, "reentry_buffer_pct": 0.02},
    "ma200 x15 r05": {"enabled": True, "index": "KOSPI", "ma_period": 200, "exit_buffer_pct": 0.15, "reentry_buffer_pct": 0.05},
    "ma150 x08 r02": {"enabled": True, "index": "KOSPI", "ma_period": 150, "exit_buffer_pct": 0.08, "reentry_buffer_pct": 0.02},
    "ma120 x10 r02": {"enabled": True, "index": "KOSPI", "ma_period": 120, "exit_buffer_pct": 0.10, "reentry_buffer_pct": 0.02},
}

WINDOWS = [
    ("H1(21.1-23.6)", date(2021, 1, 1), date(2023, 6, 30)),
    ("H2(23.7-25.6)", date(2023, 7, 1), date(2025, 6, 30)),
]
FULL = ("FULL(21.1-25.6)", date(2021, 1, 1), date(2025, 6, 30))
CUR = "cur ma200 x08 r02"


async def main() -> None:
    async with AsyncSessionLocal() as db:
        base = await db.scalar(select(Strategy).where(Strategy.id == 23))
        config = dict(base.config)

    pit_union, pool_provider = _build_pit_pool(config, WARMUP_START, PERIOD_END)
    universe = pit_union if pit_union is not None else list(config.get("universe", []))
    print(f"PIT union universe: {len(universe)}종목", flush=True)

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
    panel = pd.DataFrame(columns)
    print(f"패널 완성: {panel.shape}", flush=True)

    rf0 = config.get("regime_filter") or {}
    ticker = _REGIME_INDEX_TICKER.get(rf0.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, PERIOD_END, ticker)

    def run(regime, s, e):
        cfg = {**config, "regime_filter": regime}
        r = run_rebalance_backtest(
            panel, cfg, _to_dt(s), _to_dt(e, end=True),
            _fundamentals_provider, regime_series, pool_provider,
        )

        def _n(x):
            return float("nan") if x is None else float(x)

        return _n(r.get("total_return")), _n(r.get("sharpe")), _n(r.get("mdd"))

    def _fmt(t):
        return f"ret={t[0] * 100:+6.1f}% sharpe={t[1]:5.2f} mdd={t[2] * 100:6.1f}%"

    def _shp(t):
        return t[1] if t[1] == t[1] else float("-inf")

    grid: dict[str, dict[str, tuple]] = {}
    for name, regime in REGIMES.items():
        grid[name] = {}
        for wlabel, s, e in [*WINDOWS, FULL]:
            t = await asyncio.to_thread(run, regime, s, e)
            grid[name][wlabel] = t
            print(f"[{name:20}] {wlabel}: {_fmt(t)}", flush=True)

    fl = FULL[0]
    print("\n=== 전체 구간 요약(수익 vs MDD vs Sharpe) — id=23 팩터 고정 ===", flush=True)
    for name in REGIMES:
        mark = "  ←현행" if name == CUR else ("  (상한참조)" if REGIMES[name] is None else "")
        print(f"  {name:20} {_fmt(grid[name][fl])}{mark}", flush=True)

    w1, w2 = WINDOWS[0][0], WINDOWS[1][0]
    print("\n=== Walk-forward 2-fold (IS best Sharpe → OOS vs 현행) ===", flush=True)
    # 무레짐(OFF)은 참조용이라 walk-forward 후보에서 제외.
    cands = [n for n in REGIMES if REGIMES[n] is not None]
    for is_w, oos_w in [(w1, w2), (w2, w1)]:
        bn = max(cands, key=lambda n: _shp(grid[n][is_w]))
        print(
            f"IS={is_w} best='{bn}' (IS {_fmt(grid[bn][is_w])})\n"
            f"   → OOS={oos_w}: best {_fmt(grid[bn][oos_w])} | 현행 {_fmt(grid[CUR][oos_w])}",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
