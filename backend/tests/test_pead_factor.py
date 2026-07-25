"""PEAD(실적 서프라이즈 드리프트) SUE 팩터 — 접수일(rcept_dt) 기준 PIT 정렬 검증.

이 팩터에서 가장 사고나기 쉬운 지점은 '아직 공시되지 않은 실적이 as_of 스코어에
유입되는 미래참조'다. 여기서는 그것을 명시적으로 못박는다:

- disclosure_calendar 가 정기공시 목록의 실제 접수일을 파싱·정렬·중복제거(최초 접수일).
- _pead_sue_one 이 rcept_dt <= as_of 인 보고서만 사용(경계 포함), as_of 이후 공시된
  실적은 그 값이 무엇이든 SUE 에 영향을 주지 못함.
- 단일분기 순이익 누적해제(_single_quarter_net_income) 산술.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.data import opendart


# ─────────────────────────── report_nm 파싱 ───────────────────────────


@pytest.mark.parametrize(
    "report_nm, rcept_dt, expected",
    [
        ("분기보고서 (2023.03)", "20230515", (date(2023, 5, 15), 2023, opendart.REPORT_Q1)),
        ("반기보고서 (2023.06)", "20230814", (date(2023, 8, 14), 2023, opendart.REPORT_HALF)),
        ("분기보고서 (2023.09)", "20231114", (date(2023, 11, 14), 2023, opendart.REPORT_Q3)),
        ("사업보고서 (2022.12)", "20230307", (date(2023, 3, 7), 2022, opendart.REPORT_ANNUAL)),
        # 정정공시 접두사가 붙어도 본문을 잡는다.
        ("[기재정정]사업보고서 (2022.12)", "20230601", (date(2023, 6, 1), 2022, opendart.REPORT_ANNUAL)),
    ],
)
def test_parse_periodic_report(report_nm, rcept_dt, expected):
    assert opendart._parse_periodic_report(report_nm, rcept_dt) == expected


@pytest.mark.parametrize(
    "report_nm, rcept_dt",
    [
        ("주요사항보고서(유상증자결정)", "20230515"),  # 비정기 공시
        ("분기보고서 (2023.03)", "2023051"),           # 잘못된 접수일 길이
        ("분기보고서 (2023.03)", "20231345"),          # 존재하지 않는 날짜
        ("사업보고서 (2022.02)", "20230307"),          # 월이 분기 매핑에 없음
    ],
)
def test_parse_periodic_report_rejects_invalid(report_nm, rcept_dt):
    assert opendart._parse_periodic_report(report_nm, rcept_dt) is None


def test_disclosure_calendar_dedupes_to_earliest(monkeypatch):
    """동일 (사업연도, 보고서코드)가 원공시+정정공시로 중복되면 최초 접수일만 남긴다."""
    opendart._CALENDAR_CACHE.clear()
    fake = {
        "list": [
            {"report_nm": "사업보고서 (2022.12)", "rcept_dt": "20230307"},
            # 같은 연간 보고서의 정정공시(더 늦은 접수일) — 가용시점을 앞당기면 안 됨.
            {"report_nm": "[기재정정]사업보고서 (2022.12)", "rcept_dt": "20230920"},
            {"report_nm": "분기보고서 (2023.03)", "rcept_dt": "20230515"},
        ]
    }
    monkeypatch.setattr(opendart, "_get", lambda path, params: fake)
    cal = opendart.disclosure_calendar("00126380", 2023)
    # (2022, ANNUAL) 은 최초 접수일 3/7 만, 접수일 오름차순.
    assert cal == [
        (date(2023, 3, 7), 2022, opendart.REPORT_ANNUAL),
        (date(2023, 5, 15), 2023, opendart.REPORT_Q1),
    ]


# ─────────────────────── 단일분기 순이익 누적해제 ───────────────────────


def test_single_quarter_net_income_decumulation(monkeypatch):
    """분기 누적치에서 단일분기 순이익을 정확히 빼낸다(반기 = 반기누적 − 1분기누적)."""
    cumulative = {
        (2023, opendart.REPORT_Q1): 100.0,     # 1~3월 누적
        (2023, opendart.REPORT_HALF): 250.0,   # 1~6월 누적 → 2분기 단독 150
        (2023, opendart.REPORT_Q3): 400.0,     # 1~9월 누적 → 3분기 단독 150
        (2023, opendart.REPORT_ANNUAL): 500.0,  # 1~12월 → 4분기 단독 100
    }
    monkeypatch.setattr(
        opendart, "_period_metrics",
        lambda corp, year, reprt: {"net_income": cumulative.get((year, reprt))},
    )
    q = opendart._single_quarter_net_income
    assert q("c", 2023, opendart.REPORT_Q1) == 100.0
    assert q("c", 2023, opendart.REPORT_HALF) == 150.0
    assert q("c", 2023, opendart.REPORT_Q3) == 150.0
    assert q("c", 2023, opendart.REPORT_ANNUAL) == 100.0


def test_single_quarter_net_income_missing_predecessor(monkeypatch):
    """직전분기 누적치가 없으면 단일분기를 산출할 수 없어 None(서프라이즈 건너뜀)."""
    monkeypatch.setattr(
        opendart, "_period_metrics",
        lambda corp, year, reprt: {
            "net_income": 250.0 if reprt == opendart.REPORT_HALF else None
        },
    )
    assert opendart._single_quarter_net_income("c", 2023, opendart.REPORT_HALF) is None


# ─────────────────── 접수일 기준 PIT(미래참조 방지) ───────────────────

_SEQ = [opendart.REPORT_Q1, opendart.REPORT_HALF, opendart.REPORT_Q3, opendart.REPORT_ANNUAL]


def _build_calendar(years):
    """연도별 4개 정기공시의 (접수일, 사업연도, 보고서코드) 캘린더를 만든다.

    접수일은 실제 마감과 유사하게: 1Q=5/15, 반기=8/14, 3Q=11/14, 연간=이듬해 3/15.
    """
    rc_dt = {
        opendart.REPORT_Q1: lambda y: date(y, 5, 15),
        opendart.REPORT_HALF: lambda y: date(y, 8, 14),
        opendart.REPORT_Q3: lambda y: date(y, 11, 14),
        opendart.REPORT_ANNUAL: lambda y: date(y + 1, 3, 15),
    }
    cal = [(rc_dt[rc](y), y, rc) for y in years for rc in _SEQ]
    return sorted(cal, key=lambda x: x[0])


def test_pead_sue_no_lookahead(monkeypatch):
    """as_of 이후 접수된(rcept_dt>as_of) 실적은 그 값이 무엇이든 SUE 에 유입되지 않는다."""
    corp = "C"
    years = [2019, 2020, 2021, 2022, 2023]
    monkeypatch.setattr(opendart, "disclosure_calendar", lambda c, ref_year: _build_calendar(years))

    # 단일분기 순이익: 미래분기(2023 3Q, 접수 2023-11-14)에 '극단값'을 심는다.
    FUTURE = (2023, opendart.REPORT_Q3)

    def sq_factory(future_value):
        base = {}
        # 전년 동기(2018 포함)를 채운다. 서프라이즈가 연도별로 달라지도록(std>0) 비선형(제곱).
        for y in [2018, *years]:
            for i, rc in enumerate(_SEQ):
                base[(y, rc)] = 100.0 + 3.0 * (y - 2018) ** 2 + i
        base[FUTURE] = future_value
        return lambda c, year, reprt: base.get((year, reprt))

    # as_of 는 2023 3Q 접수일(2023-11-14) '하루 전' — 미래분기는 아직 미공시.
    as_of = date(2023, 11, 13)

    monkeypatch.setattr(opendart, "_single_quarter_net_income", sq_factory(1e9))
    sue_a = opendart._pead_sue_one(corp, as_of, lookback_q=8, min_obs=6)
    monkeypatch.setattr(opendart, "_single_quarter_net_income", sq_factory(-1e9))
    sue_b = opendart._pead_sue_one(corp, as_of, lookback_q=8, min_obs=6)

    assert sue_a is not None
    # 미래분기 값을 +1e9 ↔ −1e9 로 뒤집어도 결과 불변 → 미래참조 없음.
    assert sue_a == sue_b


def test_pead_sue_boundary_inclusive_and_gate(monkeypatch):
    """rcept_dt==as_of 는 포함(경계), rcept_dt>as_of 는 배제 — 게이트가 접수일에 정확히 걸린다."""
    corp = "C"
    years = [2019, 2020, 2021, 2022, 2023]
    monkeypatch.setattr(opendart, "disclosure_calendar", lambda c, ref_year: _build_calendar(years))

    FUTURE = (2023, opendart.REPORT_Q3)  # 접수일 2023-11-14

    base = {}
    for y in [2018, *years]:
        for i, rc in enumerate(_SEQ):
            base[(y, rc)] = 100.0 + 3.0 * (y - 2018) ** 2 + i
    base[FUTURE] = 5000.0  # 극단 서프라이즈 — 포함되면 SUE 가 크게 달라진다.
    monkeypatch.setattr(opendart, "_single_quarter_net_income", lambda c, year, reprt: base.get((year, reprt)))

    before = opendart._pead_sue_one(corp, date(2023, 11, 13), lookback_q=8, min_obs=6)
    on = opendart._pead_sue_one(corp, date(2023, 11, 14), lookback_q=8, min_obs=6)

    assert before is not None and on is not None
    # 접수일 당일(on)에는 미래분기가 편입돼 최신 서프라이즈가 극단값이 되므로 결과가 달라진다.
    assert on != before


def test_pead_sue_insufficient_history_returns_none(monkeypatch):
    """유효 서프라이즈가 min_obs 미만이면 None(중립 폴백 대상)."""
    monkeypatch.setattr(opendart, "disclosure_calendar", lambda c, ref_year: _build_calendar([2022, 2023]))
    monkeypatch.setattr(opendart, "_single_quarter_net_income", lambda c, year, reprt: 100.0)
    # 전 서프라이즈가 0(순이익 동일) → 표준편차 0 → None.
    assert opendart._pead_sue_one("C", date(2023, 12, 1), lookback_q=8, min_obs=6) is None


def test_pead_sue_by_symbol_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(opendart, "is_enabled", lambda: False)
    assert opendart.pead_sue_by_symbol(["005930"], date(2023, 12, 1)) == {}


def test_pead_sue_by_symbol_maps_codes(monkeypatch):
    """종목코드→corp_code 매핑 후 _pead_sue_one 결과를 6자리 코드로 반환한다."""
    monkeypatch.setattr(opendart, "is_enabled", lambda: True)
    monkeypatch.setattr(opendart, "cached_corp_code_map", lambda: {"005930": "00126380"})
    monkeypatch.setattr(opendart, "_pead_sue_one", lambda corp, as_of, lb, mo: 1.23)
    out = opendart.pead_sue_by_symbol(["5930"], date(2023, 12, 1))
    assert out == {"005930": 1.23}
