"""잔차(베타 조정) 모멘텀(Residual Momentum, Blitz·Huij·Martens 2011) PIT KOSPI200
walk-forward 검증 + id=23 대비 상관·분산 기여 + 원시 vs 잔차 모멘텀 IC/IR 비교.

설계(financial-expert 2순위, 저비용 스카우트):
- 개별 종목 월수익률을 시장(KOSPI200)에 회귀한 잔차의 형성창 정보비율로 원시 모멘텀을
  대체하는 옵트인 변형(사이즈는 스코어러 크로스섹션 중립화로 처리 — 외부 데이터 불요).
- id=23 팩터믹스에서 momentum 가중치를 residual_momentum 으로 그대로 옮겨(합=1.0) 스왑.
- 방어형 판정은 excess/IR 이 아니라 alpha/Sharpe(id=23 저베타 아티팩트 교훈).
- id=23 일간수익 상관계수를 직접 산출해 직교성(분산 기여)을 수치로 확인.
- avg_turnover_actual 필수 리포팅. 거래비용: id=23 config 그대로(왕복 ≈0.33% ≥ 0.23%),
  체결 next_close + 슬리피지 유지.
- 원시 모멘텀(score_momentum) 단독 IC/IR vs 잔차 모멘텀(score_residual_momentum) 단독
  IC/IR 을 동일 PIT 구간에서 나란히 비교(역효과 해소 여부 확인).

패널·PIT 풀·레짐·벤치마크는 한 번만 조립. 잔차 모멘텀은 (reg,win,skip,as_of)로 캐시.
컨테이너: docker compose exec -T web python scripts/validate_residual_momentum.py
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import numpy as np
import pandas as pd

# 설정 로드(KRX 로그인용 KRX_ID/PW 를 os.environ 에 주입 — pykrx MDC 필수).
from app.core.config import settings  # noqa: F401

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
from app.services.metrics.factors import compute_residual_momentum_panel
from app.services.backtest.portfolio import run_rebalance_backtest
from app.services.data.loader import get_close_series, load_ohlcv, upsert_price_ticks
from sqlalchemy import select

# 36개월 회귀를 2021-01 시점에 확보하려면 2018-01 이전 종가가 필요하다.
WARMUP_START = date(2018, 1, 1)
PERIOD_END = date(2025, 6, 30)
WINDOWS = [
    ("H1(21.1-23.6)", date(2021, 1, 1), date(2023, 6, 30)),
    ("H2(23.7-25.6)", date(2023, 7, 1), date(2025, 6, 30)),
]
FULL = ("FULL(21.1-25.6)", date(2021, 1, 1), date(2025, 6, 30))
BASE = "id23(기준)"

# (reg_window, mom_window, skip) 스윕.
GRID = [(36, 11, 1), (24, 11, 1), (36, 6, 1), (24, 6, 1)]

_RM_CACHE: dict[tuple, pd.Series] = {}
_PANEL: pd.DataFrame | None = None
_MARKET: pd.Series | None = None


def _swap_momentum_to_resid(w23: dict) -> dict:
    """id=23 momentum 가중치를 residual_momentum 으로 그대로 옮긴다(합 보존, 원시 momentum=0)."""
    out = dict(w23)
    mom = float(out.get("momentum", 0.0) or 0.0)
    out["momentum"] = 0.0
    out["residual_momentum"] = round(mom, 6)
    return out


def _make_provider(reg: int, win: int, skip: int):
    """_fundamentals_provider(연간) 결과에 resid_mom 컬럼을 덧붙이는 provider(캐시)."""
    def prov(as_of_date, codes):
        fdf = _fundamentals_provider(as_of_date, codes, use_ttm=False)
        norm = [str(c).zfill(6) for c in codes]
        key = (reg, win, skip, as_of_date.isoformat())
        rm = _RM_CACHE.get(key)
        if rm is None:
            rm = compute_residual_momentum_panel(
                _PANEL, _MARKET, as_of_date, codes=norm,
                reg_window=reg, mom_window=win, skip=skip,
            )
            _RM_CACHE[key] = rm
        s = rm.reindex(norm)
        if fdf is None:
            return pd.DataFrame({"resid_mom": s})
        fdf = fdf.copy()
        fdf["resid_mom"] = s.reindex(fdf.index)
        return fdf
    return prov


async def main() -> None:
    global _PANEL, _MARKET
    async with AsyncSessionLocal() as db:
        s = await db.scalar(select(Strategy).where(Strategy.id == 23))
        cfg23 = dict(s.config)
    w23 = dict(cfg23["selection"]["factor_weights"])
    print(f"id=23 weights: {w23}", flush=True)

    pit_union, pool_provider = _build_pit_pool(cfg23, WARMUP_START, PERIOD_END)
    universe = pit_union if pit_union is not None else list(cfg23.get("universe", []))
    print(f"PIT union universe: {len(universe)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(PERIOD_END, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for i, sym in enumerate(universe):
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
            if (i + 1) % 50 == 0:
                print(f"  ...패널 {i + 1}/{len(universe)}", flush=True)
    _PANEL = pd.DataFrame(columns)
    print(f"패널 완성: {_PANEL.shape}", flush=True)

    rf0 = cfg23.get("regime_filter") or {}
    reg_ticker = _REGIME_INDEX_TICKER.get(rf0.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, PERIOD_END, reg_ticker)
    bench_ticker = _BENCHMARK_TICKER.get(cfg23.get("benchmark_index", "KOSPI200"), "1028")
    _MARKET = await asyncio.to_thread(_load_regime_series, WARMUP_START, PERIOD_END, bench_ticker)
    print(f"레짐/벤치(=시장 회귀 소스) 적재: regime={regime_series is not None} "
          f"bench={_MARKET is not None}", flush=True)

    # 후보: BASE(id23) + (reg,win,skip) 스윕 잔차 모멘텀 스왑.
    base_prov = _fundamentals_provider
    cands: dict[str, tuple[dict, object]] = {BASE: (copy.deepcopy(cfg23), base_prov)}
    for reg, win, skip in GRID:
        cfg = copy.deepcopy(cfg23)
        cfg["selection"]["factor_weights"] = _swap_momentum_to_resid(w23)
        cfg["selection"]["resid_mom_reg_window"] = reg
        cfg["selection"]["resid_mom_window"] = win
        cfg["selection"]["resid_mom_skip"] = skip
        cands[f"resid r{reg} w{win} s{skip}"] = (cfg, _make_provider(reg, win, skip))

    def run(cfg, prov, sd, ed):
        r = run_rebalance_backtest(
            _PANEL, cfg, _to_dt(sd), _to_dt(ed, end=True),
            prov, regime_series, pool_provider, _MARKET,
        )

        def _n(x):
            return float("nan") if x is None else float(x)

        metrics = {k: _n(r.get(k)) for k in (
            "total_return", "sharpe", "mdd", "alpha", "beta",
            "information_ratio", "avg_turnover", "avg_turnover_actual",
        )}
        return metrics, r.get("equity_curve") or [], r.get("factor_ic") or {}

    def _fmt(m):
        return (f"ret={m['total_return']*100:+7.1f}% shp={m['sharpe']:5.2f} "
                f"mdd={m['mdd']*100:6.1f}% a={m['alpha']*100:+5.1f}% b={m['beta']:.2f} "
                f"IR={m['information_ratio']:+.2f} turn(a)={m['avg_turnover_actual']*100:5.1f}%")

    def _shp(m):
        return m["sharpe"] if m["sharpe"] == m["sharpe"] else float("-inf")

    grid: dict[str, dict[str, dict]] = {}
    rets_full: dict[str, pd.Series] = {}
    fic_full: dict[str, dict] = {}
    print("\n=== 후보별 성과 (반기 2-fold + 전체, 레짐 ON, 벤치=KOSPI200) ===", flush=True)
    for name, (cfg, prov) in cands.items():
        grid[name] = {}
        for wlabel, sd, ed in [*WINDOWS, FULL]:
            m, eq, fic = run(cfg, prov, sd, ed)
            grid[name][wlabel] = m
            print(f"[{name:18}] {wlabel}: {_fmt(m)}", flush=True)
            if wlabel == FULL[0]:
                fic_full[name] = fic
                if eq:
                    ser = pd.Series({pd.Timestamp(p["t"]): float(p["v"]) for p in eq})
                    rets_full[name] = ser.pct_change().dropna()

    # 원시 모멘텀 vs 잔차 모멘텀 단독 IC/IR (동일 PIT 구간, FULL). 스왑 변형 run 의
    # factor_ic 에는 두 팩터가 모두 산출된다(attribution 은 가중치와 무관하게 전 팩터 IC 계산).
    print("\n=== 원시 모멘텀 vs 잔차 모멘텀 단독 IC/IR (FULL, 동일 PIT 구간) ===", flush=True)
    for name in cands:
        if name == BASE:
            continue
        fic = fic_full.get(name) or {}
        rm_ic = fic.get("score_residual_momentum")
        mo_ic = fic.get("score_momentum")
        if rm_ic and mo_ic:
            print(f"  [{name:18}] 원시:  IC={mo_ic['ic_mean']:+.3f} IR={mo_ic['ic_ir']:+.2f} "
                  f"hit={mo_ic['ic_hit']:.2f} LS={mo_ic['ls_return']:+.3f}", flush=True)
            print(f"  {'':18}  잔차:  IC={rm_ic['ic_mean']:+.3f} IR={rm_ic['ic_ir']:+.2f} "
                  f"hit={rm_ic['ic_hit']:.2f} LS={rm_ic['ls_return']:+.3f}", flush=True)

    # id=23 대비 상관 + 50/50 결합(분산 효과) — FULL.
    print("\n=== id=23 대비 상관 · 50/50 결합(분산 효과) — FULL ===", flush=True)
    base_r = rets_full.get(BASE)
    for name in cands:
        if name == BASE or name not in rets_full or base_r is None:
            continue
        a, b = rets_full[name].align(base_r, join="inner")
        if len(a) < 30:
            print(f"  {name:18} (구간 부족)", flush=True)
            continue
        corr = float(a.corr(b))
        comb = 0.5 * a + 0.5 * b
        csh = float(comb.mean() / comb.std(ddof=1) * np.sqrt(252)) if comb.std(ddof=1) > 0 else float("nan")
        ceq = (1 + comb).cumprod()
        cmdd = float((ceq / ceq.cummax() - 1).min())
        print(f"  {name:18} corr={corr:+.2f} | 결합Sharpe={csh:.2f} "
              f"(단독 {_shp(grid[name][FULL[0]]):.2f}, id23 {_shp(grid[BASE][FULL[0]]):.2f}) "
              f"결합MDD={cmdd*100:.1f}%", flush=True)

    # Walk-forward 판정: 양 반기 모두 id=23 을 alpha·Sharpe 로 이기는가(방어형 기준).
    print("\n=== 판정: 양 반기 모두 id=23 alpha·Sharpe 초과? ===", flush=True)
    w1, w2 = WINDOWS[0][0], WINDOWS[1][0]
    for name in cands:
        if name == BASE:
            continue
        def beat(w):
            m, bm = grid[name][w], grid[BASE][w]
            return (_shp(m) > _shp(bm)) and (m["alpha"] > bm["alpha"])
        b1, b2 = beat(w1), beat(w2)
        verdict = "채택후보(양반기 우위)" if (b1 and b2) else ("일부우위" if (b1 or b2) else "열위")
        print(f"  {name:18} H1 a={grid[name][w1]['alpha']*100:+.1f}%/shp{_shp(grid[name][w1]):.2f} "
              f"vs {grid[BASE][w1]['alpha']*100:+.1f}%/{_shp(grid[BASE][w1]):.2f} | "
              f"H2 a={grid[name][w2]['alpha']*100:+.1f}%/shp{_shp(grid[name][w2]):.2f} "
              f"vs {grid[BASE][w2]['alpha']*100:+.1f}%/{_shp(grid[BASE][w2]):.2f} → {verdict}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
