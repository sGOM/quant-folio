"""TTM 재무 경로 A/B 재검증 (docs/improvements.md §3, 남은 과제 3).

PR #61(TTM 계산 경로)·PR #69 §8(financial_period 실배선) 이후 한 번도 판정되지 않은
가설 — "재무 반영 시차가 분기로 짧아지면 퀄리티·성장 팩터 신선도가 올라 성과가
개선된다" — 를 PIT KOSPI200 에서 실측 확정한다.

- 대상: id=23(균형 멀티팩터)·id=24(밸류·퀄리티 컨트래리언), 각각 annual(등록 원본)
  vs ttm(financial_period="ttm"만 변경) 비교 — 순수 증분 효과 측정.
- 판정: 방어형 규약대로 **alpha/Sharpe** (excess/IR 금지 — 저베타 구간변경 아티팩트).
  반기 2-fold 워크포워드에서 양 반기 모두 우위면 승격 후보, 아니면 옵트인 유지.
- 프로바이더는 라우트(backtests.py)와 동일 규칙으로 구성해 실경로를 재현한다
  (financial_period → use_ttm, selection.neutralize → 중립화 축 컬럼).

실행: docker compose exec -T web python scripts/validate_ttm_ab.py
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

STRATEGY_IDS = [23, 24]


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
    configs: dict[int, dict] = {}
    async with AsyncSessionLocal() as db:
        for sid in STRATEGY_IDS:
            s = await db.scalar(select(Strategy).where(Strategy.id == sid))
            if s is None:
                print(f"전략 id={sid} 없음 — 건너뜀", flush=True)
                continue
            configs[sid] = dict(s.config)
            print(
                f"id={sid} {s.name}: financial_period={s.config.get('financial_period', 'annual')} "
                f"neutralize={s.config.get('selection', {}).get('neutralize', 'none')}",
                flush=True,
            )

    # A/B 변형: 등록 원본(annual 확인) vs financial_period="ttm"만 변경.
    variants: dict[str, dict] = {}
    for sid, cfg in configs.items():
        annual = copy.deepcopy(cfg)
        annual["financial_period"] = "annual"
        ttm = copy.deepcopy(cfg)
        ttm["financial_period"] = "ttm"
        variants[f"id{sid} annual"] = annual
        variants[f"id{sid} ttm"] = ttm

    print("\n=== 1) PIT 유니버스·패널 조립 (id23 풀 공유 — 동일 KOSPI200 소스) ===", flush=True)
    base_cfg = configs[STRATEGY_IDS[0]]
    pit_union, pool_provider = _build_pit_pool(base_cfg, WARMUP_START, PERIOD_END)
    full_universe = sorted(
        set(pit_union or [])
        | {sym for cfg in configs.values() for sym in (cfg.get("universe") or [])}
    )
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

    # 레짐 지수는 전략별로 다를 수 있어 티커 단위로 1회씩 적재.
    regime_by_ticker: dict[str, pd.Series | None] = {}
    for cfg in configs.values():
        rf = cfg.get("regime_filter") or {}
        ticker = _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001")
        if ticker not in regime_by_ticker:
            regime_by_ticker[ticker] = await asyncio.to_thread(
                _load_regime_series, WARMUP_START, PERIOD_END, ticker
            )
    bench_ticker = _BENCHMARK_TICKER.get(base_cfg.get("benchmark_index", "KOSPI200"), "1028")
    benchmark_series = await asyncio.to_thread(
        _load_regime_series, WARMUP_START, PERIOD_END, bench_ticker
    )

    def run(cfg: dict, s: date, e: date) -> dict:
        rf = cfg.get("regime_filter") or {}
        ticker = _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001")
        return run_rebalance_backtest(
            panel, cfg, _to_dt(s), _to_dt(e, end=True),
            _provider_for(cfg), regime_by_ticker[ticker], pool_provider, benchmark_series,
        )

    print("\n=== 2) 성과 그리드 (반기 2-fold + FULL) ===", flush=True)
    grid: dict[str, dict[str, dict]] = {}
    for name, cfg in variants.items():
        grid[name] = {}
        for wlabel, s, e in [*WINDOWS, FULL]:
            r = run(cfg, s, e)
            grid[name][wlabel] = r
            print(
                f"[{name:12}] {wlabel}: ret={(r.get('total_return') or 0) * 100:+7.2f}% "
                f"sharpe={r.get('sharpe') if r.get('sharpe') is not None else float('nan'):5.2f} "
                f"alpha={(r.get('alpha') or 0) * 100:+6.2f}% "
                f"beta={r.get('beta') if r.get('beta') is not None else float('nan'):5.2f} "
                f"mdd={(r.get('mdd') or 0) * 100:6.2f}% "
                f"turnover={(r.get('avg_turnover') or 0) * 100:5.1f}% "
                f"rebal={r.get('num_rebalances')}",
                flush=True,
            )

    print("\n=== 3) 판정 (방어형 규약: alpha/Sharpe, 양 반기 우위 → 승격 후보) ===", flush=True)

    def _v(name: str, wlabel: str, key: str) -> float:
        x = grid[name][wlabel].get(key)
        return float("-inf") if x is None else float(x)

    verdicts: dict[int, str] = {}
    for sid in configs:
        a, t = f"id{sid} annual", f"id{sid} ttm"
        lines = []
        wins = 0
        for wlabel, _s, _e in WINDOWS:
            sh_win = _v(t, wlabel, "sharpe") > _v(a, wlabel, "sharpe")
            al_win = _v(t, wlabel, "alpha") > _v(a, wlabel, "alpha")
            wins += int(sh_win and al_win)
            lines.append(
                f"    {wlabel}: sharpe {_v(t, wlabel, 'sharpe'):.2f} vs {_v(a, wlabel, 'sharpe'):.2f} "
                f"({'승' if sh_win else '패'}) · alpha {_v(t, wlabel, 'alpha') * 100:+.2f}% vs "
                f"{_v(a, wlabel, 'alpha') * 100:+.2f}% ({'승' if al_win else '패'})"
            )
        full_sh = _v(t, FULL[0], "sharpe") > _v(a, FULL[0], "sharpe")
        full_al = _v(t, FULL[0], "alpha") > _v(a, FULL[0], "alpha")
        verdict = (
            "승격 후보(양 반기 alpha·Sharpe 모두 우위)"
            if wins == len(WINDOWS)
            else ("혼재 — 옵트인 유지" if wins > 0 or (full_sh and full_al) else "열위 — 옵트인 유지")
        )
        verdicts[sid] = verdict
        print(f"  id={sid}: {verdict}", flush=True)
        for ln in lines:
            print(ln, flush=True)
        print(
            f"    {FULL[0]}: sharpe {'승' if full_sh else '패'} · alpha {'승' if full_al else '패'}",
            flush=True,
        )

    print("\n=== 결과 JSON 요약 ===", flush=True)
    summary: dict[str, dict] = {"verdicts": {str(k): v for k, v in verdicts.items()}}
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
