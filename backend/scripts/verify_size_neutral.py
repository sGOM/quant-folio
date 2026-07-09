"""P1-3 사이즈(시가총액) 중립화 검증.

단위(중립화 후 팩터↔로그시총 무상관·NaN 보존)·스키마·실전략(id=23) A/B(none vs size)를 확인.
컨테이너:  docker compose exec -T web python scripts/verify_size_neutral.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.schemas.strategy import RebalanceConfig, RebalanceSelection
from app.services.metrics import _compute_stock_scores, _neutralize_size


def _ok(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond, name


def test_neutralize_removes_size_exposure():
    print("\n== 사이즈 중립화: 시총 노출 제거 ==")
    rng = np.random.default_rng(0)
    n = 60
    codes = [f"{i:06d}" for i in range(n)]
    cap = np.exp(rng.normal(28, 1.5, n))  # 로그정규 시총
    logcap = np.log(cap)
    logcap_z = (logcap - logcap.mean()) / logcap.std()
    # 팩터가 사이즈에 강하게 실림: score = 0.8*sizez + 0.6*noise
    score = 0.8 * logcap_z + 0.6 * rng.normal(0, 1, n)
    df = pd.DataFrame({"score_momentum": score, "market_cap": cap}, index=codes)

    before = np.corrcoef(df["score_momentum"], logcap_z)[0, 1]
    _neutralize_size(df, ["score_momentum"])
    after = np.corrcoef(df["score_momentum"], logcap_z)[0, 1]
    print(f"    corr(팩터, 로그시총): before={before:+.3f} → after={after:+.3f}")
    _ok("중립화 전 시총 상관 큼(|r|>0.5)", abs(before) > 0.5)
    _ok("중립화 후 시총 상관≈0(|r|<1e-6)", abs(after) < 1e-6)


def test_neutralize_preserves_nan_and_missing_cap():
    print("\n== NaN·시총결측 보존 ==")
    codes = [f"{i:06d}" for i in range(8)]
    df = pd.DataFrame({
        "score_value": [1.0, -0.5, np.nan, 0.3, 0.8, -1.2, 0.1, np.nan],
        "market_cap": [1e12, 2e12, 3e12, np.nan, 5e12, 6e12, 7e12, 8e12],
    }, index=codes)
    orig_nan = df["score_value"].isna()
    _neutralize_size(df, ["score_value"])
    _ok("팩터 NaN 위치 보존", df["score_value"].isna().equals(orig_nan))


def test_compute_scores_neutralize_changes_ranking():
    print("\n== _compute_stock_scores(neutralize=size) ==")
    rng = np.random.default_rng(1)
    n = 50
    codes = [f"{i:06d}" for i in range(n)]
    cap = np.exp(rng.normal(28, 1.5, n))
    logz = (np.log(cap) - np.log(cap).mean()) / np.log(cap).std()
    # 모멘텀이 대형주에 실린 데이터: mom_6m = f(size)
    df = pd.DataFrame({
        "mom_1m": 0.3 * logz + rng.normal(0, 0.02, n),
        "mom_3m": 0.3 * logz + rng.normal(0, 0.03, n),
        "mom_6m": 0.05 * logz + rng.normal(0, 0.05, n),
        "high_52w_ratio": 0.9 + 0.05 * logz + rng.normal(0, 0.02, n),
        "vol_ann": rng.uniform(0.2, 0.5, n),
        "mdd_252": -rng.uniform(0.1, 0.4, n),
        "market_cap": cap,
    }, index=codes)
    plain = _compute_stock_scores(df.copy(), neutralize="none")
    neut = _compute_stock_scores(df.copy(), neutralize="size")
    c_plain = np.corrcoef(plain["score_momentum"], logz)[0, 1]
    c_neut = np.corrcoef(neut["score_momentum"], logz)[0, 1]
    print(f"    corr(모멘텀점수, 로그시총): none={c_plain:+.3f} size={c_neut:+.4f}")
    _ok("중립화가 모멘텀↔시총 상관을 줄임", abs(c_neut) < abs(c_plain))
    # 카테고리 점수는 JSON 크기를 위해 소수 4자리로 반올림되므로 잔차 직교성이 미세하게
    # 깨진다(정확한 0 은 반올림 전 상태). 실질적으로 ≈0(강한 노출이 거의 사라짐)만 본다.
    _ok("중립화 후 |상관|≈0(<0.01)", abs(c_neut) < 0.01)


def test_schema():
    print("\n== 스키마 ==")
    sel = RebalanceSelection(method="score", top_n=5, neutralize="size")
    _ok("neutralize 파싱", sel.neutralize == "size")
    cfg = RebalanceConfig(
        universe=["005930", "000660", "035420"],
        selection={"method": "score", "top_n": 3, "neutralize": "size"},
        drift_band_pct=0.02,
    )
    _ok("RebalanceConfig 배선", cfg.selection.neutralize == "size")
    _ok("기본값 none", RebalanceSelection(method="score").neutralize == "none")


def test_ab_backtest():
    print("\n== 실전략 id=23 A/B(none vs size) ==")
    import asyncio
    from datetime import date
    from app.api.routes.backtests import (
        _REGIME_INDEX_TICKER, _build_pit_pool, _fundamentals_provider,
        _fundamentals_provider_with_market_cap, _load_regime_series, _to_dt,
    )
    from app.core.database import AsyncSessionLocal
    from app.models import Strategy
    from app.services.backtest.portfolio import run_rebalance_backtest
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

    def run(neutralize, provider):
        c = {**cfg, "selection": {**cfg["selection"], "neutralize": neutralize}}
        return run_rebalance_backtest(panel, c, _to_dt(START), _to_dt(END, end=True),
                                      provider, regime, pool_provider)

    none = run("none", _fundamentals_provider)
    size = run("size", _fundamentals_provider_with_market_cap)
    print(f"    none: total={none['total_return']:.3f} mdd={none['mdd']:.3f} sharpe={none['sharpe']:.2f}")
    print(f"    size: total={size['total_return']:.3f} mdd={size['mdd']:.3f} sharpe={size['sharpe']:.2f}")
    _ok("중립화가 백테스트 경로에서 활성(total≠none)",
        abs(size["total_return"] - none["total_return"]) > 1e-6)


if __name__ == "__main__":
    test_schema()
    test_neutralize_removes_size_exposure()
    test_neutralize_preserves_nan_and_missing_cap()
    test_compute_scores_neutralize_changes_ranking()
    test_ab_backtest()
    print("\n모든 검증 통과 ✅")
