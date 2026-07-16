"""체결 기록·이벤트 발행 공유 모듈.

executor(주문 직후 1회 기록)와 reconcile(주기적 미체결 재조회 기록)이 **동일한**
체결 반영 로직을 써야 포지션·주문 상태가 일관된다. 이 모듈이 그 단일 진입점이며,
양쪽이 여기서 import 한다(한쪽이 다른 쪽의 내부 함수를 직접 참조하지 않게 하는 seam).
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channels import ENGINE_EVENTS_CHANNEL, engine_events_channel
from app.models import Execution, Order, OrderSide, OrderStatus, Position

logger = logging.getLogger("engine.fills")


async def record_fill(
    db: AsyncSession, order: Order, qty: int, price: Decimal, *, fully_filled: bool
) -> None:
    """Execution 기록 + Position 평균단가 갱신 + 주문 상태 갱신."""
    db.add(Execution(
        order_id=order.id,
        strategy_id=order.strategy_id,  # 비정규화: 전략별 체결 직접 조회용
        filled_qty=Decimal(qty),
        filled_price=price,
        fee=Decimal("0"),
    ))
    order.status = OrderStatus.FILLED if fully_filled else OrderStatus.PARTIAL

    # 포지션은 (user_id, strategy_id, symbol) 로 스코프한다 — 같은 종목을 여러 전략이
    # 동시에 보유해도 포지션이 섞이지 않도록 소유 전략별로 분리 기록한다.
    pos = await db.scalar(
        select(Position).where(
            Position.user_id == order.user_id,
            Position.strategy_id == order.strategy_id,
            Position.symbol == order.symbol,
        )
    )
    fill_qty = Decimal(qty)
    if order.side == OrderSide.BUY:
        if pos is None:
            db.add(Position(
                user_id=order.user_id, strategy_id=order.strategy_id,
                symbol=order.symbol, qty=fill_qty, avg_price=price,
            ))
        else:
            new_qty = pos.qty + fill_qty
            pos.avg_price = (pos.qty * pos.avg_price + fill_qty * price) / new_qty
            pos.qty = new_qty
    else:  # SELL
        if pos is not None:
            pos.qty = max(pos.qty - fill_qty, Decimal("0"))
            if pos.qty == 0:
                pos.avg_price = Decimal("0")


async def publish_event(redis: Redis, payload: dict) -> None:
    """사용자별 채널로 발행 — 각 WS 소켓이 자기 채널만 구독해 팬아웃을 줄인다."""
    data = json.dumps(payload, default=str)
    uid = payload.get("user_id")
    if uid is not None:
        await redis.publish(engine_events_channel(int(uid)), data)
    # 하위호환: 공용 채널에도 발행(기존 구독자 보호).
    await redis.publish(ENGINE_EVENTS_CHANNEL, data)
