"""수급(flow) 팩터 배선 검증(외국인+기관 누적 순매수/정규화 분모).

- _compute_stock_scores 의 flow 카테고리가 순매수 지속(flow_norm↑)을 상위로 반영하는지.
- flow_norm 컬럼이 없으면 score_flow 가 전부 0 → 기존 전략 무영향(하위호환).
- flow 가중치 0 이면 flow_norm 이 있어도 종합점수 불변.
- _fetch_net_purchases 가 (시장×투자자) 순매수거래대금을 합산하고 부분 실패를 중립
  폴백하며 전량 실패 시 빈 프레임을 반환하는지(네트워크 없이 monkeypatch).
- compute_flow_norm 정규화(mcap/value)와 전량실패 빈 Series 반환.
- compute_universe_scores 가 flow 가중치>0·전량 조회 실패 시 리밸런싱 스킵(예외)하는지.
"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import metrics
from app.services.metrics import factors, fetch


# ───────────────────── 스코어러(flow 카테고리) ─────────────────────


def test_flow_ranks_high_net_purchase_top():
    """flow_norm 이 클수록(순매수 지속) 상위 랭킹. 모멘텀은 중립으로 둬 flow 만 변별."""
    df = pd.DataFrame(index=["A", "B", "C", "D"])
    df["mom_6m"] = [0.1, 0.1, 0.1, 0.1]
    df["flow_norm"] = [0.06, 0.02, -0.01, -0.05]  # A 최대 순매수, D 최대 순매도
    scored = metrics._compute_stock_scores(df, weights={
        "momentum": 0.0, "value": 0.0, "lowvol": 0.0,
        "quality": 0.0, "growth": 0.0, "flow": 1.0,
    })
    f = scored["score_flow"]
    assert f["A"] > f["B"] > f["C"] > f["D"]
    assert scored["score"]["A"] == scored["score"].max()


def test_flow_absent_column_neutral():
    """flow_norm 컬럼이 없으면 score_flow 전부 0 → 기존 전략 무영향."""
    df = pd.DataFrame(index=["A", "B", "C"])
    df["mom_6m"] = [0.1, 0.2, 0.3]
    scored = metrics._compute_stock_scores(df)  # 기본(flow=0)
    assert (scored["score_flow"] == 0.0).all()
    assert scored["score"]["C"] == scored["score"].max()


def test_flow_zero_weight_does_not_change_score():
    """flow 가중치 0 이면 flow_norm 이 있어도 종합점수에 반영되지 않는다."""
    df = pd.DataFrame(index=["A", "B", "C", "D"])
    df["mom_6m"] = [0.30, 0.20, 0.10, 0.05]   # 모멘텀: A>B>C>D
    df["flow_norm"] = [-0.05, -0.01, 0.02, 0.06]  # 수급은 반대(D 최상)
    scored = metrics._compute_stock_scores(df, weights={
        "momentum": 1.0, "value": 0.0, "lowvol": 0.0,
        "quality": 0.0, "growth": 0.0, "flow": 0.0,
    })
    assert scored["score"]["A"] == scored["score"].max()


# ───────────────────── _fetch_net_purchases (pykrx 합산·폴백) ─────────────────────


class _FakeStock:
    """get_market_net_purchases_of_equities 만 흉내내는 페이크 pykrx.stock."""

    def __init__(self, table):
        # table: {(market, investor): DataFrame|Exception}
        self.table = table

    def get_market_net_purchases_of_equities(self, fromdate, todate, market, investor):
        v = self.table.get((market, investor))
        if isinstance(v, Exception):
            raise v
        return v


def _np_df(rows):
    """티커 인덱스 + 순매수거래대금 컬럼 DataFrame."""
    return pd.DataFrame({"순매수거래대금": rows})


def test_fetch_net_purchases_sums_markets_and_investors(monkeypatch):
    """외국인+기관합계를 종목별로 합산한다(시장 교차 합산 포함)."""
    table = {
        ("KOSPI", "외국인"): _np_df({"005930": 100.0, "000660": 50.0}),
        ("KOSPI", "기관합계"): _np_df({"005930": 30.0, "000660": -10.0}),
        ("KOSDAQ", "외국인"): _np_df({"247540": 20.0}),
        ("KOSDAQ", "기관합계"): _np_df({"247540": 5.0}),
    }
    monkeypatch.setattr(fetch, "_pykrx_stock", lambda: _FakeStock(table))
    out = fetch._fetch_net_purchases("20240101", "20240401", ["KOSPI", "KOSDAQ"])
    assert out.loc["005930", "net_buy_value"] == 130.0
    assert out.loc["000660", "net_buy_value"] == 40.0
    assert out.loc["247540", "net_buy_value"] == 25.0


def test_fetch_net_purchases_partial_failure_is_skipped(monkeypatch):
    """개별 (시장,투자자) 실패는 건너뛰고 나머지로 진행한다(중립 폴백)."""
    table = {
        ("KOSPI", "외국인"): _np_df({"005930": 100.0}),
        ("KOSPI", "기관합계"): RuntimeError("pykrx 일시 장애"),
    }
    monkeypatch.setattr(fetch, "_pykrx_stock", lambda: _FakeStock(table))
    out = fetch._fetch_net_purchases("20240101", "20240401", ["KOSPI"])
    assert out.loc["005930", "net_buy_value"] == 100.0  # 외국인분만 반영


def test_fetch_net_purchases_all_fail_returns_empty(monkeypatch):
    """전량 실패면 빈 프레임 반환(호출자가 리밸런싱 스킵 판단)."""
    table = {
        ("KOSPI", "외국인"): RuntimeError("x"),
        ("KOSPI", "기관합계"): RuntimeError("x"),
    }
    monkeypatch.setattr(fetch, "_pykrx_stock", lambda: _FakeStock(table))
    out = fetch._fetch_net_purchases("20240101", "20240401", ["KOSPI"])
    assert out.empty
    assert list(out.columns) == ["net_buy_value"]


# ───────────────────── compute_flow_norm (정규화) ─────────────────────


def test_compute_flow_norm_mcap(monkeypatch):
    """flow_norm = 누적 순매수 / 시가총액(denom=mcap)."""
    monkeypatch.setattr(
        factors, "_fetch_net_purchases",
        lambda s, e, m: pd.DataFrame({"net_buy_value": {"005930": 1e11, "000660": -2e10}}),
    )
    monkeypatch.setattr(
        factors, "_fetch_market_cap",
        lambda ymd, m: pd.DataFrame({"시가총액": {"005930": 1e13, "000660": 2e12}}),
    )
    out = factors.compute_flow_norm(["005930", "000660"], date(2024, 4, 1), window=90, denom="mcap")
    assert out["005930"] == pytest.approx(0.01)
    assert out["000660"] == pytest.approx(-0.01)


def test_compute_flow_norm_value_denominator(monkeypatch):
    """denom=value 는 동일창 누적 거래대금으로 정규화한다."""
    monkeypatch.setattr(
        factors, "_fetch_net_purchases",
        lambda s, e, m: pd.DataFrame({"net_buy_value": {"005930": 5e10}}),
    )
    monkeypatch.setattr(
        factors, "_fetch_price_change",
        lambda s, e, m: pd.DataFrame({"거래대금": {"005930": 1e12}}),
    )
    out = factors.compute_flow_norm(["005930"], date(2024, 4, 1), window=90, denom="value")
    assert out["005930"] == pytest.approx(0.05)


def test_compute_flow_norm_empty_when_all_fail(monkeypatch):
    """순매수 전량 실패면 빈 Series(호출자 스킵 판단)."""
    monkeypatch.setattr(
        factors, "_fetch_net_purchases",
        lambda s, e, m: pd.DataFrame(columns=["net_buy_value"]),
    )
    out = factors.compute_flow_norm(["005930"], date(2024, 4, 1))
    assert out.empty


def test_compute_flow_norm_skips_nonpositive_denominator(monkeypatch):
    """분모(시총) 결측·0 이하 종목은 제외한다(0 나눗셈 방지)."""
    monkeypatch.setattr(
        factors, "_fetch_net_purchases",
        lambda s, e, m: pd.DataFrame({"net_buy_value": {"005930": 1e11, "000660": 1e11}}),
    )
    monkeypatch.setattr(
        factors, "_fetch_market_cap",
        lambda ymd, m: pd.DataFrame({"시가총액": {"005930": 1e13, "000660": 0.0}}),
    )
    out = factors.compute_flow_norm(["005930", "000660"], date(2024, 4, 1), denom="mcap")
    assert "005930" in out.index and "000660" not in out.index


# ───────────────────── compute_universe_scores 전량실패 방어 ─────────────────────


def test_universe_scores_skips_when_flow_all_fail(monkeypatch):
    """flow 가중치>0 인데 순매수 전량 실패면 리밸런싱을 스킵(예외)한다."""
    # 가격·펀더멘털·기술지표는 정상(빈 결과 허용)이라 모멘텀 전량실패 방어엔 안 걸리도록
    # 모멘텀 가중치 0 인 flow-only 가중치를 쓴다.
    monkeypatch.setattr(factors, "_fetch_fundamentals", lambda ymd, m: pd.DataFrame())
    monkeypatch.setattr(
        factors, "_fetch_price_change",
        lambda s, e, m: pd.DataFrame({"등락률": {"005930": 1.0}}),
    )
    monkeypatch.setattr(
        "app.services.metrics.stocks._compute_tech_indicators",
        lambda code, hs, ae: {},
    )
    # flow 전량 실패.
    monkeypatch.setattr(
        factors, "compute_flow_norm",
        lambda codes, as_of, window=90, denom="mcap": pd.Series(dtype="float64"),
    )
    with pytest.raises(RuntimeError, match="수급"):
        metrics.compute_universe_scores(
            ["005930"], date(2024, 4, 1),
            factor_weights={"momentum": 0.0, "value": 0.0, "lowvol": 0.0,
                            "quality": 0.0, "growth": 0.0, "flow": 1.0},
        )


def test_flow_full_fail_defense_matches_default_weights_dict():
    """DEFAULT_FACTOR_WEIGHTS 에 flow 키가 있어 스코어러 합성에서 KeyError 가 없다."""
    assert "flow" in factors.DEFAULT_FACTOR_WEIGHTS
    assert factors.DEFAULT_FACTOR_WEIGHTS["flow"] == 0.0
