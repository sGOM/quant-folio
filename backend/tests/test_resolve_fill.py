"""executor._resolve_fill 폴백 규약 검증.

핵심 회귀 방지: 체결 조회가 실패(BrokerError)하면 체결 여부를 알 수 없으므로
반드시 미체결(0)로 보고해 주문을 SUBMITTED 로 남겨야 한다. 과거에는 요청 수량
전량을 체결로 폴백해, 실제 미체결 주문이 FILLED 로 확정되고 reconcile 대상
(SUBMITTED/PARTIAL)에서도 빠져 포지션 장부가 영구 오염되는 버그가 있었다.
"""
from decimal import Decimal

from app.models import Order
from app.services.broker.base import BrokerError, Fill
from engine.executor import _resolve_fill


class _FailingBroker:
    """체결 조회가 항상 실패하는 대역(네트워크 오류 등)."""

    async def get_order_execution(self, order_id, symbol=None):
        raise BrokerError("연결 실패")


class _StubBroker:
    """고정 Fill 을 돌려주는 대역."""

    def __init__(self, fill: Fill):
        self._fill = fill

    async def get_order_execution(self, order_id, symbol=None):
        return self._fill


def _order(kis_order_id: str | None = "K-1") -> Order:
    return Order(
        symbol="005930", idempotency_key="s1:005930:buy:2026-07-17",
        kis_order_id=kis_order_id,
    )


async def test_query_failure_reports_unfilled():
    """조회 실패 → 전량 체결 폴백이 아니라 미체결(0)로 보고(SUBMITTED 유지)."""
    qty, price = await _resolve_fill(
        _FailingBroker(), _order(), 10, Decimal("70000")
    )
    assert qty == 0
    assert price == Decimal("70000")


async def test_no_order_id_reports_unfilled():
    stub = _StubBroker(Fill(filled_qty=10, avg_price=Decimal("70000"), fully_filled=True))
    qty, _ = await _resolve_fill(stub, _order(None), 10, Decimal("70000"))
    assert qty == 0


async def test_filled_with_avg_price():
    broker = _StubBroker(Fill(filled_qty=7, avg_price=70100, fully_filled=False))
    qty, price = await _resolve_fill(broker, _order(), 10, Decimal("70000"))
    assert qty == 7
    assert price == Decimal("70100")


async def test_filled_without_avg_falls_back_to_signal_price():
    broker = _StubBroker(Fill(filled_qty=10, avg_price=None, fully_filled=True))
    qty, price = await _resolve_fill(broker, _order(), 10, Decimal("70000"))
    assert qty == 10
    assert price == Decimal("70000")


async def test_zero_filled_reports_unfilled():
    broker = _StubBroker(Fill(filled_qty=0, avg_price=None, fully_filled=False))
    qty, price = await _resolve_fill(broker, _order(), 10, Decimal("70000"))
    assert qty == 0
    assert price == Decimal("70000")
