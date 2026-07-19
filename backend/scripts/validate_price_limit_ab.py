"""체결 모델 정밀화 A/B (docs/improvements.md §4 후속 리서치, 남은 과제).

price_limit_model(호가단위 라운딩 + 상하한가 체결불가 이월, PR §4 opt-in)이 id=23/24
성과 추정을 얼마나 바꾸는지 판정한다. 켜는 쪽이 "더 현실적인" 모델이므로 이것은
우열 판정이 아니라 **영향도 측정**이다 — 변화가 미미하면 기존 근사(슬리피지 흡수)
유지로 종결, 유의하게 다르면 등록 전략 config 승격을 검토한다.

- 대상: id=23·24 등록 원본(base) vs price_limit_model=True 만 켠 변형.
- 지표: 방어형 규약(alpha/Sharpe) + total_return 차이·MDD·회전율.
- 구간: 반기 2-fold + FULL (기존 검증 스크립트와 동일 창).

실행: docker compose exec -T web python scripts/validate_price_limit_ab.py
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

# 영향도 판정 임계: FULL alpha 변화가 연 ±1%p 미만이고 Sharpe 변화가 ±0.05 미만이면
# "미미 — 기존 근사 유지"로 종결한다(§4 원문: "성과 변화 미미하면 근사 유지로 종결").
ALPHA_EPS = 0.01
SHARPE_EPS = 0.05


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
                f"id={sid} {s.name}: price_limit_model={s.config.get('price_limit_model', False)}",
                flush=True,
            )

    variants: dict[str, dict] = {}
    for sid, cfg in configs.items():
        base = copy.deepcopy(cfg)
        base["price_limit_model"] = False
        lim = copy.deepcopy(cfg)
        lim["price_limit_model"] = True
        variants[f"id{sid} base"] = base
        variants[f"id{sid} limit"] = lim

    print("\n=== 1) PIT 유니버스·패널 조립 ===", flush=True)
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
                f"[{name:11}] {wlabel}: ret={(r.get('total_return') or 0) * 100:+7.2f}% "
                f"sharpe={r.get('sharpe') if r.get('sharpe') is not None else float('nan'):5.2f} "
                f"alpha={(r.get('alpha') or 0) * 100:+6.2f}% "
                f"mdd={(r.get('mdd') or 0) * 100:6.2f}% "
                f"turnover={(r.get('avg_turnover') or 0) * 100:5.1f}% "
                f"turnover_act={(r.get('avg_turnover_actual') or 0) * 100:5.1f}% "
                f"rebal={r.get('num_rebalances')}",
                flush=True,
            )

    print("\n=== 3) 영향도 판정 (FULL alpha ±1%p·Sharpe ±0.05 미만이면 근사 유지) ===", flush=True)

    def _v(name: str, wlabel: str, key: str) -> float | None:
        x = grid[name][wlabel].get(key)
        return None if x is None else float(x)

    verdicts: dict[int, str] = {}
    for sid in configs:
        b, l = f"id{sid} base", f"id{sid} limit"
        d_alpha = (_v(l, FULL[0], "alpha") or 0) - (_v(b, FULL[0], "alpha") or 0)
        d_sharpe = (_v(l, FULL[0], "sharpe") or 0) - (_v(b, FULL[0], "sharpe") or 0)
        d_ret = (_v(l, FULL[0], "total_return") or 0) - (_v(b, FULL[0], "total_return") or 0)
        minor = abs(d_alpha) < ALPHA_EPS and abs(d_sharpe) < SHARPE_EPS
        verdicts[sid] = (
            "미미 — 기존 근사 유지"
            if minor
            else ("유의 — 상세 검토 필요(아래 수치 참고)")
        )
        print(
            f"  id={sid}: Δalpha={d_alpha * 100:+.2f}%p Δsharpe={d_sharpe:+.3f} "
            f"Δret={d_ret * 100:+.2f}%p → {verdicts[sid]}",
            flush=True,
        )
        for wlabel, _s, _e in WINDOWS:
            da = ((_v(l, wlabel, "alpha") or 0) - (_v(b, wlabel, "alpha") or 0)) * 100
            ds = (_v(l, wlabel, "sharpe") or 0) - (_v(b, wlabel, "sharpe") or 0)
            print(f"    {wlabel}: Δalpha={da:+.2f}%p Δsharpe={ds:+.3f}", flush=True)

    print("\n=== 결과 JSON 요약 ===", flush=True)
    summary: dict[str, dict] = {"verdicts": {str(k): v for k, v in verdicts.items()}}
    for name in variants:
        summary[name] = {
            wlabel: {
                k: grid[name][wlabel].get(k)
                for k in [
                    "total_return", "cagr", "sharpe", "sortino", "mdd", "alpha", "beta",
                    "information_ratio", "avg_turnover", "avg_turnover_actual",
                    "num_rebalances", "num_kills",
                ]
            }
            for wlabel, _s, _e in [*WINDOWS, FULL]
        }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
