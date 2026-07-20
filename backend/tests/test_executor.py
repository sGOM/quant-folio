"""주문 멱등성 키 결정성 + execute_signal 3중 멱등 방어·거부 경로 검증.

키 결정성 테스트는 기존부터 있었으나, engine/executor.py::execute_signal 자체
— 모든 러너(StrategyRunner·RebalanceRunner)가 공유하는 단일 주문 진입점 —
는 정상 체결 경로만 test_engine_e2e.py/test_strategy_runner.py 로 간접
검증됐을 뿐, 이중 매수/매도를 막는 핵심 방어선인 3중 멱등 방어(Redis 락
경합·DB idempotency_key 기존 주문·IntegrityError UNIQUE 충돌 흡수)와
BrokerError 발생 시 REJECTED 기록·이벤트 발행 경로는 직접 검증된 적이
없었다(§신규발굴 7차 1위). test_engine_e2e.py 와 동일한 FakeDB(`_Store`)·
FakeBroker·FakeRedis 를 쓰되, execute_signal 은 세션을 인자로 직접 받으므로
(컨텍스트 매니저 아님) 팩토리 패치 없이 FakeDB 인스턴스를 바로 넘긴다.
"""
import json
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.core.channels import ORDER_LOCK_PREFIX
from app.models import Execution, Order, OrderStatus, Position
from app.services.broker import BrokerError
from engine.executor import execute_signal, make_idempotency_key
from tests.conftest import FakeBroker, FakeDB, FakeRedis, _Store

_SYMBOL = "005930"
_USER_ID = 1
_STRATEGY_ID = 101


def test_idempotency_key_is_deterministic():
    k1 = make_idempotency_key(1, "005930", "buy", "2024-06-21")
    k2 = make_idempotency_key(1, "005930", "buy", "2024-06-21")
    assert k1 == k2


def test_idempotency_key_varies_by_input():
    base = make_idempotency_key(1, "005930", "buy", "2024-06-21")
    assert base != make_idempotency_key(2, "005930", "buy", "2024-06-21")
    assert base != make_idempotency_key(1, "000660", "buy", "2024-06-21")
    assert base != make_idempotency_key(1, "005930", "sell", "2024-06-21")
    assert base != make_idempotency_key(1, "005930", "buy", "2024-06-22")


def _key(side: str = "buy", bar_ts: str = "2026-07-21") -> str:
    return make_idempotency_key(_STRATEGY_ID, _SYMBOL, side, bar_ts)


async def test_full_fill_records_order_execution_and_position():
    store = _Store()
    db = FakeDB(store)
    redis = FakeRedis()
    broker = FakeBroker({_SYMBOL: 70_000})

    order = await execute_signal(
        db, redis, broker, user_id=_USER_ID, strategy_id=_STRATEGY_ID, symbol=_SYMBOL,
        side="buy", qty=10, price=Decimal("70000"), idempotency_key=_key(),
        reason="테스트 매수",
    )

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert len(store.all(Order)) == 1
    assert len(store.all(Execution)) == 1
    positions = store.all(Position)
    assert len(positions) == 1 and positions[0].qty == Decimal(10)
    assert any(json.loads(data).get("type") == "execution" for _, data in redis.published)


async def test_redis_lock_contention_returns_none_without_db_write():
    store = _Store()
    db = FakeDB(store)
    redis = FakeRedis()
    broker = FakeBroker({_SYMBOL: 70_000})
    key = _key()
    await redis.set(f"{ORDER_LOCK_PREFIX}{key}", "1")  # 다른 프로세스가 이미 락 보유

    order = await execute_signal(
        db, redis, broker, user_id=_USER_ID, strategy_id=_STRATEGY_ID, symbol=_SYMBOL,
        side="buy", qty=10, price=Decimal("70000"), idempotency_key=key,
    )

    assert order is None
    assert store.all(Order) == []
    assert broker.orders == {}


async def test_existing_idempotency_key_in_db_returns_none():
    store = _Store()
    key = _key()
    existing = Order(
        user_id=_USER_ID, strategy_id=_STRATEGY_ID, symbol=_SYMBOL, side="buy",
        qty=Decimal(10), price=Decimal("70000"), order_type="market",
        status=OrderStatus.FILLED, idempotency_key=key,
    )
    existing.id = 1
    store.rows[Order] = [existing]
    db = FakeDB(store)
    redis = FakeRedis()
    broker = FakeBroker({_SYMBOL: 70_000})

    order = await execute_signal(
        db, redis, broker, user_id=_USER_ID, strategy_id=_STRATEGY_ID, symbol=_SYMBOL,
        side="buy", qty=10, price=Decimal("70000"), idempotency_key=key,
    )

    assert order is None
    assert len(store.all(Order)) == 1  # 새 주문이 추가되지 않았다.
    assert broker.orders == {}  # 증권사에 주문 접수 자체를 시도하지 않았다.


async def test_integrity_error_on_commit_is_absorbed_and_returns_none():
    """DB UNIQUE 제약(최종 방어선) 충돌 — 락·존재확인 사이 경합을 흡수한다."""
    store = _Store()

    class _RaceDB(FakeDB):
        def __init__(self, store):
            super().__init__(store)
            self._raised = False

        async def commit(self):
            if not self._raised:
                self._raised = True
                raise IntegrityError("insert", {}, Exception("unique violation: idempotency_key"))
            return await super().commit()

    db = _RaceDB(store)
    redis = FakeRedis()
    broker = FakeBroker({_SYMBOL: 70_000})

    order = await execute_signal(
        db, redis, broker, user_id=_USER_ID, strategy_id=_STRATEGY_ID, symbol=_SYMBOL,
        side="buy", qty=10, price=Decimal("70000"), idempotency_key=_key(),
    )

    assert order is None
    assert broker.orders == {}  # commit 실패로 증권사 주문 접수 전에 흡수됐다.


async def test_broker_rejection_marks_order_rejected_and_publishes_event():
    class _RejectingBroker(FakeBroker):
        async def place_order(self, *a, **k):
            raise BrokerError("주문 거부: 잔고 부족")

    store = _Store()
    db = FakeDB(store)
    redis = FakeRedis()
    broker = _RejectingBroker({_SYMBOL: 70_000})

    order = await execute_signal(
        db, redis, broker, user_id=_USER_ID, strategy_id=_STRATEGY_ID, symbol=_SYMBOL,
        side="buy", qty=10, price=Decimal("70000"), idempotency_key=_key(),
    )

    assert order is not None
    assert order.status == OrderStatus.REJECTED
    assert store.all(Execution) == []
    assert store.all(Position) == []
    rejected_events = [
        json.loads(data) for _, data in redis.published if json.loads(data).get("status") == "rejected"
    ]
    assert len(rejected_events) >= 1


async def test_zero_fill_keeps_order_submitted_without_execution_or_position():
    store = _Store()
    db = FakeDB(store)
    redis = FakeRedis()
    broker = FakeBroker({_SYMBOL: 70_000}, fill_ratio=0.0)  # 접수만 되고 미체결

    order = await execute_signal(
        db, redis, broker, user_id=_USER_ID, strategy_id=_STRATEGY_ID, symbol=_SYMBOL,
        side="buy", qty=10, price=Decimal("70000"), idempotency_key=_key(),
    )

    assert order is not None
    assert order.status == OrderStatus.SUBMITTED
    assert store.all(Execution) == []
    assert store.all(Position) == []


async def test_execution_query_failure_falls_back_to_submitted_for_reconcile():
    """체결 조회 자체가 실패하면(네트워크 등) 전량 체결로 오인하지 않고 SUBMITTED 로 남긴다."""
    class _UnresolvableBroker(FakeBroker):
        async def get_order_execution(self, order_id, symbol=None):
            raise BrokerError("체결 조회 무응답")

    store = _Store()
    db = FakeDB(store)
    redis = FakeRedis()
    broker = _UnresolvableBroker({_SYMBOL: 70_000})

    order = await execute_signal(
        db, redis, broker, user_id=_USER_ID, strategy_id=_STRATEGY_ID, symbol=_SYMBOL,
        side="buy", qty=10, price=Decimal("70000"), idempotency_key=_key(),
    )

    assert order is not None
    assert order.status == OrderStatus.SUBMITTED
    assert store.all(Execution) == []
