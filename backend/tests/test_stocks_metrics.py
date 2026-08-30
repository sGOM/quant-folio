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
    """`_fetch_price_change` 반환 형태.

    247540(KOSDAQ)까지 담아둔다 — 시장 라벨 테스트가 cap 프레임에 KOSDAQ 종목을
    더할 때 여기에도 있어야 유동성 필터(avg_value_20)를 통과한다. cap 인덱스 기준
    정렬이라 이 종목을 안 쓰는 테스트에는 영향이 없다.
    """
    df = pd.DataFrame(
        {
            "시가": [70000, 180000, 45000],
            "종가": [71000, 182000, 46000],
            "등락률": [1.4, 1.1, 2.2],
            "거래량": [1_000_000, 500_000, 200_000],
            "거래대금": [2.0e12, 1.6e12, 9.0e11],
            "종목명": ["삼성전자", "SK하이닉스", "에코프로비엠"],
            "market": ["KOSPI", "KOSPI", "KOSDAQ"],
        },
        index=["005930", "000660", "247540"],
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


def test_시장_라벨은_요청_인자가_아니라_시총_프레임의_태그를_쓴다(monkeypatch, _stub_sources):
    """market="ALL" + 혼합 시장으로 요청해야 라벨의 출처가 갈린다.

    요청 인자가 "KOSPI" 인 케이스로 단언하면 폴백 경로로도 그대로 통과해 아무것도
    변별하지 못한다. 예전 판은 `_is_nan(row.get("market"))` 으로 결측을 걸렀는데
    `_is_nan` 은 문자열이면 무조건 True 라(`safe_float("KOSPI") is None`) 조건이 항상
    else 로 떨어져, ALL 요청 시 전 종목 라벨이 "ALL" 로 나왔다.
    """
    cap = _cap_frame()
    cap.loc["247540", :] = [3.0e12, 200_000, 1.0e10, 100_000_000, "KOSDAQ"]
    monkeypatch.setattr(stocks_mod, "_fetch_market_cap", lambda *a, **kw: cap)

    out = stocks_mod.compute_stocks("ALL", _AS_OF)

    market_by_code = {i.code: i.market for i in out.items}
    assert market_by_code["005930"] == "KOSPI"
    assert market_by_code["247540"] == "KOSDAQ"
    assert "ALL" not in set(market_by_code.values())


def test_펀더멘털이_비어도_시장태그와_결측_펀더멘털로_계속한다(monkeypatch, _stub_sources):
    """펀더멘털 조회가 빈 결과여도 시총 기반 유니버스는 살아 있어야 한다."""
    monkeypatch.setattr(stocks_mod, "_fetch_fundamentals", lambda *a, **kw: pd.DataFrame())

    out = stocks_mod.compute_stocks("KOSPI", _AS_OF)

    assert out.count == 2
    assert all(i.per is None for i in out.items)
    assert {i.market for i in out.items} == {"KOSPI"}


def test_종가_결측이면_price_는_0_이_아니라_None_이다(monkeypatch, _stub_sources):
    """§50·§62 와 같은 계약 — 결측 종가를 0 으로 채우면 실제 0원과 구분이 안 된다.

    거래대금은 살려둬 유동성 필터를 통과시키고 종가만 결측으로 만든다(0 sentinel 이
    실제로 도달하던 경로).
    """
    pc = _price_change_frame()
    pc.loc["000660", "종가"] = float("nan")
    monkeypatch.setattr(stocks_mod, "_fetch_price_change", lambda *a, **kw: pc)

    out = stocks_mod.compute_stocks("KOSPI", _AS_OF)

    by_code = {i.code: i for i in out.items}
    assert by_code["000660"].price is None
    assert by_code["005930"].price == 71000


def test_거래대금_컬럼이_없으면_avg_value_20_은_0_이_아니라_None_이다(monkeypatch, _stub_sources):
    """부분 소스 장애로 거래대금이 통째로 빠지면 유동성 필터도 함께 비활성화된다.

    이때 0.0 을 채우면 "거래대금 0원"과 "모름"이 같은 값이 된다.
    """
    pc = _price_change_frame().drop(columns=["거래대금"])
    monkeypatch.setattr(stocks_mod, "_fetch_price_change", lambda *a, **kw: pc)

    out = stocks_mod.compute_stocks("KOSPI", _AS_OF)

    assert out.count > 0
    assert all(i.avg_value_20 is None for i in out.items)
