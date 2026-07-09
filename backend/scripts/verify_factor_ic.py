"""P1-1 성과귀속·팩터 IC/IR 검증.

합성 검증(팩터가 수익을 예측하도록 구성 → IC>0·IR 유의·롱숏수익>0)과 실전략(id=23)
백테스트에서 factor_ic 노출·해석을 확인한다.
컨테이너:  docker compose exec -T web python scripts/verify_factor_ic.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.backtest.portfolio import _factor_attribution, run_rebalance_backtest


def _ok(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond, name


def test_predictive_factor_positive_ic():
    """팩터 점수가 forward return 을 양(+)으로 예측하면 IC>0·IR>0·롱숏수익>0."""
    print("\n== 합성: 예측력 있는 팩터 ==")
    rng = np.random.default_rng(0)
    n_syms, n_rebal = 30, 12
    syms = [f"S{i:02d}" for i in range(n_syms)]
    # 각 종목에 '진짜 알파' 부여(고정). 팩터점수 = 알파 + 잡음, forward = 알파 + 잡음.
    alpha = rng.normal(0, 1, n_syms)
    snapshots = []
    # 21거래일 간격의 리밸런싱 날짜에 대응하는 패널 구성
    dates = pd.bdate_range("2022-01-03", periods=n_rebal * 21 + 5)
    prices = {s: [100.0] for s in syms}
    rebal_days = [i * 21 for i in range(n_rebal)]
    # 가격 경로: 각 구간 수익률이 alpha 에 비례(+잡음) → 팩터가 예측
    for di in range(1, len(dates)):
        for j, s in enumerate(syms):
            seg = next((k for k in rebal_days if k < di), 0)
            drift = alpha[j] * 0.002 + rng.normal(0, 0.01)
            prices[s].append(prices[s][-1] * (1 + drift))
    panel = pd.DataFrame(prices, index=dates)
    for k in rebal_days:
        d = dates[k]
        score = pd.Series(alpha + rng.normal(0, 0.3, n_syms), index=syms)
        frame = pd.DataFrame({"score_momentum": score, "score": score})
        snapshots.append((d, frame))
    attr = _factor_attribution(snapshots, panel)
    print(f"    score_momentum: {attr.get('score_momentum')}")
    _ok("score_momentum IC>0", attr["score_momentum"]["ic_mean"] > 0)
    _ok("score_momentum IR>0", attr["score_momentum"]["ic_ir"] > 0)
    _ok("score_momentum 롱숏수익>0", attr["score_momentum"]["ls_return"] > 0)
    _ok("적중률>0.5", attr["score_momentum"]["ic_hit"] > 0.5)


def test_noise_factor_near_zero_ic():
    """무작위 팩터는 IC≈0(예측력 없음)."""
    print("\n== 합성: 무작위(무예측) 팩터 ==")
    rng = np.random.default_rng(1)
    n_syms, n_rebal = 30, 12
    syms = [f"S{i:02d}" for i in range(n_syms)]
    dates = pd.bdate_range("2022-01-03", periods=n_rebal * 21 + 5)
    prices = {s: 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(dates)))) for s in syms}
    panel = pd.DataFrame(prices, index=dates)
    snapshots = []
    for k in [i * 21 for i in range(n_rebal)]:
        score = pd.Series(rng.normal(0, 1, n_syms), index=syms)  # 가격과 무관
        snapshots.append((dates[k], pd.DataFrame({"score": score})))
    attr = _factor_attribution(snapshots, panel)
    print(f"    score: ic_mean={attr['score']['ic_mean']:.3f} ir={attr['score']['ic_ir']:.2f}")
    _ok("무작위 팩터 |IC|<0.2", abs(attr["score"]["ic_mean"]) < 0.2)


def test_real_strategy_id23():
    print("\n== 실전략 id=23 factor_ic ==")
    import asyncio
    from datetime import date
    from app.api.routes.backtests import (
        _REGIME_INDEX_TICKER, _build_pit_pool, _fundamentals_provider,
        _load_regime_series, _to_dt,
    )
    from app.core.database import AsyncSessionLocal
    from app.models import Strategy
    from app.services.data.loader import get_close_series
    from sqlalchemy import select

    WARMUP, START, END = date(2019, 8, 1), date(2021, 1, 1), date(2025, 6, 30)

    async def _setup():
        async with AsyncSessionLocal() as db:
            cfg = dict((await db.scalar(select(Strategy).where(Strategy.id == 23))).config)
        pit_union, pool_provider = _build_pit_pool(cfg, WARMUP, END)
        universe = pit_union if pit_union is not None else list(cfg.get("universe", []))
        cols = {}
        async with AsyncSessionLocal() as db:
            for sym in universe:
                s = await get_close_series(db, sym, _to_dt(WARMUP), _to_dt(END, end=True))
                if not s.empty:
                    cols[sym] = s
        panel = pd.DataFrame(cols)
        rf = cfg.get("regime_filter") or {}
        regime = await asyncio.to_thread(
            _load_regime_series, WARMUP, END, _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001"))
        return cfg, panel, pool_provider, regime

    cfg, panel, pool_provider, regime = asyncio.run(_setup())
    res = run_rebalance_backtest(panel, cfg, _to_dt(START), _to_dt(END, end=True),
                                 _fundamentals_provider, regime, pool_provider)
    fic = res.get("factor_ic") or {}
    print(f"    factor_ic 팩터 수: {len(fic)}")
    for k, v in fic.items():
        print(f"      {k:16} IC={v['ic_mean']:+.3f} IR={v['ic_ir']:+.2f} hit={v['ic_hit']:.2f} "
              f"LS={v['ls_return']:+.3f} n={v['n']}")
    _ok("factor_ic 노출(score 전략)", len(fic) > 0)
    _ok("종합(score) 항목 존재", "score" in fic)
    _ok("개별 팩터 IC/IR 필드 완비", all(
        {"ic_mean", "ic_ir", "ic_hit", "ls_return", "n"} <= set(v) for v in fic.values()))


if __name__ == "__main__":
    test_predictive_factor_positive_ic()
    test_noise_factor_near_zero_ic()
    test_real_strategy_id23()
    print("\n모든 검증 통과 ✅")
