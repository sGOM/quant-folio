"""미체결(SUBMITTED/PARTIAL) 주문을 실제 체결로 정합해 FILLED/PARTIAL·포지션을 갱신.

executor.execute_signal 은 주문 직후 딱 한 번만 체결을 조회한다(_resolve_fill). 그
시점에 아직 체결되지 않은(시장가 접수 직후) 주문은 SUBMITTED 로 남고, 이후 체결돼도
DB 는 갱신되지 않는다. 이 모듈이 남은 미체결 주문을 재조회해 실제 체결로 수렴시킨다.

- 엔진(engine/main.py._reconcile_loop): 주기적으로 자동 호출.
- 수동(scripts/reconcile_fills.py): 즉시 1회 정합.

체결 확인 소스는 두 갈래다.
  1) 일별주문체결조회(get_order_execution) — 실전에서 주문 단위 체결수량·평균가를 정확히 준다.
  2) 잔고조회(get_balance) 폴백 — KIS **모의투자**의 일별체결조회는 이 계좌에서 늘 빈
     응답을 주므로(관측), 모의투자에서는 잔고 보유수량을 신뢰 소스로 삼아 크로스체크한다.

멱등 안전성: 이미 기록된 체결수량(executions 합)을 빼고 '증분(delta)'만 기록하므로,
재조회를 몇 번 반복해도 포지션이 중복 가산되지 않는다.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Execution, Order, OrderSide, OrderStatus, User
from app.services.broker import BrokerClient, BrokerError, make_broker_for_user
from engine.fills import publish_event, record_fill

logger = logging.getLogger("engine.reconcile")

# 아직 체결이 마무리되지 않아 재조회 대상인 상태.
_OPEN_STATUSES = (OrderStatus.SUBMITTED, OrderStatus.PARTIAL)

# 같은 주문을 여러 프로세스(엔진 _reconcile_loop + 수동 scripts/reconcile_fills.py)가
# 동시에 정합하면, 둘 다 커밋 전에 executions 합을 읽어 같은 체결을 이중 기록할 수 있다.
# 주문 단위 분산 락(SET NX)으로 한 번에 한 프로세스만 해당 주문을 처리하게 직렬화한다.
_RECONCILE_LOCK_PREFIX = "reconcile:lock:"
_RECONCILE_LOCK_TTL = 30  # 초


def _balance_map(balance) -> dict[str, tuple[int, Decimal | None]]:
    """KIS 잔고 positions 를 {종목코드: (보유수량, 매입평균가)} 로 정규화한다.

    정규화 규칙은 Balance.positions_normalized 에 있다(라우트·정합이 공유).
    """
    return {
        p["symbol"]: (p["qty"], p["avg_price"])
        for p in balance.positions_normalized()
    }


async def _symbol_recorded_qty(db: AsyncSession, user_id: int, symbol: str) -> int:
    """해당 사용자·종목에 대해 이미 체결로 기록된 총 수량(executions 합)."""
    val = await db.scalar(
        select(func.coalesce(func.sum(Execution.filled_qty), 0))
        .join(Order, Execution.order_id == Order.id)
        .where(Order.user_id == user_id, Order.symbol == symbol)
    )
    return int(val or 0)


async def _order_recorded_qty(db: AsyncSession, order_id: int) -> int:
    """이 주문에 이미 기록된 체결수량(executions 합)."""
    val = await db.scalar(
        select(func.coalesce(func.sum(Execution.filled_qty), 0)).where(
            Execution.order_id == order_id
        )
    )
    return int(val or 0)


async def reconcile_open_orders(
    db: AsyncSession, redis: Redis, *, user_id: int | None = None
) -> dict[str, int]:
    """미체결 주문을 실제 체결로 재확인해 체결분을 반영한다.

    :param user_id: 지정 시 해당 사용자 주문만(엔진은 전체, 스크립트는 선택적으로 좁힘).
    :returns: {"checked", "filled", "partial", "errors"} 처리 통계.
    """
    stmt = select(Order).where(
        Order.status.in_(_OPEN_STATUSES),
        Order.kis_order_id.isnot(None),
        Order.kis_order_id != "",
    )
    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    orders = list(await db.scalars(stmt))
    stats = {"checked": 0, "filled": 0, "partial": 0, "errors": 0}
    if not orders:
        return stats

    # user 별 브로커·잔고를 한 번만 준비해 재사용(주문마다 토큰 발급·잔고조회 방지).
    brokers: dict[int, BrokerClient | None] = {}
    balances: dict[int, dict[str, tuple[int, Decimal | None]]] = {}

    async def _broker(uid: int) -> BrokerClient | None:
        if uid not in brokers:
            user = await db.scalar(select(User).where(User.id == uid))
            try:
                brokers[uid] = make_broker_for_user(user) if user else None
            except BrokerError as e:
                logger.warning("브로커 생성 실패 user=%s: %s", uid, e)
                brokers[uid] = None
                # 이전엔 여기서 조용히 None 을 반환해 해당 사용자의 주문이 매 주기
                # 아무 통계에도 안 잡히고 넘어갔다(자격증명이 계속 만료 상태여도
                # _reconcile_loop 의 실패 카운터가 못 봄). errors 에 반영해 눈에 띄게 한다.
                stats["errors"] += 1
        return brokers[uid]

    async def _balance(uid: int, broker: BrokerClient) -> dict:
        if uid not in balances:
            try:
                balances[uid] = _balance_map(await broker.get_balance())
            except BrokerError as e:
                logger.warning("잔고 조회 실패 user=%s: %s", uid, e)
                balances[uid] = {}
        return balances[uid]

    async def _apply(order: Order, delta: int, price: Decimal, fully: bool) -> None:
        await record_fill(db, order, delta, price, fully_filled=fully, redis=redis)
        await db.commit()
        stats["filled" if fully else "partial"] += 1
        await publish_event(redis, {
            "type": "execution", "user_id": order.user_id, "order_id": order.id,
            "symbol": order.symbol, "side": str(order.side), "qty": delta,
            "price": float(price), "status": str(order.status),
            "kis_order_id": order.kis_order_id,
        })
        logger.info(
            "체결 정합: %s %s +%d주 @ %s → %s",
            order.side, order.symbol, delta, price, order.status,
        )

    async def _process_one(order: Order) -> None:
        """단일 주문 정합. 분산 락 보유 상태에서만 호출된다(중복 처리 방지)."""
        broker = await _broker(order.user_id)
        if broker is None:
            return
        stats["checked"] += 1
        target = int(order.qty)
        already = await _order_recorded_qty(db, order.id)

        # ── 1) 일별주문체결조회(실전 정확 경로) ──
        kis_filled = -1  # -1: 조회 실패/무응답
        avg_price: Decimal | None = None
        try:
            info = await broker.get_order_execution(order.kis_order_id, order.symbol)
            kis_filled = int(info.filled_qty or 0)
            avg_price = info.avg_price
        except BrokerError as e:
            logger.debug("체결 재조회 실패 order=%s: %s", order.id, e)
            stats["errors"] += 1

        if kis_filled > 0:
            delta = kis_filled - already
            fully = kis_filled >= target
            if delta > 0:
                price = avg_price if avg_price and avg_price > 0 else order.price
                await _apply(order, delta, price, fully)
            else:
                # 수량은 이미 다 기록됨 — 상태만 어긋났으면(SUBMITTED 잔존) 보정.
                new_status = OrderStatus.FILLED if fully else OrderStatus.PARTIAL
                if order.status != new_status:
                    order.status = new_status
                    await db.commit()
            return

        # ── 2) 잔고 폴백(모의투자: 일별체결조회가 늘 비어 있음) ──
        # 매도는 잔고 감소라 이 폴백으로 판정하기 모호하므로 실전 경로에 맡긴다.
        if not settings.is_paper_trading or order.side != OrderSide.BUY:
            return
        bmap = await _balance(order.user_id, broker)
        held_qty, held_avg = bmap.get(order.symbol, (0, None))
        if held_qty <= 0:
            return  # 잔고에도 없음 → 진짜 미체결(또는 주문 실패).
        # 같은 종목 다른 주문이 이미 소비한 수량을 빼고 남은 만큼만 이 주문에 배정.
        sym_recorded = await _symbol_recorded_qty(db, order.user_id, order.symbol)
        available = held_qty - sym_recorded
        if available <= 0:
            return  # 이 종목 보유분은 이미 다른 주문 기록으로 설명됨.
        delta = min(target - already, available)
        if delta <= 0:
            return
        price = held_avg if held_avg and held_avg > 0 else order.price
        fully = (already + delta) >= target
        await _apply(order, delta, price, fully)

    for order in orders:
        lock_key = f"{_RECONCILE_LOCK_PREFIX}{order.id}"
        if not await redis.set(lock_key, "1", nx=True, ex=_RECONCILE_LOCK_TTL):
            continue  # 다른 프로세스가 이 주문을 정합 중 — 건너뛴다.
        try:
            await _process_one(order)
        finally:
            await redis.delete(lock_key)

    return stats
