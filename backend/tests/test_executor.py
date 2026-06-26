"""주문 멱등성 키 결정성 검증."""
from engine.executor import make_idempotency_key


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
