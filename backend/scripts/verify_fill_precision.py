"""P2-2 체결 정밀도 검증: id=23·24 를 정수주·ADV 캡 on/off 로 재백테스트.

  (1) integer_shares=False, adv_cap=None        → 구 동작(연속 비중·유동성 무제한, 기준선)
  (2) integer_shares=True,  adv_cap=None         → 정수주 절사만 추가(A-1 단독 효과)
  (3) integer_shares=True,  adv_cap=0.08         → 정수주 + ADV 8% 참여율 캡(A-1+A-2)

대형주(KOSPI200) 전략이므로 델타가 작아야 정상 — 크면 그 자체가 발견(예: 저유동성
소형주 혼입, capacity 문제)이다. 거래량 패널은 get_volume_series 로 종가 패널과
동일 구간 적재한다(pit pool·펀더멘털·레짐 조립은 verify_fill_model.py 와 동일 패턴).
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
from app.services.data.loader import (
    get_close_series,
    get_volume_series,
    load_ohlcv,
    upsert_price_ticks,
)
from sqlalchemy import select

WARMUP_START = date(2019, 8, 1)
SIM_START = date(2021, 1, 1)
SIM_END = date(2025, 6, 30)

STRATEGY_IDS = [23, 24]


async def _load_panels(universe: list[str], warmup_dt, end_dt):
    close_cols: dict[str, pd.Series] = {}
    vol_cols: dict[str, pd.Series] = {}
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
                close_cols[sym] = series
                vol_cols[sym] = await get_volume_series(db, sym, warmup_dt, end_dt)
            if (i + 1) % 50 == 0:
                print(f"  ...패널 적재 {i + 1}/{len(universe)}", flush=True)
    return pd.DataFrame(close_cols), pd.DataFrame(vol_cols)


async def _run_for_strategy(strategy_id: int) -> None:
    async with AsyncSessionLocal() as db:
        base = await db.scalar(select(Strategy).where(Strategy.id == strategy_id))
        if base is None:
            print(f"strategy id={strategy_id} 없음 — 건너뜀", flush=True)
            return
        config = dict(base.config)

    pit_union, pool_provider = _build_pit_pool(config, WARMUP_START, SIM_END)
    universe = pit_union if pit_union is not None else list(config.get("universe", []))
    print(f"id={strategy_id} PIT union universe: {len(universe)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(SIM_END, end=True)
    panel, volume_panel = await _load_panels(universe, warmup_dt, end_dt)
    print(f"패널 완성: close={panel.shape} volume={volume_panel.shape}", flush=True)

    rf = config.get("regime_filter") or {}
    ticker = _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, SIM_END, ticker)

    def _n(x):
        return float("nan") if x is None else float(x)

    def run(overrides: dict, use_volume: bool):
        cfg = {**config, **overrides}
        r = run_rebalance_backtest(
            panel, cfg, _to_dt(SIM_START), _to_dt(SIM_END, end=True),
            _fundamentals_provider, regime_series, pool_provider,
            None, None, volume_panel if use_volume else None,
        )
        return (
            _n(r.get("total_return")), _n(r.get("sharpe")), _n(r.get("mdd")),
            _n(r.get("cagr")), r.get("num_trades"), _n(r.get("avg_turnover")),
        )

    def _fmt(t):
        return (f"ret={t[0]*100:+.1f}% sharpe={t[1]:.2f} mdd={t[2]*100:.1f}% "
                f"cagr={t[3]*100:+.1f}% trades={t[4]} turn={t[5]*100:.1f}%")

    cases = [
        ("(1) integer_shares=F adv=None [구 동작]",
         {"integer_shares": False, "adv_participation_cap": None}, False),
        ("(2) integer_shares=T adv=None [정수주만]",
         {"integer_shares": True, "adv_participation_cap": None}, False),
        ("(3) integer_shares=T adv=0.08  [정수주+ADV캡]",
         {"integer_shares": True, "adv_participation_cap": 0.08}, True),
    ]

    print(f"\n=== id={strategy_id} 체결 정밀도 민감도 (FULL {SIM_START}~{SIM_END}) ===", flush=True)
    base_ret = None
    for label, ov, use_volume in cases:
        t = await asyncio.to_thread(run, ov, use_volume)
        if base_ret is None:
            base_ret = t[0]
            delta = ""
        else:
            delta = f"  (기준선 대비 {(t[0]-base_ret)*100:+.1f}%p)"
        print(f"{label:40} {_fmt(t)}{delta}", flush=True)


async def main() -> None:
    for sid in STRATEGY_IDS:
        await _run_for_strategy(sid)


if __name__ == "__main__":
    asyncio.run(main())
