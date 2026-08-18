"""engine/reconcile.py::reconcile_open_orders 검증(§23 후속 + §신규발굴 5차 1위).

이전엔 브로커 생성 실패 계측(§23)만 검증돼 있었고, 이 모듈의 핵심 계약 —
KIS 체결조회로 델타(증분) 계산·전량/부분 판정, 수량은 이미 맞지만 상태만
어긋난 경우의 보정, 모의투자 잔고 폴백(매수 한정·타 주문 소비분 차감),
분산 락으로 인한 중복 처리 방지 — 는 단 1건도 검증되지 않았다. 여기 버그는
실제 포지션·현금 수량 오류로 직결되므로(모듈 docstring이 스스로 "멱등
안전성"을 핵심 계약으로 명시) 추가한다.

Order/User 조회는 reconcile.py 의 정확한 WHERE 절 해석이 목적이 아니므로,
결과를 직접 주입하는 최소 대역만 쓴다(test_fill_quality_route.py 와 같은 관례).
_order_recorded_qty/_symbol_recorded_qty/record_fill 은 각각 별도 관심사(순수
집계 쿼리·공유 체결기록 모듈)이므로 reconcile 네임스페이스에서 대역화해,
reconcile_open_orders/_process_one 자체의 분기 로직만 순수하게 검증한다.
"""
from decimal import Decimal

from app.models import Order, OrderSide, OrderStatus, User
from app.services.broker import Balance, BrokerError, Fill
from engine import reconcile
from tests.conftest import FakeRedis


def _order(
    id_: int, user_id: int, symbol: str = "005930", *,
    side: OrderSide = OrderSide.BUY, qty: int = 10,
    status: OrderStatus = OrderStatus.SUBMITTED,
    price: Decimal | None = Decimal("70000"),
) -> Order:
    o = Order(user_id=user_id, symbol=symbol, side=side, qty=qty, price=price, status=status)
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
        self.commits = 0

    async def scalars(self, _stmt):
        return _FakeScalars(self._orders)

    async def scalar(self, stmt):
        # 사용자 조회(User)만 유저를 돌려주고, 그 외(executions 합 등)는 미기록 취급(0/None).
        entity = stmt.column_descriptions[0].get("entity")
        if entity is User:
            return self._user
        return None

    async def commit(self):
        self.commits += 1


class _FakeBroker:
    """get_order_execution/get_balance 를 시나리오별로 대역화한다."""

    def __init__(self, *, execution=None, execution_error=None, balance_positions=None):
        self._execution = execution
        self._execution_error = execution_error
        self.balance_calls = 0
        self._balance = Balance(positions=balance_positions or [])

    async def get_order_execution(self, order_id, symbol=None):
        if self._execution_error:
            raise self._execution_error
        return self._execution

    async def get_balance(self):
        self.balance_calls += 1
        return self._balance


def _record_fill_spy(monkeypatch):
    """engine.fills.record_fill 을 대역화해 delta/price/fully_filled 인자를 기록한다."""
    calls: list[dict] = []

    async def _fake(db, order, qty, price, *, fully_filled, redis=None):
        calls.append({"order": order, "qty": qty, "price": price, "fully_filled": fully_filled})
        order.status = OrderStatus.FILLED if fully_filled else OrderStatus.PARTIAL

    monkeypatch.setattr(reconcile, "record_fill", _fake)
    return calls


def _patch_recorded_qty(monkeypatch, *, order_qty: int = 0, symbol_qty: int = 0):
    async def _order_qty(db, order_id):
        return order_qty

    async def _symbol_qty(db, uid, symbol):
        return symbol_qty

    monkeypatch.setattr(reconcile, "_order_recorded_qty", _order_qty)
    monkeypatch.setattr(reconcile, "_symbol_recorded_qty", _symbol_qty)


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


async def test_full_fill_applies_delta_and_marks_filled(monkeypatch):
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10)
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    _patch_recorded_qty(monkeypatch, order_qty=0)
    broker = _FakeBroker(execution=Fill(filled_qty=10, avg_price=Decimal("71500"), fully_filled=True))
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert stats == {"checked": 1, "filled": 1, "partial": 0, "errors": 0}
    assert len(calls) == 1
    assert calls[0]["qty"] == 10
    assert calls[0]["price"] == Decimal("71500")
    assert calls[0]["fully_filled"] is True
    assert order.status == OrderStatus.FILLED
    # 사용자 채널로 체결 이벤트가 발행됐는지 확인.
    assert len(redis.published) == 2  # 사용자별 채널 + 하위호환 공용 채널


async def test_partial_fill_applies_incremental_delta_only(monkeypatch):
    """이미 4주가 기록된 상태에서 KIS 가 7주 체결을 보고하면, 증분 3주만 반영한다."""
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10)
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    _patch_recorded_qty(monkeypatch, order_qty=4)
    broker = _FakeBroker(execution=Fill(filled_qty=7, avg_price=Decimal("71500"), fully_filled=False))
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert stats["partial"] == 1 and stats["filled"] == 0
    assert len(calls) == 1
    assert calls[0]["qty"] == 3  # 7 - 4
    assert calls[0]["fully_filled"] is False


async def test_recorded_qty_matches_target_but_status_stale_is_corrected(monkeypatch):
    """수량은 이미 다 기록됐는데(재시도 중복 방지) 상태가 SUBMITTED 로 남아 있으면 보정만 한다."""
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10, status=OrderStatus.SUBMITTED)
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    _patch_recorded_qty(monkeypatch, order_qty=10)
    broker = _FakeBroker(execution=Fill(filled_qty=10, avg_price=Decimal("71500"), fully_filled=True))
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    stats = await reconcile.reconcile_open_orders(session, redis)

    # 증분이 없으므로 record_fill 은 호출되지 않고(중복 기록 방지), 상태만 FILLED 로 보정된다.
    assert calls == []
    assert order.status == OrderStatus.FILLED
    assert stats["filled"] == 0 and stats["partial"] == 0
    assert session.commits == 1


async def test_kis_query_error_falls_back_to_paper_balance_for_buy(monkeypatch):
    """실전 체결조회 실패 시(모의투자·매수) 잔고 보유수량을 신뢰 소스로 폴백한다."""
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10, side=OrderSide.BUY)
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    _patch_recorded_qty(monkeypatch, order_qty=0, symbol_qty=0)
    monkeypatch.setattr(reconcile, "settings", type("S", (), {"is_paper_trading": True})())
    broker = _FakeBroker(
        execution_error=BrokerError("모의투자 일별체결조회 무응답"),
        balance_positions=[{"symbol": "005930", "qty": 5, "avg_price": "71000"}],
    )
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert stats["errors"] == 1
    assert len(calls) == 1
    assert calls[0]["qty"] == 5  # 보유 5주 전량 이 주문에 배정(target 10 미만)
    assert calls[0]["fully_filled"] is False
    assert stats["partial"] == 1


async def test_balance_fallback_returns_when_symbol_not_held(monkeypatch):
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10, side=OrderSide.BUY)
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    _patch_recorded_qty(monkeypatch, order_qty=0)
    monkeypatch.setattr(reconcile, "settings", type("S", (), {"is_paper_trading": True})())
    broker = _FakeBroker(execution_error=BrokerError("무응답"), balance_positions=[])
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert calls == []
    assert stats["filled"] == 0 and stats["partial"] == 0 and stats["errors"] == 1


async def test_balance_fallback_skipped_for_sell_orders(monkeypatch):
    """매도는 잔고 감소라 폴백으로 판정하기 모호하므로 실전 경로에만 맡긴다."""
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10, side=OrderSide.SELL)
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    _patch_recorded_qty(monkeypatch, order_qty=0)
    monkeypatch.setattr(reconcile, "settings", type("S", (), {"is_paper_trading": True})())
    broker = _FakeBroker(
        execution_error=BrokerError("무응답"),
        balance_positions=[{"symbol": "005930", "qty": 10, "avg_price": "71000"}],
    )
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert calls == []
    assert broker.balance_calls == 0  # 매도는 잔고조회조차 시도하지 않는다.
    assert stats["errors"] == 1


async def test_balance_fallback_disabled_when_not_paper_trading(monkeypatch):
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10, side=OrderSide.BUY)
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    _patch_recorded_qty(monkeypatch, order_qty=0)
    monkeypatch.setattr(reconcile, "settings", type("S", (), {"is_paper_trading": False})())
    broker = _FakeBroker(
        execution_error=BrokerError("무응답"),
        balance_positions=[{"symbol": "005930", "qty": 10, "avg_price": "71000"}],
    )
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert calls == []
    assert broker.balance_calls == 0  # 실전 환경에서는 잔고 폴백을 쓰지 않는다.
    assert stats["errors"] == 1


async def test_balance_fallback_respects_qty_already_consumed_by_other_orders(monkeypatch):
    """같은 종목을 다른 주문이 이미 소비한 수량은 이 주문에 중복 배정하지 않는다."""
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10, side=OrderSide.BUY)
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    # 보유 10주 중 8주는 이미 다른 주문 기록으로 설명됨 → 이 주문엔 최대 2주만.
    _patch_recorded_qty(monkeypatch, order_qty=0, symbol_qty=8)
    monkeypatch.setattr(reconcile, "settings", type("S", (), {"is_paper_trading": True})())
    broker = _FakeBroker(
        execution_error=BrokerError("무응답"),
        balance_positions=[{"symbol": "005930", "qty": 10, "avg_price": "71000"}],
    )
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert len(calls) == 1
    assert calls[0]["qty"] == 2
    assert stats["partial"] == 1


async def test_lock_skip_when_another_process_holds_lock(monkeypatch):
    """다른 프로세스가 이미 이 주문의 락을 쥐고 있으면 브로커 생성조차 시도하지 않는다."""
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10)
    session = _FakeSession([order], user)
    redis = FakeRedis()
    redis.store[f"reconcile:lock:{order.id}"] = "1"  # 이미 다른 프로세스가 보유 중
    broker_created = []
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker_created.append(1))

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert stats == {"checked": 0, "filled": 0, "partial": 0, "errors": 0}
    assert broker_created == []


async def test_unconfirmed_pending_order_without_kis_id_resolved_via_paper_balance(monkeypatch):
    """executor 의 order_unconfirmed 경로가 남긴 주문(PENDING·kis_order_id=None)은
    일별체결조회 키가 없어 실전 경로를 못 타지만, 모의투자 BUY 는 잔고 폴백으로
    자기수렴돼 고아화되지 않는다. get_order_execution 은 주문번호가 없어 아예
    호출조차 되지 않아야 한다(None 을 넘겨 브로커를 깨우지 않음)."""
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10, side=OrderSide.BUY, status=OrderStatus.PENDING)
    order.kis_order_id = None  # 접수 결과 불명 — 주문번호 미수신
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    _patch_recorded_qty(monkeypatch, order_qty=0, symbol_qty=0)
    monkeypatch.setattr(reconcile, "settings", type("S", (), {"is_paper_trading": True})())

    exec_calls = []

    class _NoIdBroker(_FakeBroker):
        async def get_order_execution(self, order_id, symbol=None):
            exec_calls.append(order_id)
            return await super().get_order_execution(order_id, symbol)

    broker = _NoIdBroker(
        balance_positions=[{"symbol": "005930", "qty": 10, "avg_price": "71000"}],
    )
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert exec_calls == []  # 주문번호 없어 일별체결조회를 건너뛴다.
    assert len(calls) == 1
    assert calls[0]["qty"] == 10  # 잔고 보유 10주 전량 이 주문에 배정
    assert calls[0]["fully_filled"] is True
    assert stats["filled"] == 1


async def test_unconfirmed_pending_order_alerts_when_balance_cannot_resolve(monkeypatch):
    """잔고 교차확인으로도 못 밝히면(실전이거나, 모의투자인데 잔고에도 없으면)
    자동 재주문하지 않고 critical 알림으로 사람에게 넘긴다."""
    user = User(email="t@example.com", password_hash="x")
    user.id = 1
    order = _order(1, user_id=1, qty=10, side=OrderSide.BUY, status=OrderStatus.PENDING)
    order.kis_order_id = None
    session = _FakeSession([order], user)
    redis = FakeRedis()
    calls = _record_fill_spy(monkeypatch)
    _patch_recorded_qty(monkeypatch, order_qty=0, symbol_qty=0)
    monkeypatch.setattr(reconcile, "settings", type("S", (), {"is_paper_trading": False})())
    broker = _FakeBroker(balance_positions=[])
    monkeypatch.setattr(reconcile, "make_broker_for_user", lambda _u: broker)

    alerts_published: list[dict] = []

    async def _fake_publish_alert(_redis, **kwargs):
        alerts_published.append(kwargs)

    monkeypatch.setattr(reconcile, "publish_alert", _fake_publish_alert)

    stats = await reconcile.reconcile_open_orders(session, redis)

    assert calls == []  # 자동 재주문·자동 확정 없음
    assert order.status == OrderStatus.PENDING  # 상태를 임의로 바꾸지 않는다
    assert len(alerts_published) == 1
    alert = alerts_published[0]
    assert alert["severity"] == "critical"
    assert alert["code"] == "order_unconfirmed"
    assert stats["filled"] == 0 and stats["partial"] == 0
