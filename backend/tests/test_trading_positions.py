"""`/api/trading/positions` — 브로커 잔고 우선, 실패 시에만 로컬 DB 그림자로 폴백."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.routes import trading
from app.services.broker import BrokerError
from tests.conftest import FakeBroker

USER = SimpleNamespace(id=7)


class _DB:
    """`_db_positions` 가 쓰는 `scalars` 만 흉내낸다(그림자 포지션 1건)."""

    async def scalars(self, _stmt):
        return [SimpleNamespace(symbol="000660", qty=Decimal("3"), avg_price=Decimal("120000"))]


def _dump(rows):
    return [r.model_dump() for r in rows]


SHADOW = [{"symbol": "000660", "qty": 3.0, "avg_price": 120000.0}]


@pytest.mark.asyncio
async def test_브로커_잔고가_신뢰_소스다(monkeypatch):
    broker = FakeBroker(balance_positions=[
        {"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "70000.5"},
        {"pdno": "035720", "hldg_qty": "0", "pchs_avg_pric": "50000"},  # 수량 0 은 제외
        {"pdno": "000270", "hldg_qty": "2", "pchs_avg_pric": ""},  # 평단가 결측
    ])
    monkeypatch.setattr(trading, "make_broker_for_user", lambda _u: broker)

    out = await trading.list_positions(current=USER, db=_DB())

    # DB 그림자(000660)는 섞이지 않는다 — 브로커가 답하면 브로커만 본다.
    # 평단가 결측은 0 sentinel 이 아니라 None(프론트가 "-" 로 렌더).
    assert _dump(out) == [
        {"symbol": "005930", "qty": 10.0, "avg_price": 70000.5},
        {"symbol": "000270", "qty": 2.0, "avg_price": None},
    ]


@pytest.mark.asyncio
async def test_자격증명_미등록이면_DB_그림자로_폴백(monkeypatch):
    def _raise(_u):
        raise BrokerError("자격증명 없음")

    monkeypatch.setattr(trading, "make_broker_for_user", _raise)

    assert _dump(await trading.list_positions(current=USER, db=_DB())) == SHADOW


@pytest.mark.asyncio
async def test_잔고_조회_실패면_DB_그림자로_폴백(monkeypatch):
    class _Down:
        async def get_balance(self):
            raise BrokerError("KIS 500")

    monkeypatch.setattr(trading, "make_broker_for_user", lambda _u: _Down())

    assert _dump(await trading.list_positions(current=USER, db=_DB())) == SHADOW


@pytest.mark.asyncio
async def test_BrokerError_가_아닌_예외는_삼키지_않는다(monkeypatch):
    class _Bug:
        async def get_balance(self):
            raise RuntimeError("버그")

    monkeypatch.setattr(trading, "make_broker_for_user", lambda _u: _Bug())

    with pytest.raises(RuntimeError):
        await trading.list_positions(current=USER, db=_DB())
