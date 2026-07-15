"""financial-expert 설계 전략 A/B/C를 스키마 검증 후 등록하고,
id=23 대비 PIT KOSPI200 반기 2-fold + FULL 백테스트로 성과를 수집한다.

validate_candidates.py 패턴을 재사용: 패널·펀더멘털·레짐·PIT 풀 조립,
run_rebalance_backtest 로 alpha/beta/IR/turnover 등 전체 지표를 얻는다.
"""
from __future__ import annotations

import asyncio
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
from app.models import Strategy, StrategyStatus
from app.schemas.strategy import StrategyConfig
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
OWNER_USER_ID = 1

CONFIGS = {
    "A 미니멈볼 리스크패리티 코어": {
        "type": "rebalance", "universe": [],
        "selection": {
            "method": "score", "top_n": 30,
            "factor_weights": {"momentum": 0.0, "value": 0.2, "lowvol": 0.5, "quality": 0.3, "growth": 0.0},
            "neutralize": "size",
            "universe_rule": {"type": "momentum", "source": "KOSPI200", "lookback": 120, "pick": 80, "min_market_cap": 5000},
        },
        "weighting": "inverse_vol", "cadence": "quarterly", "rebalance_dom": 1, "rebalance_time": "14:30",
        "drift_band_pct": 0.02,
        "regime_filter": {"enabled": True, "index": "KOSPI", "ma_period": 200, "reentry_buffer_pct": 0.02, "exit_buffer_pct": 0.15},
        "risk_layer": {"max_position_pct": 0.08, "target_vol": 0.10, "vol_lookback": 20, "max_leverage": 1.0, "mdd_kill_pct": 0.20, "mdd_rearm_days": 20},
        "capital": 10000000, "fees": 0.00015, "tax": 0.0020,
        "fill_mode": "next_close", "slippage_bps": 5.0, "slippage_vol_scale": 0.5,
        "benchmark_index": "KOSPI200", "risk_free_rate": 0.03,
    },
    "B 퀄리티-성장 GARP 새틀라이트": {
        "type": "rebalance", "universe": [],
        "selection": {
            "method": "score", "top_n": 20,
            "factor_weights": {"momentum": 0.0, "value": 0.2, "lowvol": 0.15, "quality": 0.35, "growth": 0.3},
            "neutralize": "size",
            "universe_rule": {"type": "momentum", "source": "KOSPI200", "lookback": 120, "pick": 80, "min_market_cap": 5000},
        },
        "weighting": "equal", "cadence": "quarterly", "rebalance_dom": 1, "rebalance_time": "14:30",
        "drift_band_pct": 0.03,
        "regime_filter": {"enabled": True, "index": "KOSPI", "ma_period": 200, "reentry_buffer_pct": 0.0, "exit_buffer_pct": 0.10},
        "risk_layer": {"max_position_pct": 0.10, "vol_lookback": 20, "max_leverage": 1.0, "mdd_kill_pct": 0.25, "mdd_rearm_days": 20},
        "capital": 10000000, "fees": 0.00015, "tax": 0.0020,
        "fill_mode": "next_close", "slippage_bps": 5.0, "slippage_vol_scale": 0.5,
        "benchmark_index": "KOSPI200", "risk_free_rate": 0.03,
    },
    "C 정예 컨빅션 멀티팩터": {
        "type": "rebalance", "universe": [],
        "selection": {
            "method": "score", "top_n": 12,
            "factor_weights": {"momentum": 0.0, "value": 0.25, "lowvol": 0.35, "quality": 0.3, "growth": 0.1},
            "neutralize": "size",
            "universe_rule": {"type": "momentum", "source": "KOSPI200", "lookback": 120, "pick": 60, "min_market_cap": 5000},
        },
        "weighting": "score", "cadence": "monthly", "rebalance_dom": 1, "rebalance_time": "14:30",
        "drift_band_pct": 0.05,
        "regime_filter": {"enabled": True, "index": "KOSPI", "ma_period": 200, "reentry_buffer_pct": 0.0, "exit_buffer_pct": 0.12},
        "risk_layer": {"max_position_pct": 0.18, "target_vol": 0.13, "vol_lookback": 20, "max_leverage": 1.0, "mdd_kill_pct": 0.22, "mdd_rearm_days": 20},
        "capital": 10000000, "fees": 0.00015, "tax": 0.0020,
        "fill_mode": "next_close", "slippage_bps": 5.0, "slippage_vol_scale": 0.5,
        "benchmark_index": "KOSPI200", "risk_free_rate": 0.03,
    },
}


def _validate_all() -> dict[str, dict]:
    """스키마(RebalanceConfig discriminated union) 검증. 실패 시 그대로 예외 전파."""
    validated = {}
    for name, cfg in CONFIGS.items():
        model = StrategyConfig.validate_python(cfg) if hasattr(StrategyConfig, "validate_python") else None
        # StrategyConfig 는 TypeAdapter 가 아니라 Annotated Union 일 수 있으므로 개별 방식 시도.
        validated[name] = cfg
        print(f"[검증 OK] {name}", flush=True)
    return validated


async def _register(cfg_map: dict[str, dict]) -> dict[str, int]:
    ids: dict[str, int] = {}
    async with AsyncSessionLocal() as db:
        for name, cfg in cfg_map.items():
            existing = await db.scalar(select(Strategy).where(Strategy.user_id == OWNER_USER_ID, Strategy.name == name))
            if existing:
                existing.config = cfg
                existing.status = StrategyStatus.DRAFT
                s = existing
            else:
                s = Strategy(user_id=OWNER_USER_ID, name=name, description="financial-expert 설계 · PIT KOSPI200 검증", config=cfg, status=StrategyStatus.DRAFT)
                db.add(s)
            await db.flush()
            ids[name] = s.id
        await db.commit()
    return ids


async def main() -> None:
    print("=== 0) 스키마 검증 ===", flush=True)
    from pydantic import TypeAdapter
    ta = TypeAdapter(StrategyConfig)
    for name, cfg in CONFIGS.items():
        try:
            ta.validate_python(cfg)
            print(f"  [OK] {name}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {name}: {e}", flush=True)
            raise

    print("\n=== 1) 등록 ===", flush=True)
    ids = await _register(CONFIGS)
    for name, sid in ids.items():
        print(f"  {name} -> id={sid}", flush=True)

    async with AsyncSessionLocal() as db:
        s23 = await db.scalar(select(Strategy).where(Strategy.id == 23))
        cfg23 = dict(s23.config)

    all_cfgs = {BASE: cfg23, **CONFIGS}

    print("\n=== 2) PIT 유니버스·패널 조립 ===", flush=True)
    pit_union, pool_provider = _build_pit_pool(cfg23, WARMUP_START, PERIOD_END)
    universe = pit_union if pit_union is not None else list(cfg23.get("universe", []))
    # 후보 universe_rule 의 pick(A/B=80, C=60)이 id=23과 다르면 최대 pick 기준 union 필요.
    # _build_pit_pool 은 cfg 의 universe_rule 만 보므로 각 cfg 별로 별도 union 계산.
    pools: dict[str, tuple] = {BASE: (pit_union, pool_provider)}
    for name, cfg in CONFIGS.items():
        pools[name] = _build_pit_pool(cfg, WARMUP_START, PERIOD_END)

    full_universe: set[str] = set(universe or [])
    for name, (u, _p) in pools.items():
        if u:
            full_universe |= set(u)
    full_universe = sorted(full_universe)
    print(f"통합 PIT universe: {len(full_universe)}종목", flush=True)

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
    print(f"벤치마크(KOSPI200) 시리즈: {len(benchmark_series)}건", flush=True)

    def run(cfg, pool_prov, s, e):
        r = run_rebalance_backtest(
            panel, cfg, _to_dt(s), _to_dt(e, end=True),
            _fundamentals_provider, regime_series, pool_prov, benchmark_series,
        )
        return r

    print("\n=== 3) 성과 그리드 (반기 2-fold + FULL) ===", flush=True)
    grid: dict[str, dict[str, dict]] = {}
    rets_full: dict[str, pd.Series] = {}
    for name, cfg in all_cfgs.items():
        _u, prov = pools[name]
        grid[name] = {}
        for wlabel, s, e in [*WINDOWS, FULL]:
            r = run(cfg, prov, s, e)
            grid[name][wlabel] = r
            tr = r.get("total_return") or 0.0
            sh = r.get("sharpe")
            so = r.get("sortino")
            mdd = r.get("mdd") or 0.0
            cagr = r.get("cagr")
            alpha = r.get("alpha")
            beta = r.get("beta")
            ir = r.get("information_ratio")
            to = r.get("avg_turnover")
            print(
                f"[{name:26}] {wlabel}: ret={tr*100:+7.2f}% cagr={(cagr or 0)*100:+6.2f}% "
                f"sharpe={sh if sh is not None else float('nan'):5.2f} sortino={so if so is not None else float('nan'):5.2f} "
                f"mdd={mdd*100:6.2f}% alpha={(alpha or 0)*100:+6.2f}% beta={beta if beta is not None else float('nan'):5.2f} "
                f"IR={ir if ir is not None else float('nan'):5.2f} turnover={(to or 0)*100:5.1f}%",
                flush=True,
            )
            if wlabel == FULL[0]:
                eq = r.get("equity_curve") or []
                if eq:
                    ser = pd.Series({pd.Timestamp(p["t"]): float(p["v"]) for p in eq})
                    rets_full[name] = ser.pct_change().dropna()

    print("\n=== 4) id=23 대비 상관 · 50/50 결합(FULL) ===", flush=True)
    base_r = rets_full.get(BASE)
    for name in CONFIGS:
        if name not in rets_full or base_r is None:
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
        solo_r = grid[name][FULL[0]]
        base_full = grid[BASE][FULL[0]]
        print(
            f"  {name:26} corr={corr:+.2f} | 결합Sharpe={csh:.2f} "
            f"(단독 {solo_r.get('sharpe'):.2f}, id23 {base_full.get('sharpe'):.2f}) 결합MDD={cmdd*100:.1f}%",
            flush=True,
        )

    print("\n=== 결과 JSON 덤프 (요약) ===", flush=True)
    summary = {}
    for name in all_cfgs:
        summary[name] = {}
        for wlabel, _s, _e in [*WINDOWS, FULL]:
            r = grid[name][wlabel]
            summary[name][wlabel] = {
                k: r.get(k) for k in [
                    "total_return", "cagr", "sharpe", "sortino", "mdd", "alpha", "beta",
                    "information_ratio", "tracking_error", "excess_return", "avg_turnover",
                    "benchmark_return", "num_rebalances", "num_kills",
                ]
            }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
