"""app/services/recommend.py::compute_kospi200_scored 순수 로직 검증(§신규발굴 4차 2위).

추천 화면 전용 스코어링에 테스트가 전무했다. 실 KRX/OpenDART 호출 없이
`app.services.recommend` 네임스페이스에 바인딩된 fetch 헬퍼들을 대역화해,
멤버·시가총액 부재 시 빈 결과 반환, 종가 폴백(시가총액/상장주식수),
OpenDART 실패 시 중립 처리를 검증한다.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import recommend as recommend_mod
from app.services.data import krx_index, opendart
from app.services.data.errors import SourceUnavailableError

AS_OF = date(2026, 7, 20)


def test_no_members_returns_empty_result(monkeypatch):
    monkeypatch.setattr(krx_index, "index_members", lambda as_of, idx: [])

    out = recommend_mod.compute_kospi200_scored(AS_OF)

    assert out.count == 0
    assert out.items == []
    assert out.index == "KOSPI200"


def test_empty_market_cap_returns_empty_result(monkeypatch):
    monkeypatch.setattr(krx_index, "index_members", lambda as_of, idx: ["005930"])
    monkeypatch.setattr(recommend_mod, "_fetch_fundamentals", lambda *a: pd.DataFrame())
    monkeypatch.setattr(recommend_mod, "_fetch_market_cap", lambda *a: pd.DataFrame())

    out = recommend_mod.compute_kospi200_scored(AS_OF)

    assert out.count == 0
    assert out.items == []


def _patch_common(monkeypatch, *, qmetrics_side_effect=None):
    """멤버 2종목(005930 종가결측→폴백, 000660 정상)에 대한 최소 픽스처 배선."""
    codes = ["005930", "000660"]
    monkeypatch.setattr(krx_index, "index_members", lambda as_of, idx: codes)

    fund_df = pd.DataFrame(
        {"PER": [15.0, 10.0], "PBR": [1.2, 0.9], "DIV": [1.5, 2.0]}, index=codes,
    )
    cap_df = pd.DataFrame(
        {"시가총액": [400_000_000_000_000, 100_000_000_000_000],
         "상장주식수": [5_000_000_000, 700_000_000]},
        index=codes,
    )
    # 005930 은 당일 종가 결측 → market_cap/shares 폴백 경로를 태운다.
    pc_1d = pd.DataFrame(
        {"종가": [np.nan, 143000.0], "등락률": [0.5, -0.3], "거래대금": [1e12, 2e11],
         "종목명": ["삼성전자", "SK하이닉스"]},
        index=codes,
    )
    pc_21d = pd.DataFrame({"등락률": [2.0, 1.0], "거래대금": [1e12, 2e11]}, index=codes)
    pc_63d = pd.DataFrame({"등락률": [5.0, 3.0], "거래대금": [1e12, 2e11]}, index=codes)
    pc_126d = pd.DataFrame({"등락률": [8.0, 4.0], "거래대금": [1e12, 2e11]}, index=codes)

    monkeypatch.setattr(recommend_mod, "_fetch_fundamentals", lambda *a: fund_df)
    monkeypatch.setattr(recommend_mod, "_fetch_market_cap", lambda *a: cap_df)

    calls = {"n": 0}

    def _fake_fetch_price_change(start_ymd, end_ymd, mkts):
        calls["n"] += 1
        return [pc_1d, pc_21d, pc_63d, pc_126d][calls["n"] - 1]

    monkeypatch.setattr(recommend_mod, "_fetch_price_change", _fake_fetch_price_change)
    monkeypatch.setattr(
        recommend_mod, "_compute_tech_indicators",
        lambda code, start, end: {
            "high_52w_ratio": 0.9, "rsi14": 55.0, "vol_ann": 0.2,
            "mdd_252": -0.15, "trend_aligned": True, "above_sma200": True,
        },
    )
    monkeypatch.setattr(recommend_mod, "_build_name_map", lambda: {})
    monkeypatch.setattr(
        recommend_mod, "_build_krx_name_map",
        lambda *frames: {"005930": "삼성전자", "000660": "SK하이닉스"},
    )
    if qmetrics_side_effect is not None:
        monkeypatch.setattr(opendart, "metrics_by_symbol", qmetrics_side_effect)
    return codes


def test_price_close_falls_back_to_market_cap_over_shares(monkeypatch):
    _patch_common(monkeypatch, qmetrics_side_effect=lambda members, as_of: {})

    out = recommend_mod.compute_kospi200_scored(AS_OF)

    assert out.count == 2
    by_code = {m.code: m for m in out.items}
    # 005930: 종가 결측 → 400조 / 50억주 = 80,000
    assert by_code["005930"].price == 80_000
    # 000660: 정상 종가 그대로
    assert by_code["000660"].price == 143_000


def test_price_none_when_close_and_fallback_both_missing(monkeypatch):
    """§50: 종가·폴백(시가총액/상장주식수) 둘 다 결측이면 price 는 0 sentinel 이 아니라
    None 이어야 한다 — 0 은 실제 KRX 상장종목에서 나올 수 없는 값인데도 예전엔 0 으로
    채워 "값 있음"과 구분이 안 됐다.
    """
    _patch_common(monkeypatch, qmetrics_side_effect=lambda members, as_of: {})
    # 상장주식수 컬럼 자체를 없애 폴백 경로를 막는다(005930 은 원래도 당일 종가 결측).
    cap_df = pd.DataFrame(
        {"시가총액": [400_000_000_000_000, 100_000_000_000_000]},
        index=["005930", "000660"],
    )
    monkeypatch.setattr(recommend_mod, "_fetch_market_cap", lambda *a: cap_df)

    out = recommend_mod.compute_kospi200_scored(AS_OF)

    by_code = {m.code: m for m in out.items}
    assert by_code["005930"].price is None
    assert by_code["000660"].price == 143_000  # 정상 종가는 그대로 채워진다


def test_opendart_failure_falls_back_to_neutral_without_raising(monkeypatch):
    """DataSourceError(외부 장애)는 삼키고 중립 처리한다(Task 7 — except DataSourceError)."""
    def _boom(members, as_of):
        raise SourceUnavailableError("dart", "OpenDART 장애")

    _patch_common(monkeypatch, qmetrics_side_effect=_boom)

    out = recommend_mod.compute_kospi200_scored(AS_OF)

    assert out.count == 2
    for m in out.items:
        assert m.name in ("삼성전자", "SK하이닉스")
        # OpenDART 실패로 quality 원천 컬럼이 아예 없었으므로 score_quality 는 중립(0)
        # 혹은 결측 처리되며, 최소한 예외 없이 항목이 만들어진다는 것만 검증한다.
        assert m.code in ("005930", "000660")


def test_opendart_bug_propagates(monkeypatch):
    """우리 쪽 버그(TypeError)는 삼키지 않고 전파된다(Task 7 — 좁힌 except 의 핵심 계약)."""
    def _bug(members, as_of):
        raise TypeError("잘못된 인자")

    _patch_common(monkeypatch, qmetrics_side_effect=_bug)

    with pytest.raises(TypeError):
        recommend_mod.compute_kospi200_scored(AS_OF)


def test_opendart_success_populates_items_without_error(monkeypatch):
    _patch_common(
        monkeypatch,
        qmetrics_side_effect=lambda members, as_of: {
            "005930": {"roe": 12.0, "debt_ratio": 30.0, "fcf": 1e12, "f_score": 7,
                       "op_growth": 5.0, "net_growth": 4.0, "turnaround": False},
            "000660": {"roe": 8.0, "debt_ratio": 45.0, "fcf": 5e11, "f_score": 5,
                       "op_growth": 2.0, "net_growth": 1.0, "turnaround": False},
        },
    )

    out = recommend_mod.compute_kospi200_scored(AS_OF)

    assert out.count == 2
    assert out.opendart_enabled == opendart.is_enabled()
    # 시가총액 내림차순 기본 정렬
    assert [m.code for m in out.items] == ["005930", "000660"]
