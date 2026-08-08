"""compute_stocks 병합 경로 회귀 테스트.

이 경로에는 그동안 테스트가 하나도 없었다. 그래서 로컬 저장소 도입(§49 B1)이
`_fetch_market_cap` 반환 프레임에 `market` 컬럼을 더했을 때, `cap_df` 와 `fund_df`
양쪽에 같은 컬럼이 생겨 `DataFrame.join` 이 "columns overlap but no suffix
specified" 로 죽는 회귀가 스위트 827건을 그대로 통과했다 — 유저 대면
`GET /api/metrics/stocks` 가 100% 503 이 되는 결함이었다.

외부(pykrx·로컬 스토어·개별 OHLCV)는 전부 대역한다.
"""
from datetime import date

import pandas as pd
import pytest

from app.services.metrics import stocks as stocks_mod

_AS_OF = date(2026, 8, 5)


def _cap_frame() -> pd.DataFrame:
    """§49 B1 이후의 _fetch_market_cap 반환 형태 — market 컬럼을 포함한다."""
    df = pd.DataFrame(
        {
            "시가총액": [5.0e14, 8.0e13],
            "거래량": [1_000_000, 500_000],
            "거래대금": [2.0e11, 8.0e10],
            "상장주식수": [5_969_782_550, 728_002_365],
            "market": ["KOSPI", "KOSPI"],
        },
        index=["005930", "000660"],
    )
    df.index.name = "티커"
    return df


def _fund_frame() -> pd.DataFrame:
    """_fetch_fundamentals 반환 형태 — 이쪽도 market 을 싣는다(원래부터)."""
    df = pd.DataFrame(
        {
            "PER": [12.0, 9.0],
            "PBR": [1.3, 1.1],
            "DIV": [2.1, 1.4],
            "market": ["KOSPI", "KOSPI"],
        },
        index=["005930", "000660"],
    )
    df.index.name = "티커"
    return df


def _price_change_frame() -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "시가": [70000, 180000],
            "종가": [71000, 182000],
            "등락률": [1.4, 1.1],
            "거래량": [1_000_000, 500_000],
            "거래대금": [2.0e12, 1.6e12],
            "종목명": ["삼성전자", "SK하이닉스"],
            "market": ["KOSPI", "KOSPI"],
        },
        index=["005930", "000660"],
    )
    df.index.name = "티커"
    return df


@pytest.fixture
def _stub_sources(monkeypatch):
    """compute_stocks 의 외부 의존을 전부 대역한다(망·DB 미접촉)."""
    monkeypatch.setattr(stocks_mod, "_fetch_market_cap", lambda *a, **kw: _cap_frame())
    monkeypatch.setattr(stocks_mod, "_fetch_fundamentals", lambda *a, **kw: _fund_frame())
    monkeypatch.setattr(
        stocks_mod, "_fetch_price_change", lambda *a, **kw: _price_change_frame()
    )
    # Phase 3: 개별 OHLCV 조회로 계산하는 기술지표는 이 테스트의 관심사가 아니다.
    # valid_bdays 는 252 이상이어야 뒤의 거래일수 필터를 통과한다.
    monkeypatch.setattr(
        stocks_mod,
        "_compute_tech_indicators",
        lambda code, start, end: {
            "high_52w_ratio": 0.9, "rsi14": 50.0, "vol_ann": 0.3, "mdd_252": -0.2,
            "trend_aligned": True, "above_sma200": True, "valid_bdays": 260,
        },
    )
    # 종목명 카탈로그는 외부(FDR·KRX)를 타므로 대역한다 — 테스트가 망에 닿으면 안 된다.
    monkeypatch.setattr(stocks_mod, "_build_name_map", lambda: {})
    monkeypatch.setattr(stocks_mod, "_build_krx_name_map", lambda *frames: {})


def test_시총과_펀더멘털에_market_이_모두_있어도_병합이_죽지_않는다(_stub_sources):
    """B1-R1 회귀: cap_df 와 fund_df 가 둘 다 market 을 실어도 join 이 터지면 안 된다.

    수정 전에는 `merged.join(fund_df[["PER","PBR","DIV","market"]])` 가
    ValueError 로 죽어 라우트가 503 을 돌려줬다.
    """
    out = stocks_mod.compute_stocks("KOSPI", _AS_OF)

    assert out.count == 2
    assert {i.code for i in out.items} == {"005930", "000660"}
    # 펀더멘털이 실제로 붙었는지(join 을 통째로 걷어내 회피한 것이 아닌지) 확인한다.
    per_by_code = {i.code: i.per for i in out.items}
    assert per_by_code["005930"] == pytest.approx(12.0)
    assert per_by_code["000660"] == pytest.approx(9.0)
    # 시장 태그는 cap_df 것을 그대로 쓴다.
    assert {i.market for i in out.items} == {"KOSPI"}


def test_펀더멘털이_비어도_시장태그와_결측_펀더멘털로_계속한다(monkeypatch, _stub_sources):
    """펀더멘털 조회가 빈 결과여도 시총 기반 유니버스는 살아 있어야 한다."""
    monkeypatch.setattr(stocks_mod, "_fetch_fundamentals", lambda *a, **kw: pd.DataFrame())

    out = stocks_mod.compute_stocks("KOSPI", _AS_OF)

    assert out.count == 2
    assert all(i.per is None for i in out.items)
    assert {i.market for i in out.items} == {"KOSPI"}
