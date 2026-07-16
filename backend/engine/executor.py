"""주문 실행기 — 멱등성 보장 + KIS 주문 + 주문/체결/포지션 DB 기록 + 이벤트 발행.

멱등성은 3중으로 보장한다:
  1) idempotency_key 결정적 생성(전략·종목·side·신호봉시각)
  2) Redis 분산 락(SET NX)으로 동시 중복 차단
  3) orders.idempotency_key UNIQUE 제약(최종 방어선, IntegrityError 흡수)

체결 처리: 모의투자 검증 단계에서는 시장가 주문 접수를 즉시 체결로 간주해
Execution·Position 을 갱신한다(정밀 체결 통보 연동은 5단계).
"""
from __future__ import annotations

import logging
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channels import ORDER_LOCK_PREFIX
from app.models import (
    Order,
    OrderSide,
    OrderStatus,
)
from app.services.broker import BrokerClient, BrokerError
from engine.fills import publish_event as _publish
from engine.fills import record_fill as _record_fill

logger = logging.getLogger("engine.executor")

_LOCK_TTL = 30  # 초


def make_idempotency_key(strategy_id: int, symbol: str, side: str, bar_ts: str) -> str:
    """결정적 멱등성 키. 같은 신호봉의 같은 주문은 항상 동일 키."""
    return f"s{strategy_id}:{symbol}:{side}:{bar_ts}"


async def execute_signal(
    db: AsyncSession,
    redis: Redis,
    broker: BrokerClient,
    *,
    user_id: int,
    strategy_id: int,
    symbol: str,
    side: str,
    qty: int,
    price: Decimal,
    idempotency_key: str,
    reason: str | None = None,
) -> Order | None:
    """신호에 따른 주문 실행. 중복이면 None 반환.

    :param reason: 감사 로그용 주문 사유(어떤 신호·공식·리스크·리밸런싱 기준으로
        이 주문이 나갔는지 사람이 읽을 수 있는 한국어 설명). Order.reason 에 기록된다.
    """
    # 1) Redis 분산 락
    lock_key = f"{ORDER_LOCK_PREFIX}{idempotency_key}"
    got_lock = await redis.set(lock_key, "1", nx=True, ex=_LOCK_TTL)
    if not got_lock:
        logger.info("중복 주문 차단(락 보유 중): %s", idempotency_key)
        return None

    try:
        # 2) DB 기존 주문 확인
        existing = await db.scalar(
            select(Order).where(Order.idempotency_key == idempotency_key)
        )
        if existing is not None:
            logger.info("중복 주문 차단(DB 존재): %s", idempotency_key)
            return None

        # 3) 주문 레코드 생성(pending)
        order = Order(
            user_id=user_id,
            strategy_id=strategy_id,
            symbol=symbol,
            side=OrderSide(side),
            qty=Decimal(qty),
            price=price,
            order_type="market",
            status=OrderStatus.PENDING,
            idempotency_key=idempotency_key,
            reason=reason,
        )
        db.add(order)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.info("중복 주문 차단(UNIQUE 충돌): %s", idempotency_key)
            return None
        await db.refresh(order)

        # 4) 증권사 주문 (시장가)
        try:
            res = await broker.place_order(symbol, side, qty, price=0, order_type="market")
            order.kis_order_id = res.order_id
            order.kis_order_org_no = res.order_org_no
            order.status = OrderStatus.SUBMITTED
        except BrokerError as e:
            order.status = OrderStatus.REJECTED
            await db.commit()
            logger.warning("주문 거부됨 %s: %s", idempotency_key, e)
            await _publish(redis, {"type": "order", "user_id": user_id,
                                   "order_id": order.id, "status": "rejected", "symbol": symbol})
            return order

        # 5) 실제 체결 조회 후 기록 + 포지션 갱신.
        #    시장가라도 실제 체결가는 신호 시점가(price)와 다르므로,
        #    증권사 체결 조회로 실제 체결수량·평균체결가를 받아 기록한다.
        fill_qty, fill_price = await _resolve_fill(broker, order, qty, price)
        if fill_qty <= 0:
            # 미체결(접수만 됨) — 포지션·체결 기록하지 않고 SUBMITTED 유지.
            await db.commit()
            await _publish(redis, {
                "type": "order", "user_id": user_id, "order_id": order.id,
                "symbol": symbol, "side": side, "status": "submitted",
                "kis_order_id": order.kis_order_id,
            })
            logger.info("주문 접수(미체결): %s %s %d주", side, symbol, qty)
            return order

        await _record_fill(db, order, fill_qty, fill_price, fully_filled=(fill_qty >= qty))
        await db.commit()

        await _publish(redis, {
            "type": "execution", "user_id": user_id, "order_id": order.id,
            "symbol": symbol, "side": side, "qty": fill_qty, "price": float(fill_price),
            # status 컬럼은 String 이라 DB 재로드(expire/refresh) 시 순수 str 로 복원돼
            # .value 가 AttributeError 를 낸다. StrEnum 은 str() 이 값 문자열을 주므로
            # enum·str 양쪽에서 안전하게 직렬화한다.
            "status": str(order.status), "kis_order_id": order.kis_order_id,
        })
        logger.info("주문 체결 기록: %s %s %d주 @ %s (실제 체결가)", side, symbol, fill_qty, fill_price)
        return order
    finally:
        await redis.delete(lock_key)


async def _resolve_fill(
    broker: BrokerClient, order: Order, qty: int, signal_price: Decimal
) -> tuple[int, Decimal]:
    """증권사 체결 조회로 실제 체결수량·평균체결가를 얻는다.

    조회 실패 시(네트워크/스키마 변경) 신호가로 폴백하되 경고를 남긴다.
    폴백은 정확성이 떨어지므로 추후 체결 통보(WebSocket)로 보정 대상이다.
    """
    if not order.kis_order_id:
        return 0, signal_price
    try:
        info = await broker.get_order_execution(order.kis_order_id, order.symbol)
    except BrokerError as e:
        logger.warning("체결 조회 실패 — 신호가로 폴백 기록 %s: %s", order.idempotency_key, e)
        return qty, signal_price

    filled = int(info.filled_qty or 0)
    avg = info.avg_price
    if filled <= 0:
        return 0, signal_price
    if avg is None or avg <= 0:
        logger.warning("체결 평균가 없음 — 신호가로 폴백 %s", order.idempotency_key)
        return filled, signal_price
    return filled, Decimal(str(avg))
