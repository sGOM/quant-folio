"""리스크 관리 — 주문 전 검증. 손절·최대 포지션·일일 한도.

리스크 통과 못 하면 주문을 생성하지 않는다(안전 우선).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Execution, Order, OrderSide, Position, RiskLimit

logger = logging.getLogger("engine.risk")


@dataclass
class RiskDecision:
    """리스크 평가 결과.

    :param approved: 주문 허용 여부
    :param qty: 허용 수량(매수는 산정 수량, 매도는 청산 수량)
    :param reason: 거부 사유(approved=False 일 때)
    """
    approved: bool
    qty: int
    reason: str = ""


async def evaluate_buy(
    db: AsyncSession,
    user_id: int,
    strategy_id: int,
    symbol: str,
    price: Decimal,
    desired_cash: Decimal,
) -> RiskDecision:
    """매수 리스크 평가. max_position_size 한도 내에서 수량을 산정."""
    if price <= 0:
        return RiskDecision(False, 0, "유효하지 않은 가격")

    limit = await _get_limit(db, user_id, strategy_id)
    cash = desired_cash
    if limit and limit.max_position_size is not None:
        # 이미 보유 중인 금액을 빼고 남은 한도까지만
        pos = await _get_position(db, user_id, symbol)
        held_value = (pos.qty * pos.avg_price) if pos else Decimal("0")
        remaining = limit.max_position_size - held_value
        if remaining <= 0:
            return RiskDecision(False, 0, "최대 포지션 한도 도달")
        cash = min(cash, remaining)

    qty = int(cash // price)
    if qty <= 0:
        return RiskDecision(False, 0, "주문 가능 수량 부족")
    return RiskDecision(True, qty, "")


async def evaluate_sell(
    db: AsyncSession, user_id: int, symbol: str
) -> RiskDecision:
    """매도 리스크 평가. 보유 수량 전량 청산."""
    pos = await _get_position(db, user_id, symbol)
    if pos is None or pos.qty <= 0:
        return RiskDecision(False, 0, "보유 수량 없음")
    return RiskDecision(True, int(pos.qty), "")


async def check_daily_loss_limit(
    db: AsyncSession,
    user_id: int,
    strategy_id: int,
    current_prices: dict[str, Decimal],
) -> RiskDecision:
    """일일 손실 한도 검사. 한도 초과 시 approved=False (신규 진입 차단).

    당일 손익 = 당일 체결의 실현 현금흐름(매도수익 − 매수비용 − 수수료)
              + 현재 보유 포지션의 평가손익(현재가 기준).
    한도(daily_loss_limit)는 양수(허용 최대 손실액)이며, 손익 ≤ −한도이면 차단한다.
    """
    limit = await _get_limit(db, user_id, strategy_id)
    if not limit or limit.daily_loss_limit is None:
        return RiskDecision(True, 0, "")

    pnl = await _daily_pnl(db, user_id, current_prices)
    if pnl <= -limit.daily_loss_limit:
        logger.warning(
            "일일 손실 한도 초과: 손익 %s <= -%s — 신규 진입 차단",
            pnl, limit.daily_loss_limit,
        )
        return RiskDecision(False, 0, "일일 손실 한도 초과")
    return RiskDecision(True, 0, "")


def _today_start_utc() -> datetime:
    """당일(KST 거래일)의 시작 시각을 UTC 로 반환한다(체결 조회 경계용)."""
    # KST 자정을 기준으로 당일 범위를 잡는다(KRX 거래일 기준).
    from app.services.market import KST

    kst_midnight = datetime.combine(datetime.now(KST).date(), time.min, tzinfo=KST)
    return kst_midnight.astimezone(timezone.utc)


async def _daily_pnl(
    db: AsyncSession, user_id: int, current_prices: dict[str, Decimal]
) -> Decimal:
    """당일 손익(실현 현금흐름 + 보유 포지션 평가손익)을 계산한다.

    :param current_prices: 종목별 현재가(평가손익 계산용). 없는 종목은 평가에서 제외
    :return: 당일 손익(양수=이익, 음수=손실)
    """
    start = _today_start_utc()
    rows = (
        await db.execute(
            select(Execution, Order.side)
            .join(Order, Execution.order_id == Order.id)
            .where(Order.user_id == user_id, Execution.executed_at >= start)
        )
    ).all()

    realized = Decimal("0")
    for ex, side in rows:
        gross = ex.filled_qty * ex.filled_price
        if side == OrderSide.SELL:
            realized += gross - ex.fee
        else:  # BUY — 현금 유출
            realized -= gross + ex.fee

    # 보유 포지션 평가손익(현재가 제공된 종목만).
    unrealized = Decimal("0")
    positions = (
        await db.scalars(select(Position).where(Position.user_id == user_id))
    ).all()
    for pos in positions:
        cur = current_prices.get(pos.symbol)
        if cur is None or pos.qty <= 0:
            continue
        unrealized += (Decimal(str(cur)) - pos.avg_price) * pos.qty

    return realized + unrealized


async def check_stop_loss(
    db: AsyncSession,
    user_id: int,
    strategy_id: int,
    symbol: str,
    current_price: Decimal,
) -> bool:
    """손절 조건 도달 여부. avg_price 대비 stop_loss_pct 이상 하락 시 True."""
    limit = await _get_limit(db, user_id, strategy_id)
    if not limit or limit.stop_loss_pct is None:
        return False
    pos = await _get_position(db, user_id, symbol)
    if pos is None or pos.qty <= 0 or pos.avg_price <= 0:
        return False
    drop = (pos.avg_price - current_price) / pos.avg_price
    if drop >= limit.stop_loss_pct:
        logger.info(
            "손절 조건 도달: %s 하락률 %.4f >= %.4f", symbol, drop, limit.stop_loss_pct
        )
        return True
    return False


async def _get_limit(db: AsyncSession, user_id: int, strategy_id: int) -> RiskLimit | None:
    """적용할 리스크 한도를 조회한다(전략별 한도 우선, 없으면 사용자 공통 한도)."""
    return await db.scalar(
        select(RiskLimit)
        .where(RiskLimit.user_id == user_id)
        .where((RiskLimit.strategy_id == strategy_id) | (RiskLimit.strategy_id.is_(None)))
        .order_by(RiskLimit.strategy_id.desc().nullslast())
    )


async def _get_position(db: AsyncSession, user_id: int, symbol: str) -> Position | None:
    """사용자의 특정 종목 포지션을 조회한다(없으면 None)."""
    return await db.scalar(
        select(Position).where(Position.user_id == user_id, Position.symbol == symbol)
    )
