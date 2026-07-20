"""engine/reconcile.py::reconcile_open_orders 의 브로커 생성 실패 계측 검증(§23 후속).

이전엔 사용자별 브로커 생성이 실패(BrokerError, 예: 자격증명 만료)해도 로그만
남기고 stats 어디에도 반영되지 않아, engine/main.py::_reconcile_loop 가 이런
지속 실패를 알림으로 띄우지 못했다. Order/User 조회는 reconcile.py 의 정확한
WHERE 절 해석이 이 테스트의 목적이 아니므로, 결과를 직접 주입하는 최소 대역만
쓴다(test_fill_quality_route.py 와 같은 관례).
"""
from app.models import Order, OrderSide, OrderStatus, User
from app.services.broker import BrokerError
from engine import reconcile
from tests.conftest import FakeRedis


def _order(id_: int, user_id: int, symbol: str = "005930") -> Order:
    o = Order(
        user_id=user_id, symbol=symbol, side=OrderSide.BUY,
        qty=10, price=None, status=OrderStatus.SUBMITTED,
    )
    o.id = id_
    o.kis_order_id = f"KIS{id_}"
    return o


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """reconcile_open_orders 가 쓰는 db.scalars(Order 목록)/db.scalar(User) 만 대역한다."""

    def __init__(self, orders: list[Order], user: User):
        self._orders = orders
        self._user = user

    async def scalars(self, _stmt):
        return _FakeScalars(self._orders)

    async def scalar(self, stmt):
        # 사용자 조회(User)만 유저를 돌려주고, 그 외(executions 합 등)는 미기록 취급(0/None).
        entity = stmt.column_descriptions[0].get("entity")
        if entity is User:
            return self._user
        return None


async def test_broker_creation_failure_counted_as_error(monkeypatch):
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    session = _FakeSession([_order(1, user_id=1)], user)
    redis = FakeRedis()

    def _boom(_user):
        raise BrokerError("자격증명 만료")

    monkeypatch.setattr(reconcile, "make_broker_for_user", _boom)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert stats["errors"] == 1
    # 브로커가 없어 해당 주문은 체결조회 자체를 시도하지 못했다(checked 는 조회 성공 이후에만 증가).
    assert stats["checked"] == 0
    assert stats["filled"] == 0 and stats["partial"] == 0


async def test_broker_available_processes_normally_without_error(monkeypatch):
    """대조군 — 브로커 생성이 정상이면 errors 가 늘지 않는다(get_order_execution 자체는 대역 밖)."""
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    session = _FakeSession([_order(1, user_id=1)], user)
    redis = FakeRedis()

    class _FakeBroker:
        async def get_order_execution(self, *_a, **_k):
            from app.services.broker.base import Fill

            return Fill(filled_qty=0, avg_price=None, fully_filled=False)

        async def get_balance(self):
            from app.services.broker.base import Balance

            return Balance(positions=[])

    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: _FakeBroker())

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert stats["errors"] == 0
    assert stats["checked"] == 1
