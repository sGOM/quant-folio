"""팩터 섹터 중립화 A/B (docs/improvements.md §20 후속 리서치, 남은 과제).

§20 에서 배선한 `neutralize="sector"`(섹터별 그룹 demean)·`"size_sector"`(사이즈 잔차화
후 섹터 demean)가 id=23 성과의 업종 쏠림을 분리해 개선하는지 PIT KOSPI200 에서 확정한다.

- 대상: id=23 등록 원본(base) vs neutralize="sector" vs "size_sector" — 다른 설정 불변.
- 판정: 방어형 규약대로 **alpha/Sharpe** (excess/IR 금지 — 저베타 구간변경 아티팩트).
  반기 2-fold 워크포워드에서 양 반기 모두 우위면 승격 후보, 아니면 현행 유지.
- 프로바이더는 라우트(backtests.py)와 동일 규칙으로 구성해 실경로를 재현한다.

실행: docker compose exec -T web python scripts/validate_sector_neutralize_ab.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from functools import partial

import pandas as pd

from app.api.routes.backtests import (
    _BENCHMARK_TICKER,
    _REGIME_INDEX_TICKER,
    _build_pit_pool,
    _fundamentals_provider,
    _fundamentals_provider_with_neutralize_cols,
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

STRATEGY_ID = 23
NEUTRALIZE_VARIANTS = ["sector", "size_sector"]


def _provider_for(cfg: dict):
    """라우트(backtests.py run 경로)와 동일한 규칙으로 펀더멘털 프로바이더를 만든다."""
    use_ttm = cfg.get("financial_period", "annual") == "ttm"
    neutralize = cfg.get("selection", {}).get("neutralize", "none")
    if neutralize in ("size", "sector", "size_sector"):
        return partial(
            _fundamentals_provider_with_neutralize_cols, use_ttm=use_ttm, neutralize=neutralize,
        )
    return partial(_fundamentals_provider, use_ttm=use_ttm)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        s = await db.scalar(select(Strategy).where(Strategy.id == STRATEGY_ID))
        if s is None:
            print(f"전략 id={STRATEGY_ID} 없음 — 중단", flush=True)
            return
        base_cfg = dict(s.config)
    base_neut = base_cfg.get("selection", {}).get("neutralize", "none")
    print(f"id={STRATEGY_ID} {s.name}: 등록 neutralize={base_neut}", flush=True)

    variants: dict[str, dict] = {f"base({base_neut})": copy.deepcopy(base_cfg)}
    for neut in NEUTRALIZE_VARIANTS:
        if neut == base_neut:
            continue
        cfg = copy.deepcopy(base_cfg)
        cfg.setdefault("selection", {})["neutralize"] = neut
        variants[neut] = cfg

    print("\n=== 1) PIT 유니버스·패널 조립 ===", flush=True)
    pit_union, pool_provider = _build_pit_pool(base_cfg, WARMUP_START, PERIOD_END)
    full_universe = sorted(set(pit_union or []) | set(base_cfg.get("universe") or []))
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

    rf = base_cfg.get("regime_filter") or {}
    regime_ticker = _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(
        _load_regime_series, WARMUP_START, PERIOD_END, regime_ticker
    )
    bench_ticker = _BENCHMARK_TICKER.get(base_cfg.get("benchmark_index", "KOSPI200"), "1028")
    benchmark_series = await asyncio.to_thread(
        _load_regime_series, WARMUP_START, PERIOD_END, bench_ticker
    )

    def run(cfg: dict, s_: date, e_: date) -> dict:
        return run_rebalance_backtest(
            panel, cfg, _to_dt(s_), _to_dt(e_, end=True),
            _provider_for(cfg), regime_series, pool_provider, benchmark_series,
        )

    print("\n=== 2) 성과 그리드 (반기 2-fold + FULL) ===", flush=True)
    grid: dict[str, dict[str, dict]] = {}
    for name, cfg in variants.items():
        grid[name] = {}
        for wlabel, s_, e_ in [*WINDOWS, FULL]:
            r = run(cfg, s_, e_)
            grid[name][wlabel] = r
            print(
                f"[{name:14}] {wlabel}: ret={(r.get('total_return') or 0) * 100:+7.2f}% "
                f"sharpe={r.get('sharpe') if r.get('sharpe') is not None else float('nan'):5.2f} "
                f"alpha={(r.get('alpha') or 0) * 100:+6.2f}% "
                f"beta={r.get('beta') if r.get('beta') is not None else float('nan'):5.2f} "
                f"mdd={(r.get('mdd') or 0) * 100:6.2f}% "
                f"turnover={(r.get('avg_turnover') or 0) * 100:5.1f}% "
                f"rebal={r.get('num_rebalances')}",
                flush=True,
            )

    print("\n=== 3) 판정 (방어형 규약: alpha/Sharpe, 양 반기 우위 → 승격 후보) ===", flush=True)
    base_name = f"base({base_neut})"

    def _v(name: str, wlabel: str, key: str) -> float:
        x = grid[name][wlabel].get(key)
        return float("-inf") if x is None else float(x)

    verdicts: dict[str, str] = {}
    for name in variants:
        if name == base_name:
            continue
        lines = []
        wins = 0
        for wlabel, _s, _e in WINDOWS:
            sh_win = _v(name, wlabel, "sharpe") > _v(base_name, wlabel, "sharpe")
            al_win = _v(name, wlabel, "alpha") > _v(base_name, wlabel, "alpha")
            wins += int(sh_win and al_win)
            lines.append(
                f"    {wlabel}: sharpe {_v(name, wlabel, 'sharpe'):.2f} vs "
                f"{_v(base_name, wlabel, 'sharpe'):.2f} ({'승' if sh_win else '패'}) · "
                f"alpha {_v(name, wlabel, 'alpha') * 100:+.2f}% vs "
                f"{_v(base_name, wlabel, 'alpha') * 100:+.2f}% ({'승' if al_win else '패'})"
            )
        full_sh = _v(name, FULL[0], "sharpe") > _v(base_name, FULL[0], "sharpe")
        full_al = _v(name, FULL[0], "alpha") > _v(base_name, FULL[0], "alpha")
        verdict = (
            "승격 후보(양 반기 alpha·Sharpe 모두 우위)"
            if wins == len(WINDOWS)
            else ("혼재 — 현행 유지" if wins > 0 or (full_sh and full_al) else "열위 — 현행 유지")
        )
        verdicts[name] = verdict
        print(f"  {name}: {verdict}", flush=True)
        for ln in lines:
            print(ln, flush=True)
        print(
            f"    {FULL[0]}: sharpe {'승' if full_sh else '패'} · alpha {'승' if full_al else '패'}",
            flush=True,
        )

    print("\n=== 결과 JSON 요약 ===", flush=True)
    summary: dict[str, dict] = {"verdicts": verdicts}
    for name in variants:
        summary[name] = {
            wlabel: {
                k: grid[name][wlabel].get(k)
                for k in [
                    "total_return", "cagr", "sharpe", "sortino", "mdd", "alpha", "beta",
                    "information_ratio", "avg_turnover", "num_rebalances", "num_kills",
                ]
            }
            for wlabel, _s, _e in [*WINDOWS, FULL]
        }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
