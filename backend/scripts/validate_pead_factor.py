"""PEAD(실적 서프라이즈 드리프트) 팩터 PIT KOSPI200 walk-forward 검증.

financial-expert 3순위 설계: OpenDART 정기공시 접수일(rcept_dt) 기준 PIT 로 단일분기
순이익 YoY 서프라이즈를 표준화(SUE, 컨센서스 부재로 기대치는 전년 동기 계절적 랜덤워크
프록시)한 새 팩터 score_pead 를 id=23(균형 멀티팩터·저베타)에 보완재로 삽입한다.

- lookback_q(SUE 표준화 분기수) ∈ {6,8,12} 스윕. pead 가중치는 id=23 믹스를 0.8 로
  축소하고 pead=0.2 삽입(합=1.0).
- 방어형 판정은 excess/IR 이 아니라 alpha/Sharpe(id=23 저베타 아티팩트 교훈).
- id=23 일간수익 상관계수를 직접 산출해 직교성(분산 기여)을 수치로 확인(앞선 flow·
  resid-mom 은 +0.87~0.92 로 실패 — 높게 나올 가능성 열어둠).
- PEAD 단독 IC/IR(factor_ic["score_pead"]) 리포팅(컨센서스 부재로 SUE 프록시가 약할 수
  있다는 경고 확인).
- avg_turnover_actual 필수. 거래비용: id=23 config 그대로(왕복 ≈0.33% ≥ 0.23%),
  체결 next_close + 슬리피지 유지.
- 캐던스 불일치 진단: 분기(기본) vs 월간 A/B 를 최적 lookback_q 로 짧게 확인.

컨테이너: docker compose exec -T web python scripts/validate_pead_factor.py
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

# 설정 로드(KRX 로그인용 KRX_ID/PW·OpenDART 키를 주입).
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
from app.services.metrics.factors import compute_pead_sue
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
PEAD_WEIGHT = 0.20
LOOKBACKS = [6, 8, 12]

_PEAD_CACHE: dict[tuple, pd.Series] = {}


def _pead_variant_weights(w23: dict, wp: float) -> dict:
    """id=23 팩터가중치를 (1-wp) 로 비례 축소하고 pead=wp 를 삽입한다(합=1.0)."""
    scale = 1.0 - wp
    out = {k: round(v * scale, 6) for k, v in w23.items()}
    out["pead"] = wp
    return out


def _make_provider(lookback_q: int):
    """_fundamentals_provider(연간) 결과에 pead_sue 컬럼을 덧붙이는 provider(캐시)."""
    def prov(as_of_date, codes):
        fdf = _fundamentals_provider(as_of_date, codes, use_ttm=False)
        norm = [str(c).zfill(6) for c in codes]
        key = (lookback_q, as_of_date.isoformat())
        pead = _PEAD_CACHE.get(key)
        if pead is None:
            pead = compute_pead_sue(norm, as_of_date, lookback_q=lookback_q)
            _PEAD_CACHE[key] = pead
        s = pead.reindex(norm)
        if fdf is None:
            return pd.DataFrame({"pead_sue": s})
        fdf = fdf.copy()
        fdf["pead_sue"] = s.reindex(fdf.index)
        return fdf
    return prov


def _n(x):
    return float("nan") if x is None else float(x)


def _fmt(m):
    return (f"ret={m['total_return']*100:+7.1f}% shp={m['sharpe']:5.2f} "
            f"mdd={m['mdd']*100:6.1f}% a={m['alpha']*100:+5.1f}% b={m['beta']:.2f} "
            f"IR={m['information_ratio']:+.2f} turn(a)={m['avg_turnover_actual']*100:5.1f}%")


def _shp(m):
    return m["sharpe"] if m["sharpe"] == m["sharpe"] else float("-inf")


async def main() -> None:
    async with AsyncSessionLocal() as db:
        s = await db.scalar(select(Strategy).where(Strategy.id == 23))
        cfg23 = dict(s.config)
    w23 = dict(cfg23["selection"]["factor_weights"])
    print(f"id=23 weights: {w23} cadence={cfg23.get('cadence')}", flush=True)

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
    panel = pd.DataFrame(columns)
    print(f"패널 완성: {panel.shape}", flush=True)

    rf0 = cfg23.get("regime_filter") or {}
    reg_ticker = _REGIME_INDEX_TICKER.get(rf0.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, PERIOD_END, reg_ticker)
    bench_ticker = _BENCHMARK_TICKER.get(cfg23.get("benchmark_index", "KOSPI200"), "1028")
    benchmark_series = await asyncio.to_thread(_load_regime_series, WARMUP_START, PERIOD_END, bench_ticker)
    print(f"레짐/벤치 적재: regime={regime_series is not None} bench={benchmark_series is not None}", flush=True)

    def run(cfg, prov, sd, ed):
        r = run_rebalance_backtest(
            panel, cfg, _to_dt(sd), _to_dt(ed, end=True),
            prov, regime_series, pool_provider, benchmark_series,
        )
        metrics = {k: _n(r.get(k)) for k in (
            "total_return", "sharpe", "mdd", "alpha", "beta",
            "information_ratio", "avg_turnover", "avg_turnover_actual",
        )}
        return metrics, r.get("equity_curve") or [], r.get("factor_ic") or {}

    # 후보: BASE(id23) + lookback_q 스윕 pead 변형.
    cands: dict[str, tuple[dict, object]] = {BASE: (copy.deepcopy(cfg23), _fundamentals_provider)}
    for lb in LOOKBACKS:
        cfg = copy.deepcopy(cfg23)
        cfg["selection"]["factor_weights"] = _pead_variant_weights(w23, PEAD_WEIGHT)
        cfg["selection"]["pead_lookback_q"] = lb
        cands[f"pead lb{lb}"] = (cfg, _make_provider(lb))

    grid: dict[str, dict[str, dict]] = {}
    rets_full: dict[str, pd.Series] = {}
    fic_full: dict[str, dict] = {}
    print("\n=== 후보별 성과 (반기 2-fold + 전체, 레짐 ON, 벤치=KOSPI200, 분기 리밸런싱) ===", flush=True)
    for name, (cfg, prov) in cands.items():
        grid[name] = {}
        for wlabel, sd, ed in [*WINDOWS, FULL]:
            m, eq, fic = run(cfg, prov, sd, ed)
            grid[name][wlabel] = m
            print(f"[{name:12}] {wlabel}: {_fmt(m)}", flush=True)
            if wlabel == FULL[0]:
                fic_full[name] = fic
                if eq:
                    ser = pd.Series({pd.Timestamp(p["t"]): float(p["v"]) for p in eq})
                    rets_full[name] = ser.pct_change().dropna()

    # PEAD 단독 IC/IR (동일 PIT 구간, FULL). attribution 은 가중치 무관 전 팩터 IC 계산.
    print("\n=== PEAD 단독 IC/IR (FULL, 분기) — 예측력 확인 ===", flush=True)
    for name in cands:
        if name == BASE:
            continue
        fic = fic_full.get(name) or {}
        pic = fic.get("score_pead")
        if pic:
            print(f"  [{name:12}] IC={pic['ic_mean']:+.3f} IR={pic['ic_ir']:+.2f} "
                  f"hit={pic['ic_hit']:.2f} LS={pic['ls_return']:+.3f} n={pic.get('n')}", flush=True)

    # id=23 대비 상관 + 50/50 결합(분산 효과) — FULL.
    print("\n=== id=23 대비 상관 · 50/50 결합(분산 효과) — FULL ===", flush=True)
    base_r = rets_full.get(BASE)
    for name in cands:
        if name == BASE or name not in rets_full or base_r is None:
            continue
        a, b = rets_full[name].align(base_r, join="inner")
        if len(a) < 30:
            print(f"  {name:12} (구간 부족)", flush=True)
            continue
        corr = float(a.corr(b))
        comb = 0.5 * a + 0.5 * b
        csh = float(comb.mean() / comb.std(ddof=1) * np.sqrt(252)) if comb.std(ddof=1) > 0 else float("nan")
        ceq = (1 + comb).cumprod()
        cmdd = float((ceq / ceq.cummax() - 1).min())
        print(f"  {name:12} corr={corr:+.2f} | 결합Sharpe={csh:.2f} "
              f"(단독 {_shp(grid[name][FULL[0]]):.2f}, id23 {_shp(grid[BASE][FULL[0]]):.2f}) "
              f"결합MDD={cmdd*100:.1f}%", flush=True)

    # Walk-forward 판정.
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
        print(f"  {name:12} H1 a={grid[name][w1]['alpha']*100:+.1f}%/shp{_shp(grid[name][w1]):.2f} "
              f"vs {grid[BASE][w1]['alpha']*100:+.1f}%/{_shp(grid[BASE][w1]):.2f} | "
              f"H2 a={grid[name][w2]['alpha']*100:+.1f}%/shp{_shp(grid[name][w2]):.2f} "
              f"vs {grid[BASE][w2]['alpha']*100:+.1f}%/{_shp(grid[BASE][w2]):.2f} → {verdict}", flush=True)

    # 캐던스 A/B: PEAD 드리프트(~60거래일)와 분기 리밸런싱 불일치 진단. 최적 lookback_q 로
    # 월간 vs 분기 비교(월간이 명확히 우월하면 캐던스가 알파를 깎았다는 증거).
    best_lb = max(LOOKBACKS, key=lambda lb: _shp(grid[f"pead lb{lb}"][FULL[0]]))
    print(f"\n=== 캐던스 A/B (분기 vs 월간, pead lb{best_lb}, FULL) ===", flush=True)
    for cad in ("quarterly", "monthly"):
        for tag, wsrc in (("id23", None), (f"pead lb{best_lb}", best_lb)):
            cfg = copy.deepcopy(cfg23)
            cfg["cadence"] = cad
            if cad == "monthly":
                cfg["rebalance_dom"] = cfg.get("rebalance_dom") or 1
            if wsrc is not None:
                cfg["selection"]["factor_weights"] = _pead_variant_weights(w23, PEAD_WEIGHT)
                cfg["selection"]["pead_lookback_q"] = wsrc
                prov = _make_provider(wsrc)
            else:
                prov = _fundamentals_provider
            m, _eq, _fic = run(cfg, prov, *FULL[1:])
            print(f"  [{cad:9} {tag:12}] {_fmt(m)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
