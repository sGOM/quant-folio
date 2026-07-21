"""매매 조회 라우트 — 포지션·주문 내역(감사 로그)."""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import Order, Position, User
from app.services.broker import BrokerError, make_broker_for_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trading", tags=["trading"])


# 응답 계약을 코드로 고정해 프론트와의 스키마 드리프트를 방지한다.
class PositionOut(BaseModel):
    symbol: str
    qty: float
    avg_price: float


class OrderOut(BaseModel):
    id: int
    symbol: str
    side: str
    qty: float
    price: float | None
    status: str
    reason: str | None
    created_at: datetime


async def _db_positions(db: AsyncSession, user_id: int) -> list[PositionOut]:
    """로컬 DB(Position) 그림자 잔고. 브로커 조회 실패 시의 폴백.

    사용자에게 보여줄 계좌 잔고는 전략에 무관한 전체 합산 뷰이므로 strategy_id 로
    좁히지 않는다(다중 전략 분리는 엔진 내부 포지션 회계에만 적용).
    """
    rows = await db.scalars(
        select(Position).where(Position.user_id == user_id, Position.qty > 0)
    )
    return [
        PositionOut(symbol=p.symbol, qty=float(p.qty), avg_price=float(p.avg_price))
        for p in rows
    ]


@router.get("/positions", response_model=list[PositionOut])
async def list_positions(
    current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """로그인 사용자의 보유 포지션(수량 > 0)을 반환한다.

    신뢰 소스는 **실제 증권사 계좌 잔고**다. 로컬 DB(Position)는 이 시스템이 직접
    체결·정합한 주문만 반영하므로, 다른 서버·계좌·수동 매매로 생긴 보유분을 놓친다.
    브로커 잔고를 우선 조회하고, 자격증명 미등록·조회 실패 시에만 DB 그림자로 폴백한다.
    """
    try:
        broker = make_broker_for_user(current)
    except BrokerError:
        # 자격증명 미등록 — 로컬 그림자라도 보여준다.
        return await _db_positions(db, current.id)
    try:
        balance = await broker.get_balance()
    except BrokerError as e:
        logger.warning("실계좌 잔고 조회 실패 — 로컬 포지션으로 폴백 user=%s: %s", current.id, e)
        return await _db_positions(db, current.id)
    return [
        PositionOut(
            symbol=p["symbol"],
            qty=float(p["qty"]),
            avg_price=float(p["avg_price"]) if p["avg_price"] is not None else 0.0,
        )
        for p in balance.positions_normalized()
    ]


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    limit: int = Query(50, ge=1, le=200),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """최근 주문 내역을 최신순으로 반환한다(감사 로그). limit 1~200."""
    rows = await db.scalars(
        select(Order)
        .where(Order.user_id == current.id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    return [
        OrderOut(
            id=o.id,
            symbol=o.symbol,
            side=o.side,
            qty=float(o.qty),
            price=float(o.price) if o.price is not None else None,
            status=o.status,
            reason=o.reason,
            created_at=o.created_at,
        )
        for o in rows
    ]
