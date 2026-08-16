"""장 운영시간/휴장일 판단 검증."""
from datetime import date, datetime

from app.services import market
from app.services.market import KST, estimated_trading_days, is_market_open


def test_weekend_closed():
    # 2024-06-22 토요일
    sat = datetime(2024, 6, 22, 10, 0, tzinfo=KST)
    assert is_market_open(sat) is False


def test_weekday_hours(monkeypatch):
    monkeypatch.setattr(market, "is_business_day", lambda d: True)
    # 2024-06-21 금요일
    assert is_market_open(datetime(2024, 6, 21, 10, 0, tzinfo=KST)) is True
    assert is_market_open(datetime(2024, 6, 21, 8, 59, tzinfo=KST)) is False
    assert is_market_open(datetime(2024, 6, 21, 15, 31, tzinfo=KST)) is False


# ───── estimated_trading_days: pykrx 호출 없는 근사 거래일 수 ─────


def test_estimated_trading_days_1년_구간은_약_240_250():
    days = estimated_trading_days(date(2024, 1, 1), date(2024, 12, 31))
    assert 240 <= days <= 250


def test_estimated_trading_days_1주_구간은_약_3_5():
    # 2024-06-17(월) ~ 2024-06-23(일) — 평일 5일
    days = estimated_trading_days(date(2024, 6, 17), date(2024, 6, 23))
    assert 3 <= days <= 5


def test_estimated_trading_days_역구간은_0이다():
    assert estimated_trading_days(date(2024, 6, 23), date(2024, 6, 17)) == 0.0
