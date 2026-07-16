"""KIS 실시간 체결통보(fill_notice) 골격 검증 — 실계정 라이브 접속은 검증하지 않는다.

라이브 KIS WS 접속(approval_key 발급·wss 연결)은 이 테스트가 다루지 않는다(실계정이
없어 종단 검증 불가). 대신 다음을 모의 프레임/인메모리 대역으로 검증한다:
  1) AES-256-CBC(+PKCS7) 복호화 왕복
  2) 파싱된 필드로 Order 매칭
  3) 델타 멱등(같은 누적체결수량 재통보는 스킵)
  4) 부분체결 → 전량체결 순차 통보의 누적 반영(중복 없음)
  5) KIS_HTS_ID 미설정 시 매니저 no-op
"""
from __future__ import annotations

import base64
from decimal import Decimal

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.core.config import settings
from app.models import Execution, Order, OrderSide, OrderStatus, Position
from engine.fill_notice import (
    FillNoticeManager,
    aes_cbc_base64_decrypt,
    apply_fill_notice,
    parse_fill_notice,
)

_KEY = "01234567890123456789012345678901"  # 32바이트(AES-256)
_IV = "0123456789012345"  # 16바이트


def _kis_encrypt(plain: str) -> str:
    """테스트용: KIS 와 동일한 AES-256-CBC(+PKCS7) 로 평문을 암호화해 base64 로 돌려준다."""
    padder = sym_padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plain.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(_KEY.encode("utf-8")), modes.CBC(_IV.encode("utf-8")))
    encryptor = cipher.encryptor()
    raw = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(raw).decode("utf-8")


def _notice_plain(odno: str, symbol: str, qty: int, price: str, side_code: str = "02") -> str:
    """23필드 체결통보 평문을 만든다(필드 인덱스는 engine/fill_notice.py 상단 표 참고)."""
    fields = ["0"] * 23
    fields[0] = "CUSTID"
    fields[1] = "ACNT001"
    fields[2] = odno
    fields[3] = ""
    fields[4] = side_code  # 01매도, 02매수
    fields[8] = symbol
    fields[9] = str(qty)
    fields[10] = price
    return "^".join(fields)


# ─────────────────────────── 1) AES 복호화 왕복 ───────────────────────────


def test_aes_roundtrip():
    plain = _notice_plain("0000000001", "005930", 10, "70000")
    cipher_b64 = _kis_encrypt(plain)
    decrypted = aes_cbc_base64_decrypt(_KEY, _IV, cipher_b64)
    assert decrypted == plain


def test_aes_roundtrip_non_ascii_safe_content():
    # 필드 값 자체는 ASCII 뿐이지만, 패딩 경계(블록 크기 배수 근처) 길이도 확인한다.
    plain = _notice_plain("1" * 10, "000660", 1, "123456789")
    cipher_b64 = _kis_encrypt(plain)
    assert aes_cbc_base64_decrypt(_KEY, _IV, cipher_b64) == plain


# ─────────────────────────── 2) 필드 파싱 ───────────────────────────


def test_parse_fill_notice_extracts_fields():
    plain = _notice_plain("0000000042", "005930", 7, "71500", side_code="02")
    notice = parse_fill_notice(plain)
    assert notice is not None
    assert notice.odno == "0000000042"
    assert notice.symbol == "005930"
    assert notice.qty == 7
    assert notice.price == Decimal("71500")
    assert notice.side == "buy"


def test_parse_fill_notice_sell_side():
    plain = _notice_plain("0000000043", "005930", 3, "71000", side_code="01")
    notice = parse_fill_notice(plain)
    assert notice is not None
    assert notice.side == "sell"


def test_parse_fill_notice_zero_qty_is_none():
    """체결수량 0(접수/거부/취소 통보)은 반영할 체결이 없으므로 None."""
    plain = _notice_plain("0000000044", "005930", 0, "0")
    assert parse_fill_notice(plain) is None


def test_parse_fill_notice_too_few_fields_is_none():
    assert parse_fill_notice("a^b^c") is None


# ─────────────────────────── 3~4) Order 매칭 + 델타 멱등 ───────────────────────────


def _eq_filters(stmt) -> dict:
    clause = stmt.whereclause
    clauses = getattr(clause, "clauses", [clause])
    out = {}
    for c in clauses:
        out[c.left.name] = c.right.value
    return out


class _FakeSession:
    """apply_fill_notice 가 쓰는 scalar(select(Order/Execution/Position).where(...))·
    add()·commit() 만 지원하는 인메모리 대역. AsyncSessionLocal 자리에 monkeypatch 로
    주입한다."""

    def __init__(self):
        self.orders: list[Order] = []
        self.executions: list[Execution] = []
        self.positions: list[Position] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj) -> None:
        if isinstance(obj, Execution):
            self.executions.append(obj)
        elif isinstance(obj, Position):
            self.positions.append(obj)
        elif isinstance(obj, Order):
            self.orders.append(obj)

    async def commit(self) -> None:
        return None

    async def scalar(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        f = _eq_filters(stmt)
        if entity is Order:
            for o in self.orders:
                if all(getattr(o, k, None) == v for k, v in f.items()):
                    return o
            return None
        if entity is Execution:
            # _order_recorded_qty 의 select(func.coalesce(func.sum(...))) 쿼리.
            return sum(
                int(e.filled_qty)
                for e in self.executions
                if all(getattr(e, k, None) == v for k, v in f.items())
            )
        if entity is Position:
            for p in self.positions:
                if all(getattr(p, k, None) == v for k, v in f.items()):
                    return p
            return None
        return None


class _FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, data):
        self.published.append((channel, data))
        return 1


def _order(order_id: int, user_id: int, symbol: str, qty: int, kis_order_id: str) -> Order:
    return Order(
        id=order_id,
        user_id=user_id,
        strategy_id=1,
        symbol=symbol,
        side=OrderSide.BUY,
        qty=Decimal(qty),
        price=Decimal("70000"),
        kis_order_id=kis_order_id,
        status=OrderStatus.SUBMITTED,
    )


async def test_apply_fill_notice_matches_order_and_records_fill(monkeypatch):
    from engine import fill_notice

    session = _FakeSession()
    order = _order(1, user_id=7, symbol="005930", qty=10, kis_order_id="ODNO001")
    session.orders.append(order)
    monkeypatch.setattr(fill_notice, "AsyncSessionLocal", lambda: session)

    notice = parse_fill_notice(_notice_plain("ODNO001", "005930", 10, "71000"))
    redis = _FakeRedis()
    applied = await apply_fill_notice(7, notice, redis)

    assert applied is True
    assert len(session.executions) == 1
    assert session.executions[0].filled_qty == Decimal(10)
    assert order.status == OrderStatus.FILLED
    # publish_event 는 사용자별 채널 + 공용 채널(하위호환) 양쪽에 발행한다.
    assert len(redis.published) == 2
    assert {ch for ch, _ in redis.published} == {"engine:events:7", "engine:events"}


async def test_apply_fill_notice_no_matching_order_is_noop(monkeypatch):
    from engine import fill_notice

    session = _FakeSession()  # 주문 없음
    monkeypatch.setattr(fill_notice, "AsyncSessionLocal", lambda: session)

    notice = parse_fill_notice(_notice_plain("UNKNOWN", "005930", 5, "70000"))
    redis = _FakeRedis()
    applied = await apply_fill_notice(999, notice, redis)

    assert applied is False
    assert session.executions == []
    assert redis.published == []


async def test_duplicate_notice_same_cumulative_qty_is_skipped(monkeypatch):
    """같은 누적체결수량의 통보가 재수신돼도 두 번째 호출에서는 반영되지 않는다."""
    from engine import fill_notice

    session = _FakeSession()
    order = _order(1, user_id=7, symbol="005930", qty=10, kis_order_id="ODNO002")
    session.orders.append(order)
    monkeypatch.setattr(fill_notice, "AsyncSessionLocal", lambda: session)
    redis = _FakeRedis()

    notice = parse_fill_notice(_notice_plain("ODNO002", "005930", 4, "70000"))
    first = await apply_fill_notice(7, notice, redis)
    assert first is True
    assert len(session.executions) == 1

    # 동일 누적체결수량(4)의 통보가 재수신 — 델타 0이므로 스킵.
    second = await apply_fill_notice(7, notice, redis)
    assert second is False
    assert len(session.executions) == 1  # 중복 기록 없음


async def test_partial_then_full_fill_accumulates_without_duplication(monkeypatch):
    """부분체결(누적 4) → 전량체결(누적 10) 순차 통보가 중복 없이 정확히 반영된다."""
    from engine import fill_notice

    session = _FakeSession()
    order = _order(1, user_id=7, symbol="005930", qty=10, kis_order_id="ODNO003")
    session.orders.append(order)
    monkeypatch.setattr(fill_notice, "AsyncSessionLocal", lambda: session)
    redis = _FakeRedis()

    partial = parse_fill_notice(_notice_plain("ODNO003", "005930", 4, "70000"))
    await apply_fill_notice(7, partial, redis)
    assert order.status == OrderStatus.PARTIAL
    assert sum(int(e.filled_qty) for e in session.executions) == 4

    full = parse_fill_notice(_notice_plain("ODNO003", "005930", 10, "70100"))
    await apply_fill_notice(7, full, redis)

    assert order.status == OrderStatus.FILLED
    # 첫 4주 + 이번 델타 6주 = 누적 10주. 중복 기록 없이 정확히 두 건.
    assert len(session.executions) == 2
    assert sum(int(e.filled_qty) for e in session.executions) == 10


# ─────────────────────────── 5) KIS_HTS_ID 미설정 시 no-op ───────────────────────────


async def test_manager_noop_when_hts_id_unset(monkeypatch):
    monkeypatch.setattr(settings, "KIS_HTS_ID", None)
    redis = _FakeRedis()
    mgr = FillNoticeManager(redis)

    await mgr.ensure(1, "appkey", "appsecret")

    assert mgr.active_users() == set()

    # 정리 시에도 안전(구독한 적 없으므로 아무 일도 일어나지 않음).
    await mgr.shutdown()
    assert mgr.active_users() == set()
