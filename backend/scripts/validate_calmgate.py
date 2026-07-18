"""역방향 변동성 게이트(calm gate) 실험: id=23 에 저변동 진입 게이트를 얹으면 개선되는가.

변동성 수확 게이트(id=32)가 PIT 전면 기각된 뒤의 역발상 실험. vol_gate 의 spike_max
(RV20/RV252 상한, 신규 필드)를 써서 "단기 변동성이 장기 대비 안정된 종목만 편입"을
표현한다. id=23(균형 멀티팩터, 저변동 IR 최강)의 우위와 정합적인 가설이므로,
id=23 config 에 게이트만 추가한 변형으로 순수 증분 효과를 측정한다.

- 등록하지 않고 검증만 한다(유망하면 별도 등록).
- 성긴 격자(spike_max 0.75/0.85/0.95)만 — 촘촘한 그리드서치는 과최적화라 금지.
- register_and_validate_volharvest.py 의 PIT 파이프라인 재사용.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import numpy as np
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
from app.schemas.strategy import StrategyConfig
from app.services.backtest.deflated_sharpe import (
    compute_deflated_sharpe,
    estimate_trial_stats,
    grade_dsr,
    returns_from_equity_curve,
)
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
BASE = "id23(기준)"

CALM_GATE_BASE = {
    # spike_min 은 사실상 무하한(0.01) — 상한(spike_max)만으로 '안정 구간' 판정.
    "spike_lookback": 20, "base_lookback": 252,
    "spike_min": 0.01, "cap": 0.90, "require_uptrend": False,
}


def _calm_variant(cfg23: dict, spike_max: float) -> dict:
    cfg = copy.deepcopy(cfg23)
    sel = cfg.setdefault("selection", {})
    sel["vol_gate"] = {**CALM_GATE_BASE, "spike_max": spike_max}
    return cfg


def _invested_ratio(result: dict) -> float | None:
    """markers 스텝 재구성으로 가동률 산출(register_and_validate_volharvest 와 동일)."""
    eq = result.get("equity_curve") or []
    if not eq:
        return None
    events: dict[str, int] = {}
    for m in result.get("markers") or []:
        t = m.get("t")
        if not t:
            continue
        if m.get("type") == "rebalance":
            events[t] = int(m.get("holdings") or 0)
        else:
            events[t] = 0
    holdings = 0
    invested_days = 0
    for pt in eq:
        t = pt.get("t")
        if t in events:
            holdings = events[t]
        if holdings > 0:
            invested_days += 1
    return invested_days / len(eq)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        s23 = await db.scalar(select(Strategy).where(Strategy.id == 23))
        cfg23 = dict(s23.config)

    variants = {
        "calm(spike_max=0.75)": _calm_variant(cfg23, 0.75),
        "calm(spike_max=0.85)": _calm_variant(cfg23, 0.85),
        "calm(spike_max=0.95)": _calm_variant(cfg23, 0.95),
    }
    all_cfgs = {BASE: cfg23, **variants}

    print("=== 0) 스키마 검증 ===", flush=True)
    from pydantic import TypeAdapter
    ta = TypeAdapter(StrategyConfig)
    for name, cfg in all_cfgs.items():
        ta.validate_python(cfg)
        print(f"  [OK] {name}", flush=True)

    print("\n=== 1) PIT 유니버스·패널 조립 (id23 풀 공유) ===", flush=True)
    pool = _build_pit_pool(cfg23, WARMUP_START, PERIOD_END)
    u, prov = pool
    full_universe = sorted(set(cfg23.get("universe", []) or []) | set(u or []))
    print(f"PIT universe: {len(full_universe)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(PERIOD_END, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for sym in full_universe:
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
    bench_ticker = _BENCHMARK_TICKER.get(cfg23.get("benchmark_index", "KOSPI200"), "1028")
    benchmark_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, PERIOD_END, bench_ticker)

    def run(cfg, s, e):
        return run_rebalance_backtest(
            panel, cfg, _to_dt(s), _to_dt(e, end=True),
            _fundamentals_provider, regime_series, prov, benchmark_series,
        )

    print("\n=== 2) 성과 그리드 (반기 2-fold + FULL) ===", flush=True)
    grid: dict[str, dict[str, dict]] = {}
    rets_full: dict[str, pd.Series] = {}
    for name, cfg in all_cfgs.items():
        grid[name] = {}
        for wlabel, s, e in [*WINDOWS, FULL]:
            r = run(cfg, s, e)
            grid[name][wlabel] = r
            inv = _invested_ratio(r)
            print(
                f"[{name:22}] {wlabel}: ret={(r.get('total_return') or 0)*100:+7.2f}% "
                f"sharpe={r.get('sharpe') if r.get('sharpe') is not None else float('nan'):5.2f} "
                f"mdd={(r.get('mdd') or 0)*100:6.2f}% "
                f"alpha={(r.get('alpha') or 0)*100:+6.2f}% "
                f"beta={r.get('beta') if r.get('beta') is not None else float('nan'):5.2f} "
                f"turnover={(r.get('avg_turnover') or 0)*100:5.1f}% "
                f"가동률={inv*100 if inv is not None else float('nan'):5.1f}% "
                f"rebal={r.get('num_rebalances')} kills={r.get('num_kills')}",
                flush=True,
            )
            if wlabel == FULL[0]:
                eq = r.get("equity_curve") or []
                if eq:
                    ser = pd.Series({pd.Timestamp(p["t"]): float(p["v"]) for p in eq})
                    rets_full[name] = ser.pct_change().dropna()

    print("\n=== 3) DSR(동질 시행=spike_max 격자 FULL) ===", flush=True)
    sharpes, rets_list = [], []
    for name in variants:
        r = grid[name][FULL[0]]
        sh = r.get("sharpe")
        dr = returns_from_equity_curve(r.get("equity_curve") or [])
        if sh is not None and dr.size >= 2:
            sharpes.append(float(sh))
            rets_list.append(dr)
    if len(sharpes) >= 2:
        stats = estimate_trial_stats(sharpes, rets_list)
        best_name = max(variants, key=lambda n: grid[n][FULL[0]].get("sharpe") or -9)
        best_r = grid[best_name][FULL[0]]
        best_dr = returns_from_equity_curve(best_r.get("equity_curve") or [])
        dsr_out = compute_deflated_sharpe(
            best_dr, float(best_r.get("sharpe") or 0.0), stats["N_eff"], stats["V_daily"],
        )
        dsr = dsr_out.get("dsr")
        print(
            f"  best={best_name} N={stats['N']} N_eff={stats['N_eff']} "
            f"rho_bar={stats['rho_bar']:.2f} DSR={dsr if dsr is not None else 'None'} "
            f"등급={grade_dsr(dsr)}",
            flush=True,
        )
    else:
        print("  유효 시행 부족 — DSR 미산출", flush=True)

    print("\n=== 결과 JSON 요약 ===", flush=True)
    summary = {}
    for name in all_cfgs:
        summary[name] = {}
        for wlabel, _s, _e in [*WINDOWS, FULL]:
            r = grid[name][wlabel]
            summary[name][wlabel] = {
                **{k: r.get(k) for k in [
                    "total_return", "cagr", "sharpe", "sortino", "mdd", "alpha", "beta",
                    "information_ratio", "avg_turnover", "num_rebalances", "num_kills",
                ]},
                "invested_ratio": _invested_ratio(r),
            }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
