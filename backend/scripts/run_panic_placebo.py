"""패닉 오버레이 교란변수 분리(플라시보) 백테스트.

## 검증 질문
Design A(패닉 재진입 가속기)의 MDD 개선이 **패닉 신호의 타이밍 알파**인가,
아니면 단지 base_exposure=0.70 이 만드는 **상시 30% 현금 드래그** 아티팩트인가?

## 4-암 설계 (단일 변수 조작)
- id23         : 오버레이 없음(기준)
- A_full       : base=1.00, panic=1.00 → 오버레이 무해화(항상 풀노출). ≈id23 여야 함(배관 새너티).
- A0_static70  : base=0.70, panic=0.70 → 확인/스케일/청산 모든 exposure 가 0.70 로 수렴,
                 즉 상시 70% 고정. "가속 없는" 순수 현금 드래그 플라시보.
- A            : base=0.70, panic=1.00 → 원안(확인 시 풀노출 가속).

## 판정
- A_full ≈ id23        → 오버레이 배관이 성과를 왜곡하지 않음(전제 검증).
- A0 vs id23           → 상시 30% 현금 드래그의 순효과(수익↓·MDD↓ 예상).
- **A vs A0(결정적)**  → 패닉 타이밍 가속의 순효과. A 가 유사 MDD 에서 수익이 유의하게
                         높으면 신호에 진짜 알파. A ≈ A0 이면 신호는 무력(드래그가 전부).

run_panic_overlay_comparison.py 의 조립 파이프라인을 그대로 재사용한다(동일 PIT
유니버스·가격패널·레짐·벤치마크·패닉 시계열). 브레드스는 로컬 캐시 활용.
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
BASE = "id23(기준)"

SIM_START = date(2019, 1, 2)
SIM_END = date(2025, 6, 30)
WARMUP_START = date(2017, 6, 1)

FULL_WINDOW = ("FULL(2019.01-2025.06)", SIM_START, SIM_END)
OOS_WINDOW = ("OOS(2020.07-2025.06, 2020-03 제외)", date(2020, 7, 1), SIM_END)
EPISODE_WINDOWS = [
    ("EP2020-03(코로나)", date(2020, 2, 1), date(2020, 4, 30)),
    ("EP2022(약세장)", date(2022, 1, 1), date(2022, 12, 30)),
    ("EP2024-08(엔캐리)", date(2024, 7, 1), date(2024, 9, 30)),
]

# 공통 오버레이 파라미터(A 원안과 동일) — base/panic_exposure 만 암별로 달리한다.
_COMMON = {
    "enabled": True, "market": "KOSPI", "arm_level": "warning", "arm_window": 5,
    "hold_days": 20, "profit_reclaim_pct": 0.5, "knife_stop_pct": 0.05,
    "scale_in_confirm": 0.5, "ma_recovery_period": 20, "event_only": False,
}
OVERLAY_A_FULL = {**_COMMON, "base_exposure": 1.00, "panic_exposure": 1.00}
OVERLAY_A0 = {**_COMMON, "base_exposure": 0.70, "panic_exposure": 0.70}
OVERLAY_A = {**_COMMON, "base_exposure": 0.70, "panic_exposure": 1.00}


async def _load_base_config() -> dict:
    async with AsyncSessionLocal() as db:
        s = await db.scalar(select(Strategy).where(Strategy.id == BASE_ID))
        if s is None:
            raise SystemExit(f"전략 id={BASE_ID} 를 찾을 수 없습니다.")
        return dict(s.config)


def _with_overlay(cfg23: dict, overlay: dict | None) -> dict:
    cfg = json.loads(json.dumps(cfg23))
    if overlay is not None:
        cfg["panic_overlay"] = dict(overlay)
    else:
        cfg.pop("panic_overlay", None)
    return cfg


async def main() -> None:
    cfg23 = await _load_base_config()
    all_cfgs = {
        BASE: _with_overlay(cfg23, None),
        "A_full(1.00/1.00)": _with_overlay(cfg23, OVERLAY_A_FULL),
        "A0_static70(0.70/0.70)": _with_overlay(cfg23, OVERLAY_A0),
        "A(0.70/1.00)": _with_overlay(cfg23, OVERLAY_A),
    }

    print("=== 1) PIT 유니버스·패널 조립 ===", flush=True)
    # 4개 암 모두 동일 selection.universe_rule → PIT 풀 조회는 1회만.
    _u, prov = _build_pit_pool(cfg23, WARMUP_START, SIM_END)
    universe = sorted(set(_u or []))
    print(f"PIT universe: {len(universe)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(SIM_END, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for n, sym in enumerate(universe, 1):
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
                print(f"  가격 패널 진행: {n}/{len(universe)}", flush=True)
    panel = pd.DataFrame(columns)
    print(f"패널 완성: {panel.shape}", flush=True)

    rf0 = cfg23.get("regime_filter") or {}
    regime_ticker = _REGIME_INDEX_TICKER.get(rf0.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, SIM_END, regime_ticker)
    bench_ticker = _BENCHMARK_TICKER.get(cfg23.get("benchmark_index", "KOSPI200"), "1028")
    benchmark_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, SIM_END, bench_ticker)
    print("레짐/벤치마크 시리즈 로드 완료", flush=True)

    print("\n=== 2) 패닉 시계열 로드(로컬 캐시) ===", flush=True)
    panic_series = await asyncio.to_thread(_load_panic_series, SIM_START, SIM_END, "KOSPI")
    if panic_series is None:
        raise SystemExit("패닉 시계열을 확보하지 못했습니다(캐시 확인 필요) — 실험 중단.")
    print(f"  패닉 시계열 {len(panic_series)}행, level 분포: "
          f"{panic_series['level'].value_counts().to_dict()}", flush=True)

    def run(cfg, s, e):
        return run_rebalance_backtest(
            panel, cfg, _to_dt(s), _to_dt(e, end=True),
            _fundamentals_provider, regime_series, prov, benchmark_series, panic_series,
        )

    ALL_WINDOWS = [FULL_WINDOW, OOS_WINDOW, *EPISODE_WINDOWS]

    print("\n=== 3) 성과 비교 ===", flush=True)
    grid: dict[str, dict[str, dict]] = {}
    rets_full: dict[str, pd.Series] = {}
    for name, cfg in all_cfgs.items():
        grid[name] = {}
        for wlabel, s, e in ALL_WINDOWS:
            r = run(cfg, s, e)
            grid[name][wlabel] = r
            print(
                f"[{name:24}] {wlabel:34}: ret={(r.get('total_return') or 0)*100:+7.2f}% "
                f"cagr={(r.get('cagr') or 0)*100:+6.2f}% "
                f"sharpe={(r.get('sharpe') if r.get('sharpe') is not None else float('nan')):5.2f} "
                f"sortino={(r.get('sortino') if r.get('sortino') is not None else float('nan')):5.2f} "
                f"mdd={(r.get('mdd') or 0)*100:6.2f}% "
                f"alpha={(r.get('alpha') or 0)*100:+6.2f}% "
                f"beta={(r.get('beta') if r.get('beta') is not None else float('nan')):5.2f} "
                f"IR={(r.get('information_ratio') if r.get('information_ratio') is not None else float('nan')):5.2f} "
                f"trades={r.get('num_trades')} panic_events={r.get('num_panic_events')}",
                flush=True,
            )
            if wlabel == FULL_WINDOW[0]:
                eq = r.get("equity_curve") or []
                if eq:
                    ser = pd.Series({pd.Timestamp(p["t"]): float(p["v"]) for p in eq})
                    rets_full[name] = ser.pct_change().dropna()

    print("\n=== 4) 결정적 판정: A vs A0 (패닉 타이밍 가속의 순효과) ===", flush=True)
    fa = grid["A(0.70/1.00)"][FULL_WINDOW[0]]
    fa0 = grid["A0_static70(0.70/0.70)"][FULL_WINDOW[0]]
    fid = grid[BASE][FULL_WINDOW[0]]
    ffull = grid["A_full(1.00/1.00)"][FULL_WINDOW[0]]

    def g(r, k):
        return r.get(k)

    print(f"  A_full ret/mdd/cagr = {(g(ffull,'total_return') or 0)*100:+.2f}% / "
          f"{(g(ffull,'mdd') or 0)*100:.2f}% / {(g(ffull,'cagr') or 0)*100:+.2f}%  "
          f"vs id23 {(g(fid,'total_return') or 0)*100:+.2f}% / {(g(fid,'mdd') or 0)*100:.2f}% / "
          f"{(g(fid,'cagr') or 0)*100:+.2f}%  → 배관 새너티(≈ 여야 함)", flush=True)
    print(f"  A0 vs id23 : Δret={((g(fa0,'total_return') or 0)-(g(fid,'total_return') or 0))*100:+.2f}%p "
          f"Δmdd={((g(fa0,'mdd') or 0)-(g(fid,'mdd') or 0))*100:+.2f}%p (현금드래그 순효과)", flush=True)
    d_ret = ((g(fa, 'total_return') or 0) - (g(fa0, 'total_return') or 0)) * 100
    d_mdd = ((g(fa, 'mdd') or 0) - (g(fa0, 'mdd') or 0)) * 100
    d_cagr = ((g(fa, 'cagr') or 0) - (g(fa0, 'cagr') or 0)) * 100
    d_alpha = ((g(fa, 'alpha') or 0) - (g(fa0, 'alpha') or 0)) * 100
    d_sharpe = (g(fa, 'sharpe') or 0) - (g(fa0, 'sharpe') or 0)
    print(f"  **A − A0 : Δret={d_ret:+.2f}%p  Δcagr={d_cagr:+.2f}%p  Δmdd={d_mdd:+.2f}%p  "
          f"Δalpha={d_alpha:+.2f}%p  Δsharpe={d_sharpe:+.3f}**", flush=True)
    n_events = g(fa, 'num_panic_events') or 0
    print(f"  발동 표본 n={n_events} → {'유의(>=20)' if n_events >= 20 else '부족(<20): 일화적 해석만'}",
          flush=True)
    verdict = (
        "패닉 타이밍이 드래그 위에 추가 알파를 만든다(신호 유효)"
        if (d_cagr > 0.5 and d_sharpe > 0.02)
        else "가속 효과 미미 — MDD 이득은 사실상 현금 드래그가 전부(신호 무력)"
    )
    print(f"  판정: {verdict}", flush=True)

    print("\n=== 5) id23 대비 일별수익 상관(FULL) ===", flush=True)
    base_r = rets_full.get(BASE)
    for name in all_cfgs:
        if name == BASE or name not in rets_full or base_r is None:
            continue
        a, b = rets_full[name].align(base_r, join="inner")
        if len(a) >= 30:
            print(f"  {name:24} corr(id23)={float(a.corr(b)):+.3f}", flush=True)

    print("\n=== 6) 결과 JSON 덤프 ===", flush=True)
    summary = {}
    for name in all_cfgs:
        summary[name] = {}
        for wlabel, _s, _e in ALL_WINDOWS:
            r = grid[name][wlabel]
            summary[name][wlabel] = {
                k: r.get(k) for k in [
                    "total_return", "cagr", "sharpe", "sortino", "mdd", "alpha", "beta",
                    "information_ratio", "tracking_error", "excess_return", "avg_turnover",
                    "num_trades", "num_panic_events", "num_kills",
                ]
            }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
