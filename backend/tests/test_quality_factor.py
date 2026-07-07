"""퀄리티 팩터(OpenDART) 배선 검증.

- metrics._compute_stock_scores 의 quality 카테고리(ROE↑·부채비율↓·FCF흑자)가
  올바른 방향으로 랭킹에 반영되는지.
- quality 컬럼이 없으면(=OpenDART 미배선) score_quality 가 전부 0 이라 기존
  전략에 영향이 없는지(하위호환).
- opendart.metrics_by_symbol 이 PIT 공시지연 연도로 조회하고 corp_code 매핑을
  통과시키는지(네트워크 없이 monkeypatch).
"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import metrics
from app.services.data import opendart


def _base_frame():
    """모멘텀·밸류·저변동 팩터를 중립화한 4종목 프레임(quality 만 변별)."""
    idx = ["A", "B", "C", "D"]
    return pd.DataFrame(index=idx)


def test_quality_ranks_high_roe_low_debt_positive_fcf_top():
    df = _base_frame()
    # 모멘텀은 동일값(중립)으로 둬 quality 만 변별되게 한다(실 백테스트는 가격에서
    # 모멘텀이 항상 산출되므로 momentum 결측으로 인한 score NaN 은 발생하지 않는다).
    df["mom_6m"] = [0.1, 0.1, 0.1, 0.1]
    # A: 최우량(고ROE·저부채·흑자), D: 최열위(저ROE·고부채·적자)
    df["roe"] = [0.20, 0.12, 0.05, -0.03]
    df["debt_ratio"] = [0.3, 0.8, 1.5, 2.5]
    df["fcf"] = [5e11, 1e11, -2e11, -8e11]
    scored = metrics._compute_stock_scores(df, weights={
        "momentum": 0.0, "value": 0.0, "lowvol": 0.0, "quality": 1.0,
    })
    q = scored["score_quality"]
    # 퀄리티 순위: A > B > C > D
    assert q["A"] > q["B"] > q["C"] > q["D"]
    # 종합점수도 quality 만 반영하므로 동일 순위
    assert scored["score"]["A"] == scored["score"].max()


def test_quality_absent_columns_neutral():
    """roe/debt_ratio/fcf 컬럼이 없으면 score_quality 는 전부 0 → 기존 전략 무영향."""
    df = pd.DataFrame(index=["A", "B", "C"])
    df["mom_6m"] = [0.1, 0.2, 0.3]  # 모멘텀만 존재
    scored = metrics._compute_stock_scores(df)  # 기본 가중치(quality=0)
    assert (scored["score_quality"] == 0.0).all()
    # 모멘텀 방향은 정상 반영(C 최상)
    assert scored["score"]["C"] == scored["score"].max()


def test_quality_zero_weight_does_not_change_score():
    """quality 가중치 0 이면 quality 컬럼이 있어도 종합점수에 반영되지 않는다."""
    df = _base_frame()
    df["mom_6m"] = [0.30, 0.20, 0.10, 0.05]  # 모멘텀: A>B>C>D
    df["roe"] = [-0.03, 0.05, 0.12, 0.20]     # 퀄리티는 반대 방향(D 최우량)
    df["debt_ratio"] = [2.5, 1.5, 0.8, 0.3]
    df["fcf"] = [-8e11, -2e11, 1e11, 5e11]
    scored = metrics._compute_stock_scores(df, weights={
        "momentum": 1.0, "value": 0.0, "lowvol": 0.0, "quality": 0.0,
    })
    # quality 가중치 0 → 모멘텀만 반영되어 A 가 최상(퀄리티 반대여도 무관)
    assert scored["score"]["A"] == scored["score"].max()


def test_metrics_by_symbol_pit_and_mapping(monkeypatch):
    """metrics_by_symbol: 공시지연 연도로 조회하고 corp_code 매핑을 통과시킨다."""
    monkeypatch.setattr(opendart, "is_enabled", lambda: True)
    monkeypatch.setattr(opendart, "cached_corp_code_map",
                        lambda: {"005930": "00126380", "035420": "00266961"})
    calls = []

    def fake_annual(corp_code, year):
        calls.append((corp_code, year))
        return {"roe": 0.1, "debt_ratio": 0.5, "op_income": 1.0,
                "net_income": 1.0, "fcf": 1.0, "roa": 0.05}

    monkeypatch.setattr(opendart, "annual_metrics", fake_annual)

    # as_of 2025-05 → 공시지연 연도 2024 (4월 이후). 성장 파생 위해 전년(2023)도 조회.
    out = opendart.metrics_by_symbol(["005930", "035420", "999999"], date(2025, 5, 20))
    assert set(out) == {"005930", "035420"}  # 매핑 없는 999999 제외
    years = {y for _, y in calls}
    assert max(years) == 2024  # PIT 최신 사용연도
    assert 2023 in years        # 전년(YoY 성장용)
    assert out["005930"]["roe"] == 0.1


def test_metrics_by_symbol_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(opendart, "is_enabled", lambda: False)
    assert opendart.metrics_by_symbol(["005930"], date(2025, 5, 20)) == {}


def test_growth_ranks_high_yoy_and_turnaround_top():
    """성장 팩터: 영업이익·순이익 YoY 성장률↑, 흑자전환=1 일수록 상위."""
    df = _base_frame()
    df["mom_6m"] = [0.1, 0.1, 0.1, 0.1]  # 모멘텀 중립
    df["op_growth"] = [0.50, 0.20, 0.00, -0.30]
    df["net_growth"] = [0.60, 0.10, -0.10, -0.40]
    df["turnaround"] = [1.0, 0.0, 0.0, 0.0]
    scored = metrics._compute_stock_scores(df, weights={
        "momentum": 0.0, "value": 0.0, "lowvol": 0.0, "quality": 0.0, "growth": 1.0,
    })
    g = scored["score_growth"]
    assert g["A"] > g["B"] > g["C"] > g["D"]
    assert scored["score"]["A"] == scored["score"].max()


def test_growth_absent_columns_neutral():
    df = pd.DataFrame(index=["A", "B", "C"])
    df["mom_6m"] = [0.1, 0.2, 0.3]
    scored = metrics._compute_stock_scores(df)  # 기본(growth=0)
    assert (scored["score_growth"] == 0.0).all()


def test_metrics_by_symbol_adds_growth_and_turnaround(monkeypatch):
    """metrics_by_symbol 이 당해·전년 재무로 YoY 성장·흑자전환을 파생한다."""
    monkeypatch.setattr(opendart, "is_enabled", lambda: True)
    monkeypatch.setattr(opendart, "cached_corp_code_map", lambda: {"000000": "00000000"})

    def fake_annual(corp, year):
        # 당해(year=2024): 흑자·성장 / 전년(2023): 적자
        if year == 2024:
            return {"op_income": 120.0, "net_income": 100.0, "roe": 0.1,
                    "debt_ratio": 0.5, "fcf": 1.0, "roa": 0.05}
        return {"op_income": 100.0, "net_income": -50.0, "roe": None,
                "debt_ratio": 0.6, "fcf": -1.0, "roa": None}

    monkeypatch.setattr(opendart, "annual_metrics", fake_annual)
    out = opendart.metrics_by_symbol(["000000"], date(2025, 6, 1))["000000"]
    assert out["op_growth"] == pytest.approx(0.2)  # 120/100 - 1
    assert out["net_growth"] is None  # 전년 순이익 ≤0 → 성장률 정의불가
    assert out["turnaround"] == 1.0   # 적자(-50) → 흑자(100)


def test_yoy_growth_and_turnaround_helpers():
    assert opendart._yoy_growth(120, 100) == pytest.approx(0.2)
    assert opendart._yoy_growth(120, 0) is None      # 전년 0 → None
    assert opendart._yoy_growth(120, -10) is None    # 전년 적자 → None
    assert opendart._yoy_growth(None, 100) is None
    assert opendart._turnaround_flag(10, -5) == 1.0  # 흑자전환
    assert opendart._turnaround_flag(10, 5) == 0.0   # 계속 흑자
    assert opendart._turnaround_flag(-1, -5) == 0.0  # 계속 적자
    assert opendart._turnaround_flag(10, None) is None


def test_annual_metrics_caches_and_falls_back_to_ofs(monkeypatch):
    """연결(CFS) 무자료면 개별(OFS)로 폴백하고, 결과를 (corp,year)로 캐시한다."""
    opendart._METRICS_CACHE.clear()
    opendart._ACCOUNTS_CACHE.clear()
    fetched = []

    def fake_accounts(corp, year, reprt, fs):
        fetched.append(fs)
        if fs == opendart.FS_CONSOLIDATED:
            return None  # 연결 무자료
        return [{"sj_div": "BS", "account_id": "ifrs-full_Equity",
                 "account_nm": "자본총계", "thstrm_amount": "1000"},
                {"sj_div": "BS", "account_id": "ifrs-full_Liabilities",
                 "account_nm": "부채총계", "thstrm_amount": "500"}]

    monkeypatch.setattr(opendart, "single_company_accounts", fake_accounts)
    m1 = opendart.annual_metrics("00999999", 2024)
    assert m1["debt_ratio"] == 0.5
    assert opendart.FS_SEPARATE in fetched  # OFS 폴백 발생
    # 두 번째 호출은 캐시 → single_company_accounts 재호출 없음
    fetched.clear()
    m2 = opendart.annual_metrics("00999999", 2024)
    assert m2 == m1
    assert fetched == []
