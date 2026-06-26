"""매매 조회 라우트 — 포지션·주문 내역(감사 로그)."""
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import Order, Position, User

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
    created_at: datetime


@router.get("/positions", response_model=list[PositionOut])
async def list_positions(
    current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """로그인 사용자의 보유 포지션(수량 > 0)을 반환한다."""
    rows = await db.scalars(
        select(Position).where(Position.user_id == current.id, Position.qty > 0)
    )
    return [
        PositionOut(symbol=p.symbol, qty=float(p.qty), avg_price=float(p.avg_price))
        for p in rows
    ]


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    limit: int = 50,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """최근 주문 내역을 최신순으로 반환한다(감사 로그). limit 최대 200."""
    rows = await db.scalars(
        select(Order)
        .where(Order.user_id == current.id)
        .order_by(Order.created_at.desc())
        .limit(min(limit, 200))
    )
    return [
        OrderOut(
            id=o.id,
            symbol=o.symbol,
            side=o.side,
            qty=float(o.qty),
            price=float(o.price) if o.price is not None else None,
            status=o.status,
            created_at=o.created_at,
        )
        for o in rows
    ]
