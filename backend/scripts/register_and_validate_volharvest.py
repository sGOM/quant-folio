"""변동성 수확 게이트 전략(품질 점수 + vol_gate + 현금 대기)을 등록하고 PIT 검증한다.

financial-expert 설계: 품질·모멘텀 중심 종합점수(min_score 하한) + 절대 변동성
적격 게이트(RV20/RV252 급등비·상방 추세·과열 상한)를 통과한 종목이 있을 때만
진입, 없으면 전량 현금. register_and_validate_abc.py 패턴 재사용.

추가 산출:
- 가동률(invested %): markers 를 스텝 시계열로 재구성해 투자일 비율을 구한다.
- 민감도: spike_min 1.3/1.5 변형을 동질 시행으로 돌려 DSR(과최적화 관문)을 계산.
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
from app.models import Strategy, StrategyStatus
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
BASE = "id23(균형·기준)"
OWNER_USER_ID = 1

MAIN_NAME = "변동성 수확 게이트(품질+vol_gate)"

VOL_CONFIG = {
    "type": "rebalance", "universe": [],
    "selection": {
        "method": "score", "top_n": 5, "min_score": 0.5,
        "factor_weights": {"momentum": 0.30, "value": 0.15, "lowvol": 0.0, "quality": 0.40, "growth": 0.15},
        "neutralize": "size",
        "vol_gate": {"spike_lookback": 20, "base_lookback": 252, "spike_min": 1.4, "cap": 0.90, "require_uptrend": True},
        "universe_rule": {"type": "momentum", "source": "KOSPI200", "lookback": 60, "pick": 40, "min_market_cap": 5000},
    },
    "weighting": "equal", "cadence": "weekly", "rebalance_weekday": 0, "rebalance_time": "14:30",
    "drift_band_pct": 0.03,
    "regime_filter": {"enabled": True, "index": "KOSPI", "ma_period": 200, "reentry_buffer_pct": 0.02, "exit_buffer_pct": 0.05},
    "risk_layer": {"max_position_pct": 0.25, "mdd_kill_pct": 0.18, "mdd_rearm_days": 20},
    "capital": 10000000, "fees": 0.00015, "tax": 0.0020,
    "fill_mode": "next_close", "slippage_bps": 8.0, "slippage_vol_scale": 1.5,
    "price_limit_model": True,
    "benchmark_index": "KOSPI200", "risk_free_rate": 0.03,
}


def _variant(spike_min: float) -> dict:
    cfg = copy.deepcopy(VOL_CONFIG)
    cfg["selection"]["vol_gate"]["spike_min"] = spike_min
    return cfg


# 민감도 변형(성긴 격자만 — 촘촘한 그리드서치는 과최적화라 금지). DSR 동질 시행으로도 사용.
VARIANTS = {
    MAIN_NAME: VOL_CONFIG,
    "spike_min=1.3": _variant(1.3),
    "spike_min=1.5": _variant(1.5),
}


def _invested_ratio(result: dict) -> float | None:
    """markers 를 스텝 시계열로 재구성해 가동률(보유종목>0 인 날 비율)을 구한다.

    rebalance 마커는 체결 직후 보유종목 수를 담고, 그 외(레짐 이탈·킬 등) 마커는
    전량 청산이므로 0 으로 간주한다. 상태 변화는 반드시 체결(=마커)을 동반하므로
    마커 사이 구간은 직전 상태가 유지된다. 시작 상태는 현금(0).
    """
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


async def _register(name: str, cfg: dict) -> int:
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(Strategy).where(Strategy.user_id == OWNER_USER_ID, Strategy.name == name))
        if existing:
            existing.config = cfg
            existing.status = StrategyStatus.DRAFT
            s = existing
        else:
            s = Strategy(
                user_id=OWNER_USER_ID, name=name,
                description="financial-expert 설계 · 변동성 수확 게이트 · PIT KOSPI200 검증",
                config=cfg, status=StrategyStatus.DRAFT,
            )
            db.add(s)
        await db.flush()
        sid = s.id
        await db.commit()
    return sid


async def main() -> None:
    print("=== 0) 스키마 검증 ===", flush=True)
    from pydantic import TypeAdapter
    ta = TypeAdapter(StrategyConfig)
    for name, cfg in VARIANTS.items():
        ta.validate_python(cfg)
        print(f"  [OK] {name}", flush=True)

    print("\n=== 1) 등록(본 전략만) ===", flush=True)
    sid = await _register(MAIN_NAME, VOL_CONFIG)
    print(f"  {MAIN_NAME} -> id={sid}", flush=True)

    async with AsyncSessionLocal() as db:
        s23 = await db.scalar(select(Strategy).where(Strategy.id == 23))
        cfg23 = dict(s23.config)

    all_cfgs = {BASE: cfg23, **VARIANTS}

    print("\n=== 2) PIT 유니버스·패널 조립 ===", flush=True)
    pools: dict[str, tuple] = {BASE: _build_pit_pool(cfg23, WARMUP_START, PERIOD_END)}
    # 변형들은 universe_rule 이 동일하므로 본 전략의 풀을 공유한다.
    vol_pool = _build_pit_pool(VOL_CONFIG, WARMUP_START, PERIOD_END)
    for name in VARIANTS:
        pools[name] = vol_pool

    full_universe: set[str] = set(cfg23.get("universe", []) or [])
    for _name, (u, _p) in pools.items():
        if u:
            full_universe |= set(u)
    full_universe_sorted = sorted(full_universe)
    print(f"통합 PIT universe: {len(full_universe_sorted)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(PERIOD_END, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for sym in full_universe_sorted:
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
        return run_rebalance_backtest(
            panel, cfg, _to_dt(s), _to_dt(e, end=True),
            _fundamentals_provider, regime_series, pool_prov, benchmark_series,
        )

    print("\n=== 3) 성과 그리드 (반기 2-fold + FULL, 가동률 병기) ===", flush=True)
    grid: dict[str, dict[str, dict]] = {}
    rets_full: dict[str, pd.Series] = {}
    for name, cfg in all_cfgs.items():
        _u, prov = pools[name]
        grid[name] = {}
        for wlabel, s, e in [*WINDOWS, FULL]:
            r = run(cfg, prov, s, e)
            grid[name][wlabel] = r
            inv = _invested_ratio(r)
            print(
                f"[{name:28}] {wlabel}: ret={(r.get('total_return') or 0)*100:+7.2f}% "
                f"cagr={(r.get('cagr') or 0)*100:+6.2f}% "
                f"sharpe={r.get('sharpe') if r.get('sharpe') is not None else float('nan'):5.2f} "
                f"mdd={(r.get('mdd') or 0)*100:6.2f}% "
                f"alpha={(r.get('alpha') or 0)*100:+6.2f}% "
                f"beta={r.get('beta') if r.get('beta') is not None else float('nan'):5.2f} "
                f"IR={r.get('information_ratio') if r.get('information_ratio') is not None else float('nan'):5.2f} "
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

    print("\n=== 4) id=23 대비 상관 · 결합 포트폴리오(FULL) ===", flush=True)
    base_r = rets_full.get(BASE)
    for name in [MAIN_NAME]:
        if name not in rets_full or base_r is None:
            continue
        a, b = rets_full[name].align(base_r, join="inner")
        if len(a) < 30:
            print(f"  {name} (구간 부족)", flush=True)
            continue
        corr = float(a.corr(b))
        base_full = grid[BASE][FULL[0]]
        solo = grid[name][FULL[0]]
        for w in (0.15, 0.30, 0.50):
            comb = w * a + (1 - w) * b
            csh = float(comb.mean() / comb.std(ddof=1) * np.sqrt(252)) if comb.std(ddof=1) > 0 else float("nan")
            ceq = (1 + comb).cumprod()
            cmdd = float((ceq / ceq.cummax() - 1).min())
            print(
                f"  {name} corr={corr:+.2f} | {int(w*100)}/{int((1-w)*100)} 결합Sharpe={csh:.2f} "
                f"(단독 {solo.get('sharpe')}, id23 {base_full.get('sharpe')}) 결합MDD={cmdd*100:.1f}%",
                flush=True,
            )

    print("\n=== 5) DSR(과최적화 관문, 동질 시행=spike_min 격자 FULL) ===", flush=True)
    trial_names = list(VARIANTS.keys())
    sharpes, rets_list = [], []
    for name in trial_names:
        r = grid[name][FULL[0]]
        sh = r.get("sharpe")
        dr = returns_from_equity_curve(r.get("equity_curve") or [])
        if sh is not None and dr.size >= 2:
            sharpes.append(float(sh))
            rets_list.append(dr)
    if len(sharpes) >= 2:
        stats = estimate_trial_stats(sharpes, rets_list)
        main_r = grid[MAIN_NAME][FULL[0]]
        main_dr = returns_from_equity_curve(main_r.get("equity_curve") or [])
        dsr_out = compute_deflated_sharpe(
            main_dr, float(main_r.get("sharpe") or 0.0), stats["N_eff"], stats["V_daily"],
        )
        dsr = dsr_out.get("dsr")
        print(
            f"  N={stats['N']} N_eff={stats['N_eff']} rho_bar={stats['rho_bar']:.2f} "
            f"DSR={dsr if dsr is not None else 'None'} 등급={grade_dsr(dsr)}",
            flush=True,
        )
    else:
        print("  유효 시행 부족 — DSR 미산출", flush=True)

    print("\n=== 결과 JSON 덤프 (요약) ===", flush=True)
    summary = {}
    for name in all_cfgs:
        summary[name] = {}
        for wlabel, _s, _e in [*WINDOWS, FULL]:
            r = grid[name][wlabel]
            summary[name][wlabel] = {
                **{k: r.get(k) for k in [
                    "total_return", "cagr", "sharpe", "sortino", "mdd", "alpha", "beta",
                    "information_ratio", "tracking_error", "excess_return", "avg_turnover",
                    "avg_turnover_actual", "benchmark_return", "num_rebalances", "num_kills",
                ]},
                "invested_ratio": _invested_ratio(r),
            }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
