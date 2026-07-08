"""P0-1 체결 현실성 모델 검증: id=23 을 세 가지 체결 설정으로 재백테스트.

  (1) same_close, slip=0  → 구 동작(당일 종가·무슬리피지) 재현(기준선)
  (2) next_close, slip=0  → 익일 종가 체결만 (미래참조 제거 효과 격리)
  (3) next_close, slip=5bp → 익일 종가 + 편도 5bp 슬리피지(신규 기본값)
  (4) next_close, slip=5bp, vol_scale=1.0 → 변동성 비례 슬리피지 옵션 동작 확인

패널·펀더멘털·레짐·PIT pool_provider 를 한 번만 조립하고 config 의 체결 필드만 바꾼다.
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
            if (i + 1) % 50 == 0:
                print(f"  ...패널 적재 {i + 1}/{len(universe)}", flush=True)
    panel = pd.DataFrame(columns)
    print(f"패널 완성: {panel.shape}", flush=True)

    rf = config.get("regime_filter") or {}
    ticker = _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, SIM_END, ticker)

    def _n(x):
        return float("nan") if x is None else float(x)

    def run(overrides: dict):
        cfg = {**config, **overrides}
        r = run_rebalance_backtest(
            panel, cfg, _to_dt(SIM_START), _to_dt(SIM_END, end=True),
            _fundamentals_provider, regime_series, pool_provider,
        )
        return (
            _n(r.get("total_return")), _n(r.get("sharpe")), _n(r.get("mdd")),
            _n(r.get("cagr")), r.get("num_trades"), _n(r.get("avg_turnover")),
        )

    def _fmt(t):
        return (f"ret={t[0]*100:+.1f}% sharpe={t[1]:.2f} mdd={t[2]*100:.1f}% "
                f"cagr={t[3]*100:+.1f}% trades={t[4]} turn={t[5]*100:.1f}%")

    cases = [
        ("(1) same_close  slip=0     [구 동작]", {"fill_mode": "same_close", "slippage_bps": 0.0, "slippage_vol_scale": 0.0}),
        ("(2) next_close  slip=0     [미래참조만 제거]", {"fill_mode": "next_close", "slippage_bps": 0.0, "slippage_vol_scale": 0.0}),
        ("(3) next_close  slip=5bp   [신규 기본]", {"fill_mode": "next_close", "slippage_bps": 5.0, "slippage_vol_scale": 0.0}),
        ("(4) next_close  slip=5bp vs=1.0 [변동성 스케일]", {"fill_mode": "next_close", "slippage_bps": 5.0, "slippage_vol_scale": 1.0}),
    ]

    print(f"\n=== id=23 체결 현실성 민감도 (FULL {SIM_START}~{SIM_END}) ===", flush=True)
    base_ret = None
    for label, ov in cases:
        t = await asyncio.to_thread(run, ov)
        if base_ret is None:
            base_ret = t[0]
            delta = ""
        else:
            delta = f"  (기준선 대비 {(t[0]-base_ret)*100:+.1f}%p)"
        print(f"{label:42} {_fmt(t)}{delta}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
