"""P1-2 리스크 레이어 검증 — 집중 한도·변동성 타겟팅·MDD 킬스위치.

단위 검증(캡/변동성 헬퍼)과 실전략(id=23) 백테스트 A/B(리스크 레이어 on/off)를 수행한다.
컨테이너에서:  docker compose exec -T web python scripts/verify_risk_layer.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.schemas.strategy import RiskLayer, RebalanceConfig
from app.services.backtest.portfolio import (
    _apply_risk_caps,
    _cap_position_weights,
    _portfolio_vol_ann,
)


def _ok(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond, name


def test_position_cap():
    print("\n== 종목 집중 한도 ==")
    # 한 종목이 상한 초과 → 초과분이 나머지에 비례 재분배
    w = {"A": 0.6, "B": 0.2, "C": 0.2}
    capped = _cap_position_weights(w, 0.4)
    _ok("A 가 상한 0.4 로 제한", abs(capped["A"] - 0.4) <= 1e-9)
    _ok("합 보존(1.0)", abs(sum(capped.values()) - 1.0) <= 1e-9)
    _ok("초과분 0.2 가 B/C 에 균등 재분배", abs(capped["B"] - 0.3) <= 1e-9 and abs(capped["C"] - 0.3) <= 1e-9)

    # 재분배 여력 없음(모두 상한 근처) → 남는 비중은 현금(합<1)
    w2 = {"A": 0.5, "B": 0.5}
    capped2 = _cap_position_weights(w2, 0.3)
    _ok("모두 상한이면 합<1(현금 발생)", sum(capped2.values()) < 1.0 - 1e-9)
    _ok("각 종목 상한 준수", all(v <= 0.3 + 1e-9 for v in capped2.values()))

    # 연쇄: 재분배로 다시 상한 초과 → 반복 수렴
    w3 = {"A": 0.7, "B": 0.28, "C": 0.02}
    capped3 = _cap_position_weights(w3, 0.35)
    _ok("연쇄 재분배 후 전원 상한 준수", all(v <= 0.35 + 1e-9 for v in capped3.values()))


def test_vol_target():
    print("\n== 변동성 타겟팅 ==")
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2023-01-01", periods=120)
    # 저변동(σ_d≈0.5%)·고변동(σ_d≈4%) 두 종목
    lowv = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, len(idx))))
    highv = 100 * np.exp(np.cumsum(rng.normal(0, 0.04, len(idx))))
    hist = pd.DataFrame({"LOW": lowv, "HIGH": highv}, index=idx)

    vol_low = _portfolio_vol_ann(hist, {"LOW": 1.0}, 60)
    vol_high = _portfolio_vol_ann(hist, {"HIGH": 1.0}, 60)
    print(f"    σ(LOW)≈{vol_low:.3f}, σ(HIGH)≈{vol_high:.3f}")
    _ok("고변동 종목 σ > 저변동 종목 σ", vol_high > vol_low)

    # target_vol=10%. 고변동(≈63%) 포트는 디레버리징(합<1), 저변동(≈8%)은 그대로(합=1)
    risk = {"target_vol": 0.10, "vol_lookback": 60, "max_leverage": 1.0}
    t_high = _apply_risk_caps({"HIGH": 1.0}, hist, risk)
    t_low = _apply_risk_caps({"LOW": 1.0}, hist, risk)
    print(f"    고변동 투자비중 Σ={sum(t_high.values()):.3f}, 저변동 Σ={sum(t_low.values()):.3f}")
    _ok("고변동 포트 디레버리징(Σ<1)", sum(t_high.values()) < 1.0 - 1e-6)
    _ok("저변동 포트 유지(Σ≈1, 확대 없음)", abs(sum(t_low.values()) - 1.0) <= 1e-6)


def test_schema():
    print("\n== 스키마 검증 ==")
    rl = RiskLayer(max_position_pct=0.1, target_vol=0.15, mdd_kill_pct=0.25)
    _ok("RiskLayer 파싱", rl.max_position_pct == 0.1 and rl.mdd_rearm_days == 20)
    cfg = RebalanceConfig(
        universe=["005930", "000660", "035420"],
        selection={"method": "momentum", "top_n": 3, "lookback": 60},
        drift_band_pct=0.02,
        risk_layer={"max_position_pct": 0.4, "mdd_kill_pct": 0.2},
    )
    _ok("RebalanceConfig.risk_layer 배선", cfg.risk_layer.mdd_kill_pct == 0.2)

    # 집중 한도 ≤ drift_band 이면 진입 불가 → 검증 에러여야 한다.
    import pytest  # noqa: F401 형태만; 실제로는 try/except 로 확인
    raised = False
    try:
        RebalanceConfig(
            universe=["005930", "000660", "035420"],
            selection={"method": "momentum", "top_n": 3, "lookback": 60},
            drift_band_pct=0.05,
            risk_layer={"max_position_pct": 0.05},  # == drift_band → 금지
        )
    except Exception:
        raised = True
    _ok("max_position_pct ≤ drift_band 는 검증 에러", raised)


def test_ab_backtest():
    """실전략(id=23) 백테스트: 리스크 레이어 on/off A/B — MDD 완화·집중 한도·num_kills."""
    print("\n== 실전략 A/B 백테스트(id=23) ==")
    import asyncio
    from datetime import date

    from app.api.routes.backtests import (
        _REGIME_INDEX_TICKER,
        _build_pit_pool,
        _fundamentals_provider,
        _load_regime_series,
        _to_dt,
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
            _load_regime_series, WARMUP, END, _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001")
        )
        bench = await asyncio.to_thread(_load_regime_series, WARMUP, END, "1028")  # KOSPI200
        return cfg, panel, pool_provider, regime, bench

    cfg, panel, pool_provider, regime, bench = asyncio.run(_setup())
    print(f"    패널 {panel.shape}")

    def _run(risk):
        return run_rebalance_backtest(
            panel, {**cfg, "risk_layer": risk}, _to_dt(START), _to_dt(END, end=True),
            _fundamentals_provider, regime, pool_provider, bench,
        )

    def _hmax(r):
        return max(r["holdings"].values()) if r["holdings"] else 0.0

    off = _run(None)
    # (1) 킬 임계가 자연 MDD 보다 깊으면(60%) 오버레이는 완전 무영향 — OFF 와 동일해야 한다.
    inert = _run({"mdd_kill_pct": 0.60})
    # (2) 타이트 임계(15%) → 발동(num_kills≥1).
    tight = _run({"mdd_kill_pct": 0.15})
    # (3) 집중 한도(7%>drift_band 5%) → 최대 보유비중이 OFF(≈8.2%) 대비 낮아져야 한다.
    capped = _run({"max_position_pct": 0.07})
    print(f"    OFF   : total={off['total_return']:.3f} mdd={off['mdd']:.3f} kills={off['num_kills']} hmax={_hmax(off):.3f}")
    print(f"    inert : total={inert['total_return']:.3f} mdd={inert['mdd']:.3f} kills={inert['num_kills']}")
    print(f"    tight : total={tight['total_return']:.3f} mdd={tight['mdd']:.3f} kills={tight['num_kills']}")
    print(f"    capped: total={capped['total_return']:.3f} hmax={_hmax(capped):.3f}")
    _ok("num_kills 키 노출", "num_kills" in off)
    _ok("깊은 임계(60%)면 미발동", inert["num_kills"] == 0)
    _ok("깊은 임계면 오버레이 무영향(total≈OFF)", abs(inert["total_return"] - off["total_return"]) <= 1e-9)
    _ok("깊은 임계면 오버레이 무영향(mdd≈OFF)", abs(inert["mdd"] - off["mdd"]) <= 1e-9)
    _ok("타이트 임계(15%)면 킬스위치 발동", tight["num_kills"] >= 1)
    # 집중 한도는 '리밸런싱 시점 목표비중'에 적용(단위 테스트가 정확성 검증). 기말 holdings 는
    # 마지막 리밸런싱 이후 드리프트를 포함하므로, 여기선 '캡이 백테스트 결과를 실제로 바꿈'만 본다.
    _ok("집중 한도가 백테스트 경로에서 활성(total≠OFF)", abs(capped["total_return"] - off["total_return"]) > 1e-6)


if __name__ == "__main__":
    test_schema()
    test_position_cap()
    test_vol_target()
    test_ab_backtest()
    print("\n모든 검증 통과 ✅")
