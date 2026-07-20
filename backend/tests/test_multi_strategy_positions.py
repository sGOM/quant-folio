"""다중 전략 포지션 분리 — record_fill 이 (user_id, strategy_id, symbol) 스코프로
포지션을 독립 기록·갱신하는지 검증한다.

같은 종목을 두 전략이 동시에 매매해도 포지션이 섞이지 않아야 한다:
- 매수 시 전략별로 별도 Position 행 생성
- 매도 시 자기 전략 Position 만 감소(다른 전략 불변)
- avg_price 가 전략별 독립 계산(한 전략 추가매수가 다른 전략 평단에 영향 없음)

record_fill 을 직접 호출하는 단위테스트. 실제 DB 없이 where 절을 introspect 하는
인메모리 세션 대역으로 (user_id, strategy_id, symbol) 필터링을 재현한다.
"""
import json
from decimal import Decimal

from app.models import Execution, Order, OrderSide, Position
from engine.fills import record_fill
from tests.conftest import FakeRedis


def _eq_filters(stmt) -> dict:
    """select(...).where(col==val, ...) 의 등가 조건을 {컬럼명: 값} 으로 뽑아낸다."""
    clause = stmt.whereclause
    clauses = getattr(clause, "clauses", [clause])
    out = {}
    for c in clauses:
        out[c.left.name] = c.right.value
    return out


class _FakeSession:
    """record_fill 이 쓰는 add()/scalar(select(Position).where(...)) 만 지원하는 대역."""

    def __init__(self):
        self.positions: list[Position] = []
        self.executions: list[Execution] = []

    def add(self, obj):
        if isinstance(obj, Position):
            self.positions.append(obj)
        elif isinstance(obj, Execution):
            self.executions.append(obj)

    async def scalar(self, stmt):
        f = _eq_filters(stmt)
        for p in self.positions:
            if (
                p.user_id == f.get("user_id")
                and p.strategy_id == f.get("strategy_id")
                and p.symbol == f.get("symbol")
            ):
                return p
        return None

    def position(self, strategy_id, symbol) -> Position | None:
        for p in self.positions:
            if p.strategy_id == strategy_id and p.symbol == symbol:
                return p
        return None


def _order(strategy_id: int, symbol: str, side: OrderSide, order_id: int = 1) -> Order:
    return Order(
        id=order_id,
        user_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
    )


async def test_two_strategies_same_symbol_get_separate_positions():
    """두 전략이 같은 종목을 매수하면 (user, A, sym)·(user, B, sym) 로 분리 기록된다."""
    db = _FakeSession()
    sym = "005930"

    await record_fill(db, _order(10, sym, OrderSide.BUY), 10, Decimal("1000"), fully_filled=True)
    await record_fill(db, _order(20, sym, OrderSide.BUY), 5, Decimal("2000"), fully_filled=True)

    assert len(db.positions) == 2
    a = db.position(10, sym)
    b = db.position(20, sym)
    assert a is not None and b is not None
    assert a.qty == Decimal("10") and a.avg_price == Decimal("1000")
    assert b.qty == Decimal("5") and b.avg_price == Decimal("2000")
    # 체결도 전략별 strategy_id 로 비정규화되어 기록된다.
    assert {e.strategy_id for e in db.executions} == {10, 20}


async def test_sell_only_reduces_own_strategy_position():
    """한 전략의 매도는 자기 포지션만 줄이고 다른 전략 포지션은 건드리지 않는다."""
    db = _FakeSession()
    sym = "005930"
    await record_fill(db, _order(10, sym, OrderSide.BUY), 10, Decimal("1000"), fully_filled=True)
    await record_fill(db, _order(20, sym, OrderSide.BUY), 5, Decimal("2000"), fully_filled=True)

    # 전략 10 이 전량 매도.
    await record_fill(db, _order(10, sym, OrderSide.SELL), 10, Decimal("1100"), fully_filled=True)

    a = db.position(10, sym)
    b = db.position(20, sym)
    assert a.qty == Decimal("0")  # 자기 포지션만 청산
    assert a.avg_price == Decimal("0")
    assert b.qty == Decimal("5")  # 다른 전략은 불변
    assert b.avg_price == Decimal("2000")


async def test_avg_price_is_independent_per_strategy():
    """한 전략만 추가매수해도 다른 전략의 평균단가는 변하지 않는다."""
    db = _FakeSession()
    sym = "005930"
    await record_fill(db, _order(10, sym, OrderSide.BUY), 10, Decimal("1000"), fully_filled=True)
    await record_fill(db, _order(20, sym, OrderSide.BUY), 5, Decimal("2000"), fully_filled=True)

    # 전략 10 만 10주 @2000 추가매수 → 평단 (10*1000 + 10*2000)/20 = 1500.
    await record_fill(db, _order(10, sym, OrderSide.BUY), 10, Decimal("2000"), fully_filled=True)

    a = db.position(10, sym)
    b = db.position(20, sym)
    assert a.qty == Decimal("20")
    assert a.avg_price == Decimal("1500")
    assert b.qty == Decimal("5")
    assert b.avg_price == Decimal("2000")  # 다른 전략 평단 불변


async def test_oversell_clamps_to_zero_and_warns_via_alert():
    """매도 체결이 보유수량을 초과하면 0으로 클램프하고(방어) warning 알림을 발행한다
    (§신규발굴 5~8차 이월 후보 — 이전엔 무경보였다).
    """
    db = _FakeSession()
    redis = FakeRedis()
    sym = "005930"
    await record_fill(db, _order(10, sym, OrderSide.BUY), 10, Decimal("1000"), fully_filled=True)

    # 보유 10주인데 15주 매도 체결이 들어온 상황(정합 경쟁·이중 체결 의심).
    await record_fill(
        db, _order(10, sym, OrderSide.SELL, order_id=2), 15, Decimal("1100"),
        fully_filled=True, redis=redis,
    )

    a = db.position(10, sym)
    assert a.qty == Decimal("0")  # 음수로 내려가지 않고 0에서 클램프
    assert a.avg_price == Decimal("0")

    alerts = [
        json.loads(data) for _, data in redis.published
        if json.loads(data).get("code") == "oversell_clamped"
    ]
    assert len(alerts) >= 1
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["strategy_id"] == 10


async def test_normal_sell_within_holding_does_not_alert():
    db = _FakeSession()
    redis = FakeRedis()
    sym = "005930"
    await record_fill(db, _order(10, sym, OrderSide.BUY), 10, Decimal("1000"), fully_filled=True)

    await record_fill(
        db, _order(10, sym, OrderSide.SELL, order_id=2), 10, Decimal("1100"),
        fully_filled=True, redis=redis,
    )

    alerts = [
        json.loads(data) for _, data in redis.published
        if json.loads(data).get("code") == "oversell_clamped"
    ]
    assert alerts == []
