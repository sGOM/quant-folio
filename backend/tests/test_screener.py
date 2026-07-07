"""소형주 턴어라운드 스크리너 검증(네트워크 없이 monkeypatch).

전 시장 스캔 → 시총 하위 필터 → 거래대금 급증 → OpenDART 하드 필터(부채·만성적자)
→ 종합점수 정렬의 파이프라인을 고정 fixture 로 확인한다.
"""
from datetime import date

import pandas as pd

from app.services import screener


# 6자리 코드(스크리너가 index 를 zfill(6) 하므로 fixture 도 6자리로).
S1, S2, M1, L1, L2 = "000111", "000222", "003330", "004440", "005550"


def _cap_df():
    # 5종목: 시총 하위 2종(S1,S2)만 소형주(하위 20~40%). 대형주는 필터 아웃.
    return pd.DataFrame(
        {"시가총액": [50e8, 80e8, 500e8, 1000e8, 3000e8],
         "거래대금": [0, 0, 0, 0, 0]},
        index=[S1, S2, M1, L1, L2],
    )


def _pc(days_value: dict[str, float]):
    return pd.DataFrame({"거래대금": days_value, "종목명": {k: k for k in days_value}})


def _patch_common(monkeypatch, qmetrics, enabled=True):
    monkeypatch.setattr(screener, "_fetch_market_cap", lambda *a, **k: _cap_df())
    def fake_pc(start, end, mkts):
        # 거래대금 총액 60억: avg5=12억, avg60=1억 → surge=12, avg_value_20=1억.
        return _pc({S1: 60e8, S2: 60e8, M1: 60e8, L1: 60e8, L2: 60e8})
    monkeypatch.setattr(screener, "_fetch_price_change", fake_pc)
    monkeypatch.setattr(screener, "_fetch_fundamentals",
                        lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(screener.opendart, "is_enabled", lambda: enabled)
    monkeypatch.setattr(screener.opendart, "metrics_by_symbol",
                        lambda codes, as_of: qmetrics)


def test_smallcap_and_surge_filter(monkeypatch):
    """소형주(S1,S2)만 후보가 되고, 급증·유동성 통과분만 스캔 대상."""
    # 5일 총 60억/5=12억, 60일 총 60억/60=1억 → surge=12, avg_value_20=1억.
    _patch_common(monkeypatch, qmetrics={}, enabled=False)
    out = screener.screen_turnaround(
        date(2025, 6, 2), "ALL", top_n=10, min_value=50_000_000, surge=1.5, smallcap_pct=0.4,
    )
    codes = {c.code for c in out.items}
    assert codes <= {S1, S2}  # 대형주 제외
    assert out.opendart_enabled is False


def test_debt_and_chronic_loss_hard_filter(monkeypatch):
    """OpenDART 활성 시 부채>150%·3년 만성적자는 제외된다."""
    qmetrics = {
        S1: {"debt_ratio": 0.8, "loss_years_3": 1, "net_income": 100.0,
             "turnaround": 1.0, "net_growth": 0.5, "f_score": 7},
        S2: {"debt_ratio": 2.0, "loss_years_3": 0, "net_income": 50.0,
             "turnaround": 0.0, "net_growth": 0.1, "f_score": 5},  # 부채 200% → 제외
    }
    _patch_common(monkeypatch, qmetrics, enabled=True)
    out = screener.screen_turnaround(
        date(2025, 6, 2), "ALL", top_n=10, min_value=50_000_000,
        surge=1.5, max_debt=1.5, smallcap_pct=0.4,
    )
    codes = {c.code for c in out.items}
    assert S1 in codes            # 저부채·흑자전환 통과
    assert S2 not in codes        # 부채 200% > 150% 제외
    s1 = next(c for c in out.items if c.code == S1)
    assert s1.turnaround == 1.0
    assert s1.f_score == 7
    assert s1.score > 0


def test_chronic_loss_excluded(monkeypatch):
    """최근 3년 모두 적자(loss_years_3=3)면 제외."""
    qmetrics = {
        S1: {"debt_ratio": 0.5, "loss_years_3": 3, "net_income": -10.0,
             "turnaround": 0.0, "net_growth": None, "f_score": 2},
        S2: {"debt_ratio": 0.5, "loss_years_3": 1, "net_income": 20.0,
             "turnaround": 1.0, "net_growth": 0.3, "f_score": 6},
    }
    _patch_common(monkeypatch, qmetrics, enabled=True)
    out = screener.screen_turnaround(date(2025, 6, 2), "ALL", min_value=50_000_000, smallcap_pct=0.4)
    codes = {c.code for c in out.items}
    assert S1 not in codes        # 만성 적자 제외
    assert S2 in codes


def test_empty_market_returns_empty(monkeypatch):
    monkeypatch.setattr(screener, "_fetch_market_cap", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(screener.opendart, "is_enabled", lambda: True)
    out = screener.screen_turnaround(date(2025, 6, 2), "ALL")
    assert out.count == 0 and out.items == []
