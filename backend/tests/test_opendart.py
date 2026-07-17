"""OpenDART 클라이언트 — 파생지표 계산(derive_metrics) 및 graceful degradation 검증.

네트워크 없이 도는 단위테스트다. 실제 OpenDART 응답(fnlttSinglAcntAll)의 계정
구조를 축약한 fixture 로 account_id 우선 매칭·CIS 폴백·부호·경계값을 확인한다.
실측 교차검증(삼성전자·현대차·NAVER 실제 수치 일치)은 구현 시 수동으로 완료했다.
"""
from datetime import date

import pytest

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
    assert m["equity"] == 363_677_865_000_000  # TTM 합성(_combine_ttm)이 참조하는 저량값
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


# ───────────────────── 분기 TTM(트레일링 4분기) ─────────────────────


def _fin_accounts(*, revenue, op_income, net_income, assets, liabilities, equity):
    """TTM 테스트용 단순 재무제표 fixture(BS 저량 3개 + IS 손익 3개)."""
    return [
        _row("BS", "ifrs-full_Assets", "자산총계", str(assets)),
        _row("BS", "ifrs-full_Liabilities", "부채총계", str(liabilities)),
        _row("BS", "ifrs-full_Equity", "자본총계", str(equity)),
        _row("IS", "ifrs-full_Revenue", "매출액", str(revenue)),
        _row("IS", "dart_OperatingIncomeLoss", "영업이익", str(op_income)),
        _row("IS", "ifrs-full_ProfitLoss", "당기순이익", str(net_income)),
    ]


def test_combine_ttm_telescopes_flow_and_keeps_latest_stock():
    """flow(손익) 항목은 전년연간-전년동기+당해동기로 합성, stock(BS)은 당해 시점값 그대로."""
    cur = opendart.derive_metrics(_fin_accounts(
        revenue=100, op_income=20, net_income=10, assets=2000, liabilities=800, equity=1200,
    ))
    prev_same = opendart.derive_metrics(_fin_accounts(
        revenue=80, op_income=15, net_income=8, assets=1800, liabilities=750, equity=1050,
    ))
    prev_annual = opendart.derive_metrics(_fin_accounts(
        revenue=400, op_income=70, net_income=40, assets=1900, liabilities=770, equity=1130,
    ))
    combined = opendart._combine_ttm(cur, prev_same, prev_annual)
    assert combined["revenue"] == 400 - 80 + 100  # 420
    assert combined["op_income"] == 70 - 15 + 20   # 75
    assert combined["net_income"] == 40 - 8 + 10   # 42
    # 재무상태표는 당해 분기(cur) 시점값 그대로.
    assert combined["assets"] == 2000
    assert combined["liabilities"] == 800
    assert combined["equity"] == 1200
    # 비율은 텔레스코핑된 net_income 과 최신 시점 equity/assets 로 재계산.
    assert combined["roe"] == pytest.approx(42 / 1200)
    assert combined["debt_ratio"] == pytest.approx(800 / 1200)
    assert combined["roa"] == pytest.approx(42 / 2000)


def test_combine_ttm_missing_prior_period_leaves_flow_none():
    """전년 자료가 없으면(상장 이력 짧음 등) flow 는 None, stock 은 여전히 당해값."""
    cur = opendart.derive_metrics(_fin_accounts(
        revenue=100, op_income=20, net_income=10, assets=2000, liabilities=800, equity=1200,
    ))
    empty = opendart.derive_metrics([])
    combined = opendart._combine_ttm(cur, empty, empty)
    assert combined["revenue"] is None
    assert combined["net_income"] is None
    assert combined["assets"] == 2000  # stock 은 cur 그대로 보존


def test_ttm_metrics_annual_passthrough(monkeypatch):
    """reprt_code 가 사업보고서면 TTM==연간 그 자체(전년 동기 등 추가 조회 없음)."""
    opendart._PERIOD_METRICS_CACHE.clear()
    opendart._ACCOUNTS_CACHE.clear()
    calls = []

    def fake_accounts(corp, year, reprt, fs):
        calls.append((year, reprt, fs))
        if reprt == opendart.REPORT_ANNUAL and fs == opendart.FS_CONSOLIDATED:
            return _fin_accounts(
                revenue=400, op_income=70, net_income=40,
                assets=1900, liabilities=770, equity=1130,
            )
        return None

    monkeypatch.setattr(opendart, "single_company_accounts", fake_accounts)
    m = opendart.ttm_metrics("00012345", 2024, opendart.REPORT_ANNUAL)
    assert m["revenue"] == 400
    assert m["net_income"] == 40
    # 연간 경로는 (2024, 연간) 한 번만 조회 — 전년동기 등은 조회하지 않는다.
    assert {(y, r) for y, r, _fs in calls} == {(2024, opendart.REPORT_ANNUAL)}


def test_ttm_metrics_quarterly_telescopes(monkeypatch):
    """1분기 보고서 시점의 TTM = 전년연간 - 전년1분기 + 당해1분기."""
    opendart._PERIOD_METRICS_CACHE.clear()
    opendart._ACCOUNTS_CACHE.clear()

    table = {
        (2025, opendart.REPORT_Q1): _fin_accounts(
            revenue=100, op_income=20, net_income=10, assets=2000, liabilities=800, equity=1200,
        ),
        (2024, opendart.REPORT_Q1): _fin_accounts(
            revenue=80, op_income=15, net_income=8, assets=1800, liabilities=750, equity=1050,
        ),
        (2024, opendart.REPORT_ANNUAL): _fin_accounts(
            revenue=400, op_income=70, net_income=40, assets=1900, liabilities=770, equity=1130,
        ),
    }

    def fake_accounts(corp, year, reprt, fs):
        if fs != opendart.FS_CONSOLIDATED:
            return None  # 개별(OFS) 폴백까지 갈 필요 없는 fixture
        return table.get((year, reprt))

    monkeypatch.setattr(opendart, "single_company_accounts", fake_accounts)
    m = opendart.ttm_metrics("00012345", 2025, opendart.REPORT_Q1)
    assert m["revenue"] == 420
    assert m["op_income"] == 75
    assert m["net_income"] == 42
    assert m["assets"] == 2000  # 당해 1분기 시점 재무상태표


def test_ttm_metrics_falls_back_to_annual_when_quarter_missing(monkeypatch):
    """당해 분기 원자료가 없으면(상장 이력 짧음 등) 직전 확정 연간으로 안전 폴백."""
    opendart._PERIOD_METRICS_CACHE.clear()
    opendart._ACCOUNTS_CACHE.clear()

    annual_2024 = _fin_accounts(
        revenue=400, op_income=70, net_income=40, assets=1900, liabilities=770, equity=1130,
    )

    def fake_accounts(corp, year, reprt, fs):
        if fs != opendart.FS_CONSOLIDATED:
            return None
        if (year, reprt) == (2025, opendart.REPORT_Q1):
            return None  # 당해 1분기 무자료(신규상장 등)
        if (year, reprt) == (2024, opendart.REPORT_ANNUAL):
            return annual_2024
        return None

    monkeypatch.setattr(opendart, "single_company_accounts", fake_accounts)
    m = opendart.ttm_metrics("00099999", 2025, opendart.REPORT_Q1)
    assert m["revenue"] == 400  # 연간 폴백값 그대로
    assert m["net_income"] == 40


def test_metrics_by_symbol_use_ttm_true_respects_pit(monkeypatch):
    """use_ttm=True — latest_report_period 로 정한 PIT 안전 분기만 조회하고,
    전년·전전년(동일 reprt_code) 은 과거이므로 룩어헤드가 아니다.

    기본값은 False(기존 연간 경로) 다 — id=23/24 등 기존 등록 전략의 백테스트
    재현성을 깨지 않기 위함. TTM 은 명시적 opt-in."""
    monkeypatch.setattr(opendart, "is_enabled", lambda: True)
    monkeypatch.setattr(opendart, "cached_corp_code_map", lambda: {"000000": "00000000"})

    calls = []

    def fake_ttm(corp, year, reprt):
        calls.append((year, reprt))
        return {"roe": 0.1, "debt_ratio": 0.5, "op_income": 1.0,
                "net_income": 1.0, "fcf": 1.0, "roa": 0.05}

    monkeypatch.setattr(opendart, "ttm_metrics", fake_ttm)
    out = opendart.metrics_by_symbol(["000000"], date(2025, 5, 20), use_ttm=True)
    assert "000000" in out
    # as_of=2025-05-20 → latest_report_period 는 (2025, Q1). 전년/전전년은
    # 같은 reprt_code(Q1)의 과거 연도만 — 미래 분기(반기/3Q/차년도)는 절대 조회 안 함.
    assert calls == [
        (2025, opendart.REPORT_Q1),
        (2024, opendart.REPORT_Q1),
        (2023, opendart.REPORT_Q1),
    ]


def test_metrics_by_symbol_use_ttm_false_uses_annual_path(monkeypatch):
    """use_ttm=False 는 기존 연간 경로(annual_metrics)를 그대로 사용한다(하위호환)."""
    monkeypatch.setattr(opendart, "is_enabled", lambda: True)
    monkeypatch.setattr(opendart, "cached_corp_code_map", lambda: {"000000": "00000000"})
    calls = []

    def fake_annual(corp, year):
        calls.append(year)
        return {"roe": 0.1, "debt_ratio": 0.5, "op_income": 1.0,
                "net_income": 1.0, "fcf": 1.0, "roa": 0.05}

    monkeypatch.setattr(opendart, "annual_metrics", fake_annual)
    monkeypatch.setattr(opendart, "ttm_metrics",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ttm 호출 금지")))
    out = opendart.metrics_by_symbol(["000000"], date(2025, 5, 20), use_ttm=False)
    assert "000000" in out
    assert set(calls) == {2024, 2023, 2022}  # announcement_lagged_year 기반 연간만
