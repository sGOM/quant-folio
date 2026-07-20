"""engine/risk.py::evaluate_buy/evaluate_sell/check_stop_loss/_aggregate_position 검증.

`test_per_strategy_risk.py`는 `check_daily_loss_limit`/`_daily_pnl`만 정밀
검증하고, 실제 주문 수량·청산 여부를 결정하는 이 세 함수는(§신규발굴 8차
1위, 재조사로 확신도 상향 — grep 상 직접 호출 테스트 0건 확인) 경계값이
전혀 검증돼 있지 않았다: `max_position_size` 잔여한도 소진 시 즉시 거부,
가용 현금이 1주 미만일 때 거부, `evaluate_sell`의 전략 스코프 vs 계좌
합산 스코프 분기, `check_stop_loss`의 RiskLimit 없음/`avg_price<=0` 방어,
`_aggregate_position`의 여러 전략 동일 종목 수량가중 평균 계산.

`test_per_strategy_risk.py`의 픽스처 패턴(FakeDB·store 헬퍼)을 재사용한다.
이 세 함수는 db.execute(JOIN)를 쓰지 않고 scalar/scalars 만 쓰므로
conftest 의 기본 FakeDB 로 충분하다(`_RiskFakeDB` 불필요).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import Position, RiskLimit
from engine import risk
from tests.conftest import FakeDB, _Store

_USER = 1
_A = 101
_B = 202
_SYMBOL = "005930"


@pytest.fixture
def store():
    return _Store()


@pytest.fixture
def db(store):
    return FakeDB(store)


def _add(store: _Store, obj):
    obj.id = store.next_id()
    store.rows.setdefault(type(obj), []).append(obj)
    return obj


def _position(store, *, strategy_id, symbol=_SYMBOL, qty, avg_price):
    return _add(
        store,
        Position(
            user_id=_USER, strategy_id=strategy_id, symbol=symbol,
            qty=Decimal(str(qty)), avg_price=Decimal(str(avg_price)),
        ),
    )


def _limit(store, *, strategy_id, stop_loss_pct=None, max_position_size=None):
    return _add(
        store,
        RiskLimit(
            user_id=_USER, strategy_id=strategy_id,
            stop_loss_pct=None if stop_loss_pct is None else Decimal(str(stop_loss_pct)),
            max_position_size=None if max_position_size is None else Decimal(str(max_position_size)),
        ),
    )


# ─────────────────────────── evaluate_buy ───────────────────────────
async def test_evaluate_buy_rejects_non_positive_price(db):
    decision = await risk.evaluate_buy(db, _USER, _A, _SYMBOL, Decimal("0"), Decimal("1000000"))
    assert decision.approved is False
    assert decision.reason == "유효하지 않은 가격"


async def test_evaluate_buy_no_limit_computes_qty_from_cash(db):
    decision = await risk.evaluate_buy(db, _USER, _A, _SYMBOL, Decimal("70000"), Decimal("1000000"))
    assert decision.approved is True
    assert decision.qty == 14  # 1,000,000 // 70,000


async def test_evaluate_buy_rejects_when_cash_insufficient_for_one_share(db):
    decision = await risk.evaluate_buy(db, _USER, _A, _SYMBOL, Decimal("70000"), Decimal("50000"))
    assert decision.approved is False
    assert decision.reason == "주문 가능 수량 부족"


async def test_evaluate_buy_caps_cash_by_remaining_max_position_size(store, db):
    _limit(store, strategy_id=_A, max_position_size=Decimal("500000"))
    # 이미 (여러 전략 합산) 300,000원어치 보유 → 잔여 한도 200,000원.
    _position(store, strategy_id=_B, qty=3, avg_price=100000)

    decision = await risk.evaluate_buy(db, _USER, _A, _SYMBOL, Decimal("70000"), Decimal("1000000"))

    assert decision.approved is True
    assert decision.qty == 2  # 200,000(잔여한도) // 70,000 = 2 (희망현금 1,000,000 보다 작게 캡)


async def test_evaluate_buy_rejects_when_max_position_size_fully_consumed(store, db):
    _limit(store, strategy_id=_A, max_position_size=Decimal("500000"))
    _position(store, strategy_id=_A, qty=10, avg_price=100000)  # 이미 1,000,000원 보유 > 한도

    decision = await risk.evaluate_buy(db, _USER, _A, _SYMBOL, Decimal("70000"), Decimal("1000000"))

    assert decision.approved is False
    assert decision.reason == "최대 포지션 한도 도달"


# ─────────────────────────── evaluate_sell ───────────────────────────
async def test_evaluate_sell_strategy_scope_returns_qty_for_that_strategy_only(store, db):
    _position(store, strategy_id=_A, qty=7, avg_price=70000)
    _position(store, strategy_id=_B, qty=3, avg_price=70000)  # 다른 전략 보유분(무시돼야 함)

    decision = await risk.evaluate_sell(db, _USER, _SYMBOL, strategy_id=_A)

    assert decision.approved is True
    assert decision.qty == 7  # B 의 3주는 섞이지 않는다.


async def test_evaluate_sell_strategy_scope_rejects_when_no_position(db):
    decision = await risk.evaluate_sell(db, _USER, _SYMBOL, strategy_id=_A)
    assert decision.approved is False
    assert decision.reason == "보유 수량 없음"


async def test_evaluate_sell_account_scope_aggregates_across_strategies(store, db):
    _position(store, strategy_id=_A, qty=7, avg_price=70000)
    _position(store, strategy_id=_B, qty=3, avg_price=70000)

    decision = await risk.evaluate_sell(db, _USER, _SYMBOL, strategy_id=None)

    assert decision.approved is True
    assert decision.qty == 10  # 계좌 합산 스코프는 두 전략을 합친다.


async def test_evaluate_sell_account_scope_rejects_when_none_held(db):
    decision = await risk.evaluate_sell(db, _USER, _SYMBOL, strategy_id=None)
    assert decision.approved is False
    assert decision.reason == "보유 수량 없음"


# ─────────────────────────── check_stop_loss ───────────────────────────
async def test_check_stop_loss_true_when_drop_meets_threshold(store, db):
    _limit(store, strategy_id=_A, stop_loss_pct=Decimal("0.1"))
    _position(store, strategy_id=_A, qty=10, avg_price=100000)

    assert await risk.check_stop_loss(db, _USER, _A, _SYMBOL, Decimal("89000")) is True  # -11%
    assert await risk.check_stop_loss(db, _USER, _A, _SYMBOL, Decimal("91000")) is False  # -9%


async def test_check_stop_loss_false_without_risk_limit(store, db):
    _position(store, strategy_id=_A, qty=10, avg_price=100000)
    assert await risk.check_stop_loss(db, _USER, _A, _SYMBOL, Decimal("1")) is False


async def test_check_stop_loss_false_when_stop_loss_pct_not_set(store, db):
    _limit(store, strategy_id=_A, max_position_size=Decimal("500000"))  # stop_loss_pct 는 미설정
    _position(store, strategy_id=_A, qty=10, avg_price=100000)
    assert await risk.check_stop_loss(db, _USER, _A, _SYMBOL, Decimal("1")) is False


async def test_check_stop_loss_false_for_zero_avg_price(store, db):
    """평단가 0(방어적 데이터) 인 포지션은 division-by-zero 대신 False 로 안전 처리."""
    _limit(store, strategy_id=_A, stop_loss_pct=Decimal("0.1"))
    _position(store, strategy_id=_A, qty=10, avg_price=0)
    assert await risk.check_stop_loss(db, _USER, _A, _SYMBOL, Decimal("100")) is False


# ─────────────────────────── _aggregate_position ───────────────────────────
async def test_aggregate_position_weights_average_price_by_quantity(store, db):
    _position(store, strategy_id=_A, qty=10, avg_price=100000)
    _position(store, strategy_id=_B, qty=30, avg_price=80000)

    agg = await risk._aggregate_position(db, _USER, _SYMBOL)

    assert agg.qty == Decimal("40")
    # 수량가중평균 = (10*100000 + 30*80000) / 40 = 85000
    assert agg.avg_price == Decimal("85000")


async def test_aggregate_position_none_when_no_rows_or_zero_total_qty(store, db):
    assert await risk._aggregate_position(db, _USER, _SYMBOL) is None
    _position(store, strategy_id=_A, qty=0, avg_price=100000)
    assert await risk._aggregate_position(db, _USER, _SYMBOL) is None
