"""OpenDART 클라이언트 — 파생지표 계산(derive_metrics)·status 원인 분류 검증.

네트워크 없이 도는 단위테스트다. 실제 OpenDART 응답(fnlttSinglAcntAll)의 계정
구조를 축약한 fixture 로 account_id 우선 매칭·CIS 폴백·부호·경계값을 확인한다.
실측 교차검증(삼성전자·현대차·NAVER 실제 수치 일치)은 구현 시 수동으로 완료했다.
전송 계층(`_get`)의 status 코드 원인 분류는 httpx.Client 를 목으로 갈아끼워
검증한다 — **실제 OpenDART 를 호출하지 않는다**.
"""
import io
import logging
import zipfile
from datetime import date

import httpx
import pytest

from app.services.data import opendart
from app.services.data.errors import (
    SourceAuthError,
    SourceQuotaError,
    SourceRequestError,
    SourceSchemaError,
    SourceUnavailableError,
    clear_cooldown,
    cooldown_remaining,
    note_failure,
)
from app.services.data.store.frame import make_cache_key
from app.services.data.store.ledger import InMemoryLedger


class _FakeDartStore:
    """dart_financials(정규화 테이블) + dart_accounts 원장 대역을 함께 담는 컨테이너.

    :param accounts: corp_code 등 4튜플 키 → 원계정 리스트(정규화 테이블 대역)
    :param ledger: cached_accounts 가 쓰는 페치 원장 대역(InMemoryLedger)
    """

    def __init__(self, accounts: dict[tuple, list[dict]], ledger: InMemoryLedger) -> None:
        self.accounts = accounts
        self.ledger = ledger

    # 기존 테스트가 `_fake_dart_store[key] = accounts` 형태(dict-like)로 쓰던 관례를
    # 유지한다 — 원장 검증이 필요 없는 테스트는 여전히 이렇게만 쓰면 된다.
    def __setitem__(self, key: tuple, value: list[dict]) -> None:
        self.accounts[key] = value

    def __getitem__(self, key: tuple) -> list[dict]:
        return self.accounts[key]


@pytest.fixture(autouse=True)
def _fake_dart_store(monkeypatch):
    """dart_financials 로컬 스토어와 dart_accounts 원장을 함께 인메모리로 대역한다.

    cached_accounts() 가 opendart._store_read_accounts/_store_write_accounts/
    _store_ledger 를 거쳐 로컬 DB 를 두드리므로, 네트워크 없이 도는 이 단위테스트
    스위트가 실 DB 를 타지 않도록(그리고 이전 테스트가 남긴 레코드에 캐시가 선점당하지
    않도록) 매 테스트마다 새로 갈아끼운다. 참고: tests/test_krx_index.py 의 _clear_cache.

    원장까지 여기서 autouse 로 갈아끼우는 이유: cached_accounts 가 무자료를 원장에
    기록하는 경로(§ TestCachedAccountsLedger)를 추가하면서, 원장을 전혀 의식하지 않는
    기존 테스트(TTM 등)도 cached_accounts 를 경유해 원장을 건드리게 됐다 — 개별
    테스트마다 원장을 대역하게 하면 하나라도 빠뜨렸을 때 조용히 실 DB 를 오염시킨다.
    """
    store: dict[tuple, list[dict]] = {}

    def fake_read(corp_code, bsns_year, reprt_code, fs_div):
        key = (corp_code, bsns_year, reprt_code, fs_div)
        if key not in store:
            return None
        return store[key], True  # 확정 여부 자체는 이 모듈의 관심사가 아니다

    def fake_write(corp_code, bsns_year, reprt_code, fs_div, accounts):
        store[(corp_code, bsns_year, reprt_code, fs_div)] = accounts

    monkeypatch.setattr(opendart, "_store_read_accounts", fake_read)
    monkeypatch.setattr(opendart, "_store_write_accounts", fake_write)

    ledger = InMemoryLedger()
    monkeypatch.setattr(opendart, "_store_ledger", lambda: ledger)
    opendart._ACCOUNTS_CACHE.clear()
    yield _FakeDartStore(accounts=store, ledger=ledger)
    opendart._ACCOUNTS_CACHE.clear()


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


# ─────────────────── cached_accounts 의 dart_accounts 원장 배선 ───────────────────


class TestCachedAccountsLedger:
    """cached_accounts() 의 dart_accounts 원장 배선(무자료 확정/미확정, 실패 전파).

    원장 대역(InMemoryLedger)은 autouse `_fake_dart_store` 가 이미 갈아끼워 주므로,
    이 클래스의 테스트들은 그 픽스처가 돌려주는 `.ledger`/`.accounts` 를 그대로 쓴다.
    """

    def test_no_data_recorded_final_when_year_confirmed(self, monkeypatch, _fake_dart_store):
        """확정된 과거 사업연도의 무자료(status 013)는 원장에 final=True 로 굳고,
        다음 호출은 single_company_accounts 를 다시 부르지 않는다(핵심 회귀)."""
        calls = []

        def fake_accounts(corp, year, reprt, fs):
            calls.append((corp, year, reprt, fs))
            return None  # status 013 무자료

        monkeypatch.setattr(opendart, "single_company_accounts", fake_accounts)

        bsns_year = 2020  # confirmed_date(None, 2020) = 2021-12-31 → 이미 지남
        args = ("00012345", bsns_year, opendart.REPORT_ANNUAL, opendart.FS_CONSOLIDATED)

        first = opendart.cached_accounts(*args)
        second = opendart.cached_accounts(*args)

        assert first is None
        assert second is None
        assert len(calls) == 1  # 두 번째 호출은 원장만 보고 망을 타지 않는다

        entry = _fake_dart_store.ledger.get("dart_accounts", make_cache_key(*args))
        assert entry is not None
        assert entry.row_count == 0
        assert entry.final is True

    def test_no_data_not_final_when_year_unconfirmed_retries(self, monkeypatch, _fake_dart_store):
        """당해/최근 사업연도의 무자료는 확정 기록되지 않고, 호출마다 재조회한다
        (나중에 제출되는 재무제표를 놓치지 않기 위한 보수성)."""
        calls = []

        def fake_accounts(corp, year, reprt, fs):
            calls.append((corp, year, reprt, fs))
            return None

        monkeypatch.setattr(opendart, "single_company_accounts", fake_accounts)

        bsns_year = date.today().year  # confirmed_date 가 미래 → final=False
        args = ("00012345", bsns_year, opendart.REPORT_ANNUAL, opendart.FS_CONSOLIDATED)

        opendart.cached_accounts(*args)
        opendart.cached_accounts(*args)

        assert len(calls) == 2  # 매 호출 재조회

        entry = _fake_dart_store.ledger.get("dart_accounts", make_cache_key(*args))
        assert entry is not None
        assert entry.row_count == 0
        assert entry.final is False

    def test_confirmed_local_store_skips_network(self, monkeypatch, _fake_dart_store):
        """dart_financials 에 확정 적재분이 있으면 single_company_accounts 를 부르지
        않는다(기존 2단 캐시 동작 — 원장 배선 추가로 깨지지 않았음을 확인)."""
        args = ("00012345", 2022, opendart.REPORT_ANNUAL, opendart.FS_CONSOLIDATED)
        sample = [_row("BS", "ifrs-full_Assets", "자산총계", "1,000")]
        _fake_dart_store[args] = sample

        def fail_if_called(*a, **k):
            raise AssertionError("확정 적재분이 있으면 network 를 타면 안 된다")

        monkeypatch.setattr(opendart, "single_company_accounts", fail_if_called)

        result = opendart.cached_accounts(*args)

        assert result == sample
        # 로컬 히트는 원장을 안 건드린다.
        assert _fake_dart_store.ledger.get("dart_accounts", make_cache_key(*args)) is None

    def test_fetch_failure_propagates_without_ledger_write(self, monkeypatch, _fake_dart_store):
        """조회 실패(DataSourceError)는 원장에 아무것도 남기지 않고 그대로 전파된다
        (실패를 '무자료'로 뭉개지 않는다)."""

        def boom(corp, year, reprt, fs):
            raise SourceUnavailableError("dart", "시스템 점검")

        monkeypatch.setattr(opendart, "single_company_accounts", boom)

        args = ("00012345", 2020, opendart.REPORT_ANNUAL, opendart.FS_CONSOLIDATED)
        with pytest.raises(SourceUnavailableError):
            opendart.cached_accounts(*args)

        assert _fake_dart_store.ledger.get("dart_accounts", make_cache_key(*args)) is None


# ─────────────────── 전송 계층 status 코드 원인 분류 ───────────────────


def _mock_status(monkeypatch, status: str):
    """지정 status 를 돌려주는 httpx.Client 목(실제 네트워크를 타지 않는다)."""

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": status, "message": "목"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(opendart.httpx, "Client", _Client)


class TestStatusClassification:
    """OpenDART 응답 본문 status 코드 → 원인별 예외 매핑.

    autouse 픽스처를 클래스 안에 두는 이유: 모듈 전역으로 두면 `is_enabled` 를 True 로
    고정해 미설정 동작을 검증하는 `test_disabled_without_key` 를 깨뜨린다.
    """

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        monkeypatch.setattr(opendart, "is_enabled", lambda: True)
        clear_cooldown("dart")
        yield
        clear_cooldown("dart")

    @pytest.mark.parametrize("status", ["010", "011", "012"])
    def test_키_문제는_Auth(self, monkeypatch, status):
        _mock_status(monkeypatch, status)
        with pytest.raises(SourceAuthError):
            opendart._get("fnlttSinglAcnt.json", {})

    @pytest.mark.parametrize("status", ["020", "021"])
    def test_한도_초과는_Quota(self, monkeypatch, status):
        _mock_status(monkeypatch, status)
        with pytest.raises(SourceQuotaError):
            opendart._get("fnlttSinglAcnt.json", {})

    def test_일일한도는_다음_자정까지_쿨다운(self, monkeypatch):
        """실제 자정까지의 초를 쓰면 자정 직전에 실행될 때 흔들리므로 고정한다."""
        monkeypatch.setattr(opendart, "seconds_until_midnight", lambda: 7200.0)
        _mock_status(monkeypatch, "020")
        with pytest.raises(SourceQuotaError):
            opendart._get("fnlttSinglAcnt.json", {})
        assert 7100 < cooldown_remaining("dart") <= 7200  # 클래스 기본값(300s)이 아니다

    @pytest.mark.parametrize("status", ["014", "100", "101"])
    def test_잘못된_요청은_Request(self, monkeypatch, status):
        """014(파일 부재)·100(부적절한 값)·101(부적절한 접근) — 모두 우리 요청이 잘못된 것."""
        _mock_status(monkeypatch, status)
        with pytest.raises(SourceRequestError):
            opendart._get("fnlttSinglAcnt.json", {})

    def test_시스템_점검은_Unavailable(self, monkeypatch):
        _mock_status(monkeypatch, "800")
        with pytest.raises(SourceUnavailableError):
            opendart._get("fnlttSinglAcnt.json", {})

    def test_미지_코드는_보수적으로_Unavailable(self, monkeypatch):
        _mock_status(monkeypatch, "777")
        with pytest.raises(SourceUnavailableError):
            opendart._get("fnlttSinglAcnt.json", {})

    def test_무자료_013은_예외가_아니라_None(self, monkeypatch):
        """소스가 '없다'고 정상 응답한 것 — 과거 구간엔 이런 종목이 흔하다."""
        _mock_status(monkeypatch, "013")
        assert opendart._get("fnlttSinglAcnt.json", {}) is None

    def test_정상_000은_데이터_반환(self, monkeypatch):
        _mock_status(monkeypatch, "000")
        assert opendart._get("fnlttSinglAcnt.json", {})["status"] == "000"

    def test_미설정이면_예외가_아니라_None(self, monkeypatch):
        monkeypatch.setattr(opendart, "is_enabled", lambda: False)
        assert opendart._get("fnlttSinglAcnt.json", {}) is None

    def test_쿨다운_중이면_요청_자체를_하지_않는다(self, monkeypatch):
        """쿨다운 가드의 값어치는 예외를 던지는 것이 아니라 **전송을 막는 것**이다.

        종목 루프는 _get 을 종목당 여러 번 부른다 — 일일 한도(020)를 소진한 뒤에도
        막지 않으면 남은 전 종목이 확정 실패인 요청을 그대로 내보내 한도 차단을
        스스로 연장한다. 그래서 '요청이 0건'을 직접 단언한다.
        (선례: test_krx_index.py 의 test_쿨다운_중이면_POST_자체를_하지_않는다)
        """
        note_failure(SourceQuotaError("dart", "한도 소진"))
        calls: list[int] = []

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **kw):
                calls.append(1)
                raise AssertionError("쿨다운 중에는 요청을 보내면 안 된다")

        monkeypatch.setattr(opendart.httpx, "Client", _Client)
        with pytest.raises(SourceQuotaError):
            opendart._get("fnlttSinglAcnt.json", {})
        assert calls == []  # 요청 자체가 나가지 않았다


class TestAggregateFailure:
    """집계 계층 — '성공'은 응답 수신이지 데이터 획득이 아니다.

    과거 백테스트 구간에는 재무제표 미제출 종목이 흔하다. 무자료(013→None)를
    실패로 세면 정상 백테스트가 '전량 실패'로 죽는다. 그래서 전량 실패
    (성공 0 & 실패 ≥1)일 때만 raise 하고, 부분 실패는 부분 결과를 돌려준다.
    """

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        # is_enabled 를 고정하지 않으면 컨테이너의 키 주입 여부에 따라 결과가 갈리고,
        # 키가 있으면 실제 OpenDART 를 호출하게 된다. corp_map 도 함께 대역으로 건다.
        monkeypatch.setattr(opendart, "is_enabled", lambda: True)
        monkeypatch.setattr(
            opendart,
            "cached_corp_code_map",
            lambda: {"005930": "00126380", "000660": "00164779", "035720": "00164742"},
        )
        clear_cooldown("dart")
        yield
        clear_cooldown("dart")

    def test_전_종목_무자료면_빈_dict(self, monkeypatch):
        """실패가 아니다 — 정상 백테스트가 죽으면 안 된다."""
        monkeypatch.setattr(opendart, "annual_metrics", lambda c, y: {"roe": None})
        out = opendart.metrics_by_symbol(["005930", "000660"], date(2026, 8, 3))
        assert out == {}

    def test_전_종목_실패면_raise(self, monkeypatch):
        def _boom(corp, year):
            raise SourceUnavailableError("dart", "타임아웃")

        monkeypatch.setattr(opendart, "annual_metrics", _boom)
        with pytest.raises(SourceUnavailableError):
            opendart.metrics_by_symbol(["005930", "000660"], date(2026, 8, 3))

    def test_일부만_실패하면_부분_결과(self, monkeypatch):
        """`_half` 는 note_failure 를 부르지 않아 쿨다운이 안 걸린다 — 실패 종목 이후에도
        루프가 끝까지 돈다. 실제 `_get` 은 실패 시 쿨다운을 걸므로 현실의 '부분 결과'는
        항상 **첫 실패 지점까지**다(그 경로는 TestAggregateCooldownShortCircuit 이 덮는다).
        여기서 고정하는 것은 '실패가 섞여도 raise 하지 않고 값을 돌려준다'는 계약뿐이다.
        """
        def _half(corp, year):
            if corp == "00126380":
                return {"roe": 0.12}
            raise SourceUnavailableError("dart", "타임아웃")

        monkeypatch.setattr(opendart, "annual_metrics", _half)
        out = opendart.metrics_by_symbol(["005930", "000660"], date(2026, 8, 3))
        assert "005930" in out and "000660" not in out

    def test_PEAD_전_종목_표본부족은_빈_dict(self, monkeypatch):
        """SUE 표본 부족(None)은 실패가 아니라 정상적인 '산출 불가'다."""
        monkeypatch.setattr(opendart, "_pead_sue_one", lambda *a: None)
        assert opendart.pead_sue_by_symbol(["005930", "000660"], date(2026, 8, 3)) == {}

    def test_PEAD_전_종목_실패면_raise(self, monkeypatch):
        def _boom(*a):
            raise SourceUnavailableError("dart", "타임아웃")

        monkeypatch.setattr(opendart, "_pead_sue_one", _boom)
        with pytest.raises(SourceUnavailableError):
            opendart.pead_sue_by_symbol(["005930", "000660"], date(2026, 8, 3))

    def test_PEAD_일부만_실패하면_부분_결과(self, monkeypatch):
        def _half(corp, as_of, lookback_q, min_obs):
            if corp == "00126380":
                return 1.5
            raise SourceUnavailableError("dart", "타임아웃")

        monkeypatch.setattr(opendart, "_pead_sue_one", _half)
        out = opendart.pead_sue_by_symbol(["005930", "000660"], date(2026, 8, 3))
        assert out == {"005930": 1.5}


class TestAggregateCooldownShortCircuit:
    """쿨다운이 걸린 뒤에는 남은 종목을 더 부르지 않는다.

    첫 실패가 쿨다운을 걸면 이후 종목의 `_get` 은 네트워크 전에 차단돼
    `SourceQuotaError("쿨다운 중 …")` 를 던진다. 계속 돌리면 이 **자기유발** 예외가
    쌓여 `representative` 의 우선순위(Quota > Unavailable)를 타고 대표 원인이 되고,
    운영자는 네트워크 장애를 '한도 초과'로 오독한다. 선례: krx_index.py 의 집계들.

    대역은 전송 계층(`_get`)의 두 가지 행동을 그대로 흉내낸다 —
    실패 시 `note_failure`, 쿨다운 중이면 Quota 로 즉시 거절.
    """

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        monkeypatch.setattr(opendart, "is_enabled", lambda: True)
        monkeypatch.setattr(
            opendart,
            "cached_corp_code_map",
            lambda: {"005930": "00126380", "000660": "00164779", "035720": "00164742"},
        )
        clear_cooldown("dart")
        yield
        clear_cooldown("dart")

    @staticmethod
    def _transport(calls: list[str], ok_corp: str | None = None):
        """`_get` 의 쿨다운 가드·note_failure 를 흉내내는 종목 단위 대역."""

        def _fake(corp, *_a):
            calls.append(corp)
            if cooldown_remaining("dart") > 0:
                raise SourceQuotaError("dart", "쿨다운 중 — 조회 생략")
            if corp == ok_corp:
                return {"roe": 0.12}
            exc = SourceUnavailableError("dart", "타임아웃")
            note_failure(exc)
            raise exc

        return _fake

    def test_대표_원인이_자기유발_Quota_로_오염되지_않는다(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(opendart, "annual_metrics", self._transport(calls))
        with pytest.raises(SourceUnavailableError):  # Quota 가 아니다
            opendart.metrics_by_symbol(["005930", "000660", "035720"], date(2026, 8, 3))
        assert calls == ["00126380"]  # 첫 실패 이후 아무도 더 부르지 않았다

    def test_단락해도_이미_성공한_종목의_결과는_살아남는다(self, monkeypatch):
        """조기 break 가 '부분 실패는 값을 반환한다' 계약을 깨면 안 된다."""
        calls: list[str] = []
        monkeypatch.setattr(
            opendart, "annual_metrics", self._transport(calls, ok_corp="00126380")
        )
        out = opendart.metrics_by_symbol(["005930", "000660", "035720"], date(2026, 8, 3))
        assert "005930" in out  # 쿨다운 전에 성공한 종목은 보존
        assert "00164742" not in calls  # 세 번째 종목은 시도조차 하지 않는다

    def test_부분_실패_로그가_건너뛴_종목을_감추지_않는다(self, monkeypatch, caplog):
        """단락이 걸리면 '실패 N / 전체 M' 만으로는 운영자가 나머지를 정상으로 오독한다.

        200 종목 중 2번째에서 단락되면 실패는 1건뿐이지만 데이터를 얻은 것도 1종목이고
        198 종목은 **시도조차 안 됐다**. ok(성공 수)가 예외도 반환값도 아닌 이 로그에만
        드러나므로, 셋을 함께 찍지 않으면 이 스펙이 없애려는 조용한 실패를 로그가
        정상으로 포장한다.
        """
        calls: list[str] = []
        monkeypatch.setattr(
            opendart, "annual_metrics", self._transport(calls, ok_corp="00126380")
        )
        with caplog.at_level(logging.ERROR, logger="app.services.data.opendart"):
            opendart.metrics_by_symbol(["005930", "000660", "035720"], date(2026, 8, 3))
        # 전체 3 중 성공 1 — 나머지 2 를 '정상'으로 읽을 수 없어야 한다.
        assert "성공 1 / 실패 1 / 전체 3" in caplog.text

    def test_PEAD_도_같은_단락을_한다(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(opendart, "_pead_sue_one", self._transport(calls))
        with pytest.raises(SourceUnavailableError):
            opendart.pead_sue_by_symbol(["005930", "000660", "035720"], date(2026, 8, 3))
        assert calls == ["00126380"]


class TestCorpCodeMap:
    """corpCode 매핑의 실패 분기 — 빈 매핑을 조용히 돌려주지 않는지가 핵심.

    corp_code 매핑이 비면 이후 모든 종목이 corp_code 를 못 찾아 전 종목이 조용히
    '재무 정보 없음'이 된다. 이 스펙 전체가 없애려는 실패 형태가 바로 그것이다.
    """

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        monkeypatch.setattr(opendart, "is_enabled", lambda: True)
        clear_cooldown("dart")
        yield
        clear_cooldown("dart")

    @staticmethod
    def _mock_zip(monkeypatch, xml: bytes):
        """corpCode.xml(zip) 을 돌려주는 httpx.Client 목(실제 네트워크를 타지 않는다)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("CORPCODE.xml", xml)
        payload = buf.getvalue()

        class _Resp:
            status_code = 200
            content = payload

            def raise_for_status(self):
                return None

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **kw):
                return _Resp()

        monkeypatch.setattr(opendart.httpx, "Client", _Client)

    def test_다운로드_실패는_Unavailable(self, monkeypatch):
        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **kw):
                raise httpx.ConnectError("refused")

        monkeypatch.setattr(opendart.httpx, "Client", _Client)
        with pytest.raises(SourceUnavailableError):
            opendart.corp_code_map()

    def test_XML_파싱_실패는_Schema(self, monkeypatch):
        self._mock_zip(monkeypatch, b"<result><list>")  # 닫히지 않은 태그
        with pytest.raises(SourceSchemaError):
            opendart.corp_code_map()

    def test_상장사_0개는_Schema(self, monkeypatch):
        """다운로드·파싱은 됐는데 상장사가 0개 — 스키마 변경으로 보고 raise 한다.

        빈 dict 를 그대로 돌려주면 호출부는 '정상적으로 조회했는데 매칭이 없다'로
        읽어, 전 종목 재무 지표가 조용히 사라진다.
        """
        # stock_code 가 공백인 항목만(비상장·상폐) → 상장사 매핑이 0개가 된다.
        self._mock_zip(monkeypatch, (
            "<result>"
            "<list><corp_code>00126380</corp_code>"
            "<corp_name>비상장회사</corp_name><stock_code> </stock_code></list>"
            "</result>"
        ).encode("utf-8"))
        with pytest.raises(SourceSchemaError):
            opendart.corp_code_map()

    def test_정상_매핑은_그대로_반환(self, monkeypatch):
        """빈 매핑 가드가 무조건 raise 하는 것이 아님을 고정한다(상장사 0개 테스트의 대조군)."""
        self._mock_zip(monkeypatch, (
            "<result>"
            "<list><corp_code>00126380</corp_code>"
            "<corp_name>삼성전자</corp_name><stock_code>005930</stock_code></list>"
            "<list><corp_code>00164779</corp_code>"
            "<corp_name>비상장회사</corp_name><stock_code> </stock_code></list>"
            "</result>"
        ).encode("utf-8"))
        assert opendart.corp_code_map() == {"005930": "00126380"}

    def test_미설정이면_예외가_아니라_None(self, monkeypatch):
        monkeypatch.setattr(opendart, "is_enabled", lambda: False)
        assert opendart.corp_code_map() is None


class TestAggregateSystemicSchemaShortCircuit:
    """쿨다운이 없는 체계적 실패(응답 포맷 변경)도 루프를 멈춰야 한다.

    Schema 는 기다려도 해결되지 않으므로 쿨다운을 걸지 않는다(설계). 그래서 단락이
    쿨다운만 보면 이 원인에는 **아예 걸리지 않아**, 포맷이 바뀐 날 전 종목 루프가 끝까지
    돌며 종목당 3회씩 호출을 소진한 뒤에야 raise 한다 — DART 는 일일 20,000건 한도가
    있고, 리밸런싱일마다 부르는 백테스트 한 건이면 한도를 통째로 태울 수 있다.
    """

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        monkeypatch.setattr(opendart, "is_enabled", lambda: True)
        monkeypatch.setattr(
            opendart, "cached_corp_code_map",
            lambda: {f"00{i:04d}": f"c{i}" for i in range(12)},
        )
        clear_cooldown("dart")
        yield
        clear_cooldown("dart")

    def test_스키마_오류가_이어지면_전_종목을_돌지_않는다(self, monkeypatch):
        calls: list[str] = []

        def _fake(corp, *_a):
            calls.append(corp)
            raise SourceSchemaError("dart", "list 키가 없다")  # 쿨다운 없음

        monkeypatch.setattr(opendart, "annual_metrics", _fake)
        codes = [f"00{i:04d}" for i in range(12)]
        with pytest.raises(SourceSchemaError):
            opendart.metrics_by_symbol(codes, date(2026, 8, 3))

        # 12종목 전부(×3회)를 태우지 않고 임계에서 멈춘다.
        assert len(calls) < len(codes)

    def test_한_번이라도_성공했으면_끝까지_돈다(self, monkeypatch):
        """형식이 맞다는 증거가 있으면 나머지 실패는 종목별 사정 — 부분 결과를 지킨다."""
        calls: list[str] = []

        def _fake(corp, *_a):
            calls.append(corp)
            if corp == "c0":
                return {"roe": 0.12}
            raise SourceSchemaError("dart", "list 키가 없다")

        monkeypatch.setattr(opendart, "annual_metrics", _fake)
        codes = [f"00{i:04d}" for i in range(12)]
        out = opendart.metrics_by_symbol(codes, date(2026, 8, 3))

        assert "000000" in out  # 성공한 종목의 부분 결과가 살아 있다
        # 첫 종목만 성공(3회 호출) → 나머지 11종목도 각각 시도됐다
        assert len({c for c in calls}) == len(codes)


from app.services.data.store.dart_store import confirmed_date  # noqa: E402


def test_확정일은_접수일_90일_후다():
    assert confirmed_date(date(2026, 3, 20), 2025) == date(2026, 6, 18)


def test_접수일_미상이면_사업연도_말_1년_후다():
    """보수적으로 잡는다 — 확정을 앞당기는 것보다 늦추는 쪽이 안전하다."""
    assert confirmed_date(None, 2025) == date(2026, 12, 31)
