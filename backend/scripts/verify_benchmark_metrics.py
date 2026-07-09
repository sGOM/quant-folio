"""P0-2 벤치마크 상대성과·위험조정지표 검증: id=23 을 KOSPI200 대비 재백테스트.

  (A) rf=0,   벤치마크 KOSPI200 → alpha/beta/IR/TE/excess 산출 확인
  (B) rf=3%,  벤치마크 KOSPI200 → 무위험수익률 반영 Sharpe/Sortino 변화 확인

패널·펀더멘털·레짐·PIT·벤치마크(KOSPI200)를 한 번만 조립한다.
"""
from __future__ import annotations

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import pandas as pd

from app.api.routes.backtests import (
    _BENCHMARK_TICKER,
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
SIM_START = date(2021, 1, 1)
SIM_END = date(2025, 6, 30)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        base = await db.scalar(select(Strategy).where(Strategy.id == 23))
        config = dict(base.config)

    pit_union, pool_provider = _build_pit_pool(config, WARMUP_START, SIM_END)
    universe = pit_union if pit_union is not None else list(config.get("universe", []))
    print(f"PIT union universe: {len(universe)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(SIM_END, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for i, sym in enumerate(universe):
            series = await get_close_series(db, sym, warmup_dt, end_dt)
            if series.empty:
                try:
                    df = await asyncio.to_thread(load_ohlcv, sym, WARMUP_START, SIM_END)
                    await upsert_price_ticks(db, sym, df)
                    series = await get_close_series(db, sym, warmup_dt, end_dt)
                except Exception as e:  # noqa: BLE001
                    print(f"  {sym} 적재 실패: {e}", flush=True)
            if not series.empty:
                columns[sym] = series
    panel = pd.DataFrame(columns)
    print(f"패널 완성: {panel.shape}", flush=True)

    rf_cfg = config.get("regime_filter") or {}
    ticker = _REGIME_INDEX_TICKER.get(rf_cfg.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, SIM_END, ticker)
    bench = await asyncio.to_thread(
        _load_regime_series, WARMUP_START, SIM_END, _BENCHMARK_TICKER["KOSPI200"]
    )
    print(f"KOSPI200 벤치마크 로드: {'OK' if bench is not None else 'FAIL'}"
          f"{'' if bench is None else f' ({len(bench)}봉)'}", flush=True)

    def run(rf: float):
        cfg = {**config, "risk_free_rate": rf}
        return run_rebalance_backtest(
            panel, cfg, _to_dt(SIM_START), _to_dt(SIM_END, end=True),
            _fundamentals_provider, regime_series, pool_provider, bench,
        )

    def pct(x):
        return "n/a" if x is None else f"{x*100:+.1f}%"

    def num(x):
        return "n/a" if x is None else f"{x:.3f}"

    print(f"\n=== id=23 벤치마크 상대성과·위험조정지표 (FULL {SIM_START}~{SIM_END}) ===", flush=True)
    for label, rf in [("(A) rf=0%", 0.0), ("(B) rf=3%", 0.03)]:
        r = await asyncio.to_thread(run, rf)
        print(
            f"\n{label}\n"
            f"  총수익 {pct(r['total_return'])} | 벤치(KOSPI200) {pct(r['benchmark_return'])} "
            f"| 초과 {pct(r['excess_return'])}\n"
            f"  alpha(연) {pct(r['alpha'])} | beta {num(r['beta'])} "
            f"| IR {num(r['information_ratio'])} | TE {pct(r['tracking_error'])}\n"
            f"  Sharpe {num(r['sharpe'])} | Sortino {num(r['sortino'])} | MDD {pct(r['mdd'])}",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
