"""장 운영시간/휴장일 판단 검증."""
from datetime import datetime

from app.services import market
from app.services.market import KST, is_market_open


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
