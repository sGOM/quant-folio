"""패닉 오버레이(P2) 비교 백테스트 — id=23(단독) vs A(패닉 재진입 가속기) vs B(순수 이벤트).

register_and_validate_abc.py / validate_candidates.py 패턴 재사용: PIT KOSPI200
유니버스·펀더멘털·레짐·패닉 시계열을 조립해 run_rebalance_backtest 로 전체 지표를 얻는다.
패닉 시계열은 로컬 파일 캐시(app.services.metrics.panic._BREADTH_CACHE_DIR)를 사용하므로
scripts/precompute_panic_series.py 로 미리 캐시를 채워두면 이 스크립트는 빠르게 끝난다.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import pandas as pd

from app.api.routes.backtests import (
    _BENCHMARK_TICKER,
    _REGIME_INDEX_TICKER,
    _build_pit_pool,
    _fundamentals_provider,
    _load_panic_series,
    _load_regime_series,
    _to_dt,
)
from app.core.config import settings  # noqa: F401  (KRX_ID/PW env 주입)
from app.core.database import AsyncSessionLocal
from app.models import Strategy
from app.services.backtest.portfolio import run_rebalance_backtest
from app.services.data.loader import get_close_series, load_ohlcv, upsert_price_ticks
from sqlalchemy import select

BASE_ID = 23
BASE = "id23(균형·기준)"

# 표본 확충 재검증(코디네이터 지시): scripts/precompute_panic_series.py 로 브레드스 캐시를
# 2019-01-02~2025-06-30(1595 거래일) 전 구간 적재 완료. 그 커버리지에 맞춰 sim 구간도
# 전체로 확장한다(2022 약세장·2024-08 엔캐리 청산 패닉 에피소드까지 포함).
SIM_START = date(2019, 1, 2)
SIM_END = date(2025, 6, 30)
WARMUP_START = date(2017, 6, 1)   # 팩터·모멘텀(52주고가 등) 워밍업(가격 패널용, 패닉 시계열엔 불필요)

FULL_WINDOW = ("FULL(2019.01-2025.06)", SIM_START, SIM_END)
# 2020-03 에피소드 과최적화 점검용 — 이 구간을 제외한 out-of-sample 윈도우
# (크래시 이후 전 구간: 오버레이가 허위발동으로 성과를 깎지 않는지 확인).
OOS_WINDOW = ("OOS(2020.07-2025.06, 2020-03 제외)", date(2020, 7, 1), SIM_END)

# 에피소드별 수익 분해(특정 이벤트 과최적화 여부 확인) — 3대 패닉 에피소드 + 저패닉 대조년.
EPISODE_WINDOWS = [
    ("EP2020-03(코로나 크래시)", date(2020, 2, 1), date(2020, 4, 30)),
    ("EP2022(약세장)", date(2022, 1, 1), date(2022, 12, 30)),
    ("EP2024-08(엔캐리 청산)", date(2024, 7, 1), date(2024, 9, 30)),
    ("CALM2023(저패닉 대조년)", date(2023, 1, 1), date(2023, 12, 28)),
]

OVERLAY_A = {
    "enabled": True, "market": "KOSPI", "arm_level": "warning", "arm_window": 5,
    "hold_days": 20, "profit_reclaim_pct": 0.5, "knife_stop_pct": 0.05,
    "base_exposure": 0.70, "panic_exposure": 1.00, "scale_in_confirm": 0.5,
    "ma_recovery_period": 20, "event_only": False,
}
OVERLAY_B = {
    "enabled": True, "market": "KOSPI", "arm_level": "warning", "arm_window": 5,
    "hold_days": 20, "profit_reclaim_pct": 0.5, "knife_stop_pct": 0.05,
    "base_exposure": 0.0, "panic_exposure": 1.00, "scale_in_confirm": 1.0,
    "ma_recovery_period": 20, "event_only": True,
}


async def _load_base_config() -> dict:
    async with AsyncSessionLocal() as db:
        s = await db.scalar(select(Strategy).where(Strategy.id == BASE_ID))
        if s is None:
            raise SystemExit(f"전략 id={BASE_ID} 를 찾을 수 없습니다.")
        return dict(s.config)


def _make_a(cfg23: dict) -> dict:
    """대표안 A: id=23 코어(선정·비중·레짐) 그대로 + panic_overlay 추가."""
    cfg = json.loads(json.dumps(cfg23))
    cfg["panic_overlay"] = dict(OVERLAY_A)
    return cfg


def _make_b(cfg23: dict) -> dict:
    """대조군 B: 순수 이벤트 진입 — 상시 core 없음(top_n=15 동일비중), regime_filter 미사용."""
    cfg = json.loads(json.dumps(cfg23))
    cfg["selection"] = {**cfg["selection"], "top_n": 15}
    cfg["weighting"] = "equal"
    cfg["drift_band_pct"] = 0.02
    cfg["regime_filter"] = {**(cfg.get("regime_filter") or {}), "enabled": False}
    cfg["panic_overlay"] = dict(OVERLAY_B)
    return cfg


async def main() -> None:
    cfg23 = await _load_base_config()
    all_cfgs = {BASE: cfg23, "A 패닉재진입가속기": _make_a(cfg23), "B 순수이벤트진입": _make_b(cfg23)}

    print("=== 1) PIT 유니버스·패널 조립 ===", flush=True)
    # A/B 모두 id=23 과 동일한 selection.universe_rule 을 재사용하므로(설계상 "코어를 그대로
    # 재사용"), 서명이 같은 규칙은 PIT 풀 조회(krx_index, 월별 네트워크 호출)를 1회만 수행해
    # 3배 중복 호출을 피한다.
    def _rule_sig(cfg: dict) -> str:
        return json.dumps((cfg.get("selection") or {}).get("universe_rule") or {}, sort_keys=True)

    pool_cache: dict[str, tuple] = {}
    pools: dict[str, tuple] = {}
    full_universe: set[str] = set()
    for name, cfg in all_cfgs.items():
        sig = _rule_sig(cfg)
        if sig not in pool_cache:
            print(f"  PIT 풀 조회 중... ({name})", flush=True)
            pool_cache[sig] = _build_pit_pool(cfg, WARMUP_START, SIM_END)
        pools[name] = pool_cache[sig]
        u, _prov = pools[name]
        if u:
            full_universe |= set(u)
    full_universe = sorted(full_universe)
    print(f"통합 PIT universe: {len(full_universe)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(SIM_END, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for n, sym in enumerate(full_universe, 1):
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
            if n % 25 == 0:
                print(f"  가격 패널 진행: {n}/{len(full_universe)}", flush=True)
    panel = pd.DataFrame(columns)
    print(f"패널 완성: {panel.shape}", flush=True)

    rf0 = cfg23.get("regime_filter") or {}
    regime_ticker = _REGIME_INDEX_TICKER.get(rf0.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, SIM_END, regime_ticker)

    bench_ticker = _BENCHMARK_TICKER.get(cfg23.get("benchmark_index", "KOSPI200"), "1028")
    benchmark_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, SIM_END, bench_ticker)
    print(f"레짐/벤치마크 시리즈 로드 완료", flush=True)

    print("\n=== 2) 패닉 시계열 로드(로컬 캐시 활용) ===", flush=True)
    panic_series = await asyncio.to_thread(_load_panic_series, SIM_START, SIM_END, "KOSPI")
    if panic_series is None:
        print("  경고: 패닉 시계열을 확보하지 못했습니다(오버레이 미적용으로 A/B가 id23과 동일해짐).", flush=True)
    else:
        print(f"  패닉 시계열 {len(panic_series)}행, level 분포: {panic_series['level'].value_counts().to_dict()}", flush=True)

    def run(cfg, pool_prov, s, e):
        return run_rebalance_backtest(
            panel, cfg, _to_dt(s), _to_dt(e, end=True),
            _fundamentals_provider, regime_series, pool_prov, benchmark_series, panic_series,
        )

    ALL_WINDOWS = [FULL_WINDOW, OOS_WINDOW, *EPISODE_WINDOWS]

    print("\n=== 3) 성과 비교 (FULL + OOS(2020-03 제외) + 에피소드별) ===", flush=True)
    grid: dict[str, dict[str, dict]] = {}
    rets_full: dict[str, pd.Series] = {}
    for name, cfg in all_cfgs.items():
        _u, prov = pools[name]
        grid[name] = {}
        for wlabel, s, e in ALL_WINDOWS:
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
            nt = r.get("num_trades")
            npe = r.get("num_panic_events")
            print(
                f"[{name:20}] {wlabel:32}: ret={tr*100:+7.2f}% cagr={(cagr or 0)*100:+6.2f}% "
                f"sharpe={sh if sh is not None else float('nan'):5.2f} sortino={so if so is not None else float('nan'):5.2f} "
                f"mdd={mdd*100:6.2f}% alpha={(alpha or 0)*100:+6.2f}% beta={beta if beta is not None else float('nan'):5.2f} "
                f"IR={ir if ir is not None else float('nan'):5.2f} trades={nt} panic_events={npe}",
                flush=True,
            )
            if wlabel == FULL_WINDOW[0]:
                eq = r.get("equity_curve") or []
                if eq:
                    ser = pd.Series({pd.Timestamp(p["t"]): float(p["v"]) for p in eq})
                    rets_full[name] = ser.pct_change().dropna()

    print("\n=== 4) id=23 대비 일별수익 상관(FULL) — <0.5 면 보완재 근거 ===", flush=True)
    base_r = rets_full.get(BASE)
    for name in all_cfgs:
        if name == BASE or name not in rets_full or base_r is None:
            continue
        a, b = rets_full[name].align(base_r, join="inner")
        if len(a) < 30:
            print(f"  {name:20} (구간 부족)", flush=True)
            continue
        corr = float(a.corr(b))
        print(f"  {name:20} corr(id23)={corr:+.3f}", flush=True)

    print("\n=== 4b) 표본 충분성(오버레이 발동 횟수) ===", flush=True)
    for name in all_cfgs:
        if name == BASE:
            continue
        total_events = sum(grid[name][w[0]].get("num_panic_events") or 0 for w in [FULL_WINDOW])
        total_trades = grid[name][FULL_WINDOW[0]].get("num_trades") or 0
        verdict = "충분(>=20)" if total_events >= 20 else "부족(<20) — 일화적 해석만 가능"
        print(f"  {name:20} FULL 발동={total_events}건 거래={total_trades}건 → {verdict}", flush=True)

    print("\n=== 5) 결과 JSON 덤프 ===", flush=True)
    summary = {}
    for name in all_cfgs:
        summary[name] = {}
        for wlabel, _s, _e in ALL_WINDOWS:
            r = grid[name][wlabel]
            summary[name][wlabel] = {
                k: r.get(k) for k in [
                    "total_return", "cagr", "sharpe", "sortino", "mdd", "alpha", "beta",
                    "information_ratio", "tracking_error", "excess_return", "avg_turnover",
                    "benchmark_return", "num_rebalances", "num_kills", "num_trades", "num_panic_events",
                ]
            }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
