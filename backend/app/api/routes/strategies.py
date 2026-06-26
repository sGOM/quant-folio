"""전략 CRUD 라우트."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import Strategy, StrategyStatus, User
from app.schemas.strategy import StrategyCreate, StrategyOut, StrategyUpdate

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


async def _get_owned(db: AsyncSession, user: User, strategy_id: int) -> Strategy:
    """소유자 본인의 전략만 조회한다(IDOR 방지). 없거나 타인 소유면 404.

    :return: 소유 확인된 Strategy
    :raises HTTPException: 전략이 없거나 요청자 소유가 아니면 404
    """
    s = await db.scalar(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user.id)
    )
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")
    return s


@router.get("", response_model=list[StrategyOut])
async def list_strategies(
    current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """로그인 사용자의 전략 목록을 최신순으로 반환한다."""
    rows = await db.scalars(
        select(Strategy).where(Strategy.user_id == current.id).order_by(Strategy.id.desc())
    )
    return list(rows)


@router.post("", response_model=StrategyOut, status_code=status.HTTP_201_CREATED)
async def create_strategy(
    payload: StrategyCreate,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """신규 전략을 draft 상태로 생성한다."""
    s = Strategy(
        user_id=current.id,
        name=payload.name,
        config=payload.config.model_dump(),
        status=StrategyStatus.DRAFT,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


@router.get("/{strategy_id}", response_model=StrategyOut)
async def get_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략 단건을 조회한다(본인 소유만)."""
    return await _get_owned(db, current, strategy_id)


@router.patch("/{strategy_id}", response_model=StrategyOut)
async def update_strategy(
    strategy_id: int,
    payload: StrategyUpdate,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략의 이름/설정을 부분 갱신한다(전달된 필드만)."""
    s = await _get_owned(db, current, strategy_id)
    if payload.name is not None:
        s.name = payload.name
    if payload.config is not None:
        s.config = payload.config.model_dump()
    await db.commit()
    await db.refresh(s)
    return s


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략을 삭제한다(본인 소유만). 성공 시 204."""
    s = await _get_owned(db, current, strategy_id)
    await db.delete(s)
    await db.commit()
