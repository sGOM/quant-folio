"""OpenDART 클라이언트 — 파생지표 계산(derive_metrics) 및 graceful degradation 검증.

네트워크 없이 도는 단위테스트다. 실제 OpenDART 응답(fnlttSinglAcntAll)의 계정
구조를 축약한 fixture 로 account_id 우선 매칭·CIS 폴백·부호·경계값을 확인한다.
실측 교차검증(삼성전자·현대차·NAVER 실제 수치 일치)은 구현 시 수동으로 완료했다.
"""
from datetime import date

from app.services.data import opendart


def _row(sj, aid, nm, amt):
    """OpenDART fnlttSinglAcntAll 한 행(축약)."""
    return {"sj_div": sj, "account_id": aid, "account_nm": nm, "thstrm_amount": amt}


# 삼성전자 2023 연결(억원 단위 아님, 원 단위 실제값 축약)을 모사한 fixture.
_SAMSUNG_2023 = [
    _row("BS", "ifrs-full_Assets", "자산총계", "455,905,980,000,000"),
    _row("BS", "ifrs-full_Liabilities", "부채총계", "92,228,115,000,000"),
    _row("BS", "ifrs-full_Equity", "자본총계", "363,677,865,000,000"),
    _row("IS", "dart_OperatingIncomeLoss", "영업이익", "6,566,976,000,000"),
    _row("IS", "ifrs-full_ProfitLoss", "당기순이익(손실)", "15,487,100,000,000"),
    # 귀속분은 id 가 달라 순이익으로 오인되면 안 된다.
    _row("IS", "ifrs-full_ProfitLossAttributableToOwnersOfParent",
         "지배기업의 소유주에게 귀속되는 당기순이익(손실)", "14,473,401,000,000"),
    _row("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities",
         "영업활동현금흐름", "44,137,427,000,000"),
    _row("CF", "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
         "유형자산의 취득", "57,611,292,000,000"),
    _row("CF", "ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
         "무형자산의 취득", "2,922,875,000,000"),
]


def test_derive_metrics_core_values():
    m = opendart.derive_metrics(_SAMSUNG_2023)
    assert m["net_income"] == 15_487_100_000_000  # 귀속분(14.47조) 아닌 전체 순이익
    assert m["op_income"] == 6_566_976_000_000
    # ROE = 순이익 / 자본총계 ≈ 4.26%
    assert abs(m["roe"] - 15_487_100_000_000 / 363_677_865_000_000) < 1e-9
    # 부채비율(배수) = 부채/자본 ≈ 0.2536
    assert abs(m["debt_ratio"] - 92_228_115_000_000 / 363_677_865_000_000) < 1e-9
    # ROA = 순이익 / 자산총계
    assert abs(m["roa"] - 15_487_100_000_000 / 455_905_980_000_000) < 1e-9
    # FCF = 영업CF − (유형+무형 취득) = 음수(대규모 투자연도)
    assert m["fcf"] == 44_137_427_000_000 - (57_611_292_000_000 + 2_922_875_000_000)
    assert m["fcf"] < 0


def test_derive_metrics_cis_fallback():
    """NAVER 처럼 단일 포괄손익계산서(CIS)만 작성한 회사의 영업이익·순이익."""
    rows = [
        _row("BS", "ifrs-full_Equity", "자본총계", "24,000,000,000,000"),
        _row("BS", "ifrs-full_Liabilities", "부채총계", "11,400,000,000,000"),
        _row("CIS", "dart_OperatingIncomeLoss", "영업이익", "1,488,820,269,608"),
        _row("CIS", "ifrs-full_ProfitLoss", "당기순이익", "985,017,762,493"),
    ]
    m = opendart.derive_metrics(rows)
    assert m["op_income"] == 1_488_820_269_608  # IS 없이 CIS 에서 회수
    assert m["net_income"] == 985_017_762_493
    assert m["roe"] is not None


def test_derive_metrics_name_fallback_when_id_missing():
    """account_id 가 빈 구형 공시는 계정명으로 폴백 매칭한다."""
    rows = [
        _row("BS", "", "자본총계", "1,000"),
        _row("BS", "", "부채총계", "500"),
        _row("BS", "", "자산총계", "1,500"),
        _row("IS", "", "영업이익", "200"),
        _row("IS", "", "당기순이익(손실)", "150"),
    ]
    m = opendart.derive_metrics(rows)
    assert m["op_income"] == 200
    assert m["net_income"] == 150
    assert m["debt_ratio"] == 0.5
    assert m["roe"] == 0.15


def test_derive_metrics_negative_equity_returns_none():
    """자본잠식(자본총계 ≤ 0)이면 ROE/부채비율은 정의 불가 → None."""
    rows = [
        _row("BS", "ifrs-full_Equity", "자본총계", "-1,000"),
        _row("BS", "ifrs-full_Liabilities", "부채총계", "5,000"),
        _row("IS", "ifrs-full_ProfitLoss", "당기순이익", "100"),
    ]
    m = opendart.derive_metrics(rows)
    assert m["roe"] is None
    assert m["debt_ratio"] is None


def test_derive_metrics_missing_capex_leaves_fcf_none():
    """CAPEX 계정이 아예 없으면 FCF 계산 불가 → None(영업CF 만으론 미산출)."""
    rows = [
        _row("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities",
             "영업활동현금흐름", "1,000"),
    ]
    m = opendart.derive_metrics(rows)
    assert m["fcf"] is None


def test_derive_metrics_partial_capex_sums_available():
    """유형자산 취득만 있고 무형은 없으면, 있는 것만 합산해 FCF 산출."""
    rows = [
        _row("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities",
             "영업활동현금흐름", "1,000"),
        _row("CF", "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
             "유형자산의 취득", "300"),
    ]
    m = opendart.derive_metrics(rows)
    assert m["fcf"] == 700


def test_derive_metrics_empty_returns_all_none():
    m = opendart.derive_metrics([])
    assert all(v is None for v in m.values())
    # 파생지표 + F-Score 원자료 키가 모두 존재해야 한다.
    assert {"roe", "debt_ratio", "op_income", "net_income", "fcf", "roa"} <= set(m)
    assert {"cfo", "assets", "liabilities", "current_assets",
            "current_liabilities", "revenue", "gross_profit"} <= set(m)


def test_to_number_parsing():
    assert opendart._to_number("1,234,567") == 1234567.0
    assert opendart._to_number("-500") == -500.0
    assert opendart._to_number("") is None
    assert opendart._to_number("-") is None
    assert opendart._to_number(None) is None


def test_disabled_without_key(monkeypatch):
    """키가 없으면 모든 조회가 비활성(None) — 기존 동작 무영향 보장."""
    monkeypatch.setattr(opendart.settings, "OPENDART_API_KEY", "", raising=False)
    assert opendart.is_enabled() is False
    assert opendart.corp_code_map() is None
    assert opendart.single_company_accounts("00126380", 2023) is None


def test_announcement_lagged_year():
    """사업보고서 공시지연(이듬해 3월 말) 반영 — 4월 경계."""
    assert opendart.announcement_lagged_year(date(2025, 2, 10)) == 2023
    assert opendart.announcement_lagged_year(date(2025, 3, 31)) == 2023
    assert opendart.announcement_lagged_year(date(2025, 4, 1)) == 2024
    assert opendart.announcement_lagged_year(date(2025, 12, 31)) == 2024


def test_latest_report_period_quarterly_pit():
    """분기 PIT 세분: 각 보고서 공시 마감 경계."""
    Q1, HALF, Q3, ANN = (
        opendart.REPORT_Q1, opendart.REPORT_HALF, opendart.REPORT_Q3, opendart.REPORT_ANNUAL,
    )
    assert opendart.latest_report_period(date(2025, 3, 10)) == (2023, ANN)   # 연초
    assert opendart.latest_report_period(date(2025, 4, 10)) == (2024, ANN)   # 사업보고서
    assert opendart.latest_report_period(date(2025, 5, 20)) == (2025, Q1)    # 1Q
    assert opendart.latest_report_period(date(2025, 9, 1)) == (2025, HALF)   # 반기
    assert opendart.latest_report_period(date(2025, 11, 20)) == (2025, Q3)   # 3Q
    assert opendart.latest_report_period(date(2025, 12, 31)) == (2025, Q3)


def test_count_losses():
    assert opendart._count_losses([10.0, -5.0, -1.0]) == 2
    assert opendart._count_losses([-1.0, -2.0, -3.0]) == 3   # 만성 적자
    assert opendart._count_losses([10.0, 20.0, 30.0]) == 0
    assert opendart._count_losses([None, None]) is None
    assert opendart._count_losses([None, -5.0]) == 1


def _fin(**kw):
    """F-Score 계산용 최소 재무 dict(누락 키는 None)."""
    base = {"roa": None, "cfo": None, "assets": None, "liabilities": None,
            "current_assets": None, "current_liabilities": None,
            "revenue": None, "gross_profit": None}
    base.update(kw)
    return base


def test_piotroski_f_score_perfect_improvement():
    """모든 지표가 개선된 우량 케이스 → 8점."""
    prev = _fin(roa=0.05, cfo=50, assets=1000, liabilities=600,
                current_assets=300, current_liabilities=200,
                revenue=800, gross_profit=200)
    cur = _fin(roa=0.10, cfo=150, assets=1000, liabilities=500,
               current_assets=400, current_liabilities=200,
               revenue=1000, gross_profit=300)
    assert opendart.piotroski_f_score(cur, prev) == 8


def test_piotroski_f_score_weak():
    """수익성·개선 모두 나쁜 케이스 → 낮은 점수."""
    prev = _fin(roa=0.10, cfo=100, assets=1000, liabilities=400,
                current_assets=400, current_liabilities=200,
                revenue=1000, gross_profit=300)
    # cfo=-60 → CFO/자산(-0.06) ≤ ROA(-0.05) 로 발생액 항목도 미가점 → 0점.
    cur = _fin(roa=-0.05, cfo=-60, assets=1000, liabilities=600,
               current_assets=300, current_liabilities=250,
               revenue=800, gross_profit=180)
    assert opendart.piotroski_f_score(cur, prev) == 0


def test_piotroski_f_score_insufficient_data_none():
    """계산 가능한 항목이 5개 미만이면 None."""
    assert opendart.piotroski_f_score(_fin(roa=0.1), _fin()) is None
