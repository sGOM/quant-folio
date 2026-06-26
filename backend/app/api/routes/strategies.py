"""전략 CRUD + 공유/복사/좋아요/정렬·즐겨찾기 라우트.

주의: FastAPI 는 선언 순서로 경로를 매칭하므로 정적 경로(/shared, /reorder)를
동적 경로(/{strategy_id}) 보다 먼저 선언한다.
"""
from datetime import datetime, timezone

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import Backtest, Strategy, StrategyLike, StrategyStatus, User
from app.schemas.strategy import (
    BacktestSummary,
    FeaturedBacktestIn,
    LikeOut,
    ReorderRequest,
    SharedStrategyOut,
    StrategyCreate,
    StrategyOut,
    StrategyUpdate,
)

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


async def _like_count(db: AsyncSession, strategy_id: int) -> int:
    """전략의 좋아요 수를 집계한다."""
    return await db.scalar(
        select(func.count()).select_from(StrategyLike).where(
            StrategyLike.strategy_id == strategy_id
        )
    ) or 0


@router.get("", response_model=list[StrategyOut])
async def list_strategies(
    current: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """로그인 사용자의 전략 목록. 즐겨찾기 우선, 사용자 지정 순서, 최신순으로 정렬."""
    rows = await db.scalars(
        select(Strategy)
        .where(Strategy.user_id == current.id)
        .order_by(
            Strategy.is_favorite.desc(),
            Strategy.sort_order.asc(),
            Strategy.id.desc(),
        )
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
        description=(payload.description or None),
        config=payload.config.model_dump(),
        status=StrategyStatus.DRAFT,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


# ─────────────────────────── 공유/복사/좋아요 ───────────────────────────
# 정적 경로는 /{strategy_id} 보다 먼저 선언해야 한다.


@router.get("/shared", response_model=list[SharedStrategyOut])
async def list_shared_strategies(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(None, description="전략 제목 부분일치 필터"),
    symbol: str | None = Query(None, description="종목코드 필터(단일종목·리밸런싱 universe)"),
    sort: Literal["likes", "name", "recent"] = Query("likes", description="정렬 기준"),
):
    """공유된 전체 사용자의 전략 목록. 제목·종목 필터, 좋아요/제목/최신 정렬을 지원한다."""
    like_count = func.count(StrategyLike.id)
    liked_by_me = func.coalesce(
        func.bool_or(StrategyLike.user_id == current.id), False
    )
    stmt = (
        select(
            Strategy,
            User.display_name,
            like_count.label("like_count"),
            liked_by_me.label("liked_by_me"),
            Backtest,
        )
        .join(User, User.id == Strategy.user_id)
        .outerjoin(StrategyLike, StrategyLike.strategy_id == Strategy.id)
        # 대표 백테스트(1:1). FK 가 단일 행을 가리키므로 그룹화에 PK 만 추가하면 된다.
        .outerjoin(Backtest, Backtest.id == Strategy.featured_backtest_id)
        .where(Strategy.is_shared.is_(True))
        .group_by(Strategy.id, User.display_name, Backtest.id)
    )

    if q:
        stmt = stmt.where(Strategy.name.ilike(f"%{q}%"))
    if symbol:
        # JSONB @> containment 으로 단일종목(config.symbol) 또는 리밸런싱 universe
        # 배열 포함을 OR 매칭한다.
        stmt = stmt.where(
            or_(
                Strategy.config.contains({"symbol": symbol}),
                Strategy.config.contains({"universe": [symbol]}),
            )
        )

    if sort == "name":
        stmt = stmt.order_by(Strategy.name.asc())
    elif sort == "recent":
        stmt = stmt.order_by(Strategy.shared_at.desc().nullslast())
    else:  # likes
        stmt = stmt.order_by(like_count.desc(), Strategy.shared_at.desc().nullslast())

    rows = (await db.execute(stmt)).all()
    return [
        SharedStrategyOut(
            id=s.id,
            name=s.name,
            description=s.description,
            config=s.config,
            author_name=display_name or "익명",
            like_count=int(cnt),
            liked_by_me=bool(liked),
            is_mine=(s.user_id == current.id),
            backtest=(
                BacktestSummary(
                    id=bt.id,
                    total_return=bt.total_return,
                    mdd=bt.mdd,
                    sharpe=bt.sharpe,
                    period_start=bt.period_start,
                    period_end=bt.period_end,
                )
                if bt is not None
                else None
            ),
            created_at=s.created_at,
        )
        for s, display_name, cnt, liked, bt in rows
    ]


@router.patch("/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_strategies(
    payload: ReorderRequest,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """내 전략들의 표시 순서를 배열 순서대로 0..n 으로 일괄 갱신한다.

    본인 소유 전략만 갱신하며 타인/존재하지 않는 ID 는 무시한다.
    """
    owned = {
        s.id: s
        for s in await db.scalars(
            select(Strategy).where(Strategy.user_id == current.id)
        )
    }
    for order, sid in enumerate(payload.ordered_ids):
        s = owned.get(sid)
        if s is not None:
            s.sort_order = order
    await db.commit()


@router.post("/{strategy_id}/share", response_model=StrategyOut)
async def share_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략을 공유 상태로 전환한다(공유 시각 기록)."""
    s = await _get_owned(db, current, strategy_id)
    s.is_shared = True
    s.shared_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(s)
    return s


@router.delete("/{strategy_id}/share", response_model=StrategyOut)
async def unshare_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략 공유를 해제한다. 기존 좋아요는 좋아요 테이블에 남아 재공유 시 복원된다."""
    s = await _get_owned(db, current, strategy_id)
    s.is_shared = False
    s.shared_at = None
    await db.commit()
    await db.refresh(s)
    return s


@router.post("/{strategy_id}/favorite", response_model=StrategyOut)
async def favorite_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략을 즐겨찾기로 표시한다(목록 상단 고정)."""
    s = await _get_owned(db, current, strategy_id)
    s.is_favorite = True
    await db.commit()
    await db.refresh(s)
    return s


@router.delete("/{strategy_id}/favorite", response_model=StrategyOut)
async def unfavorite_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략 즐겨찾기를 해제한다."""
    s = await _get_owned(db, current, strategy_id)
    s.is_favorite = False
    await db.commit()
    await db.refresh(s)
    return s


async def _get_shared_or_owned(
    db: AsyncSession, user: User, strategy_id: int
) -> Strategy:
    """공유 상태이거나 본인 소유인 전략만 반환한다(아니면 404)."""
    s = await db.scalar(select(Strategy).where(Strategy.id == strategy_id))
    if s is None or not (s.is_shared or s.user_id == user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")
    return s


@router.post(
    "/{strategy_id}/copy",
    response_model=StrategyOut,
    status_code=status.HTTP_201_CREATED,
)
async def copy_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """공유 전략(또는 본인 전략)을 내 전략으로 복사한다.

    설정만 복제하고 공유/좋아요/즐겨찾기는 초기화하며, draft 상태로 저장한다.
    """
    src = await _get_shared_or_owned(db, current, strategy_id)
    copy = Strategy(
        user_id=current.id,
        name=f"{src.name} (사본)",
        description=src.description,
        config=dict(src.config),
        status=StrategyStatus.DRAFT,
        copied_from_id=src.id,
    )
    db.add(copy)
    await db.commit()
    await db.refresh(copy)
    return copy


@router.post("/{strategy_id}/like", response_model=LikeOut)
async def like_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """공유 전략에 좋아요를 누른다(인당 1회, 멱등). 본인 전략엔 불가."""
    s = await db.scalar(select(Strategy).where(Strategy.id == strategy_id))
    if s is None or not s.is_shared:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")
    if s.user_id == current.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "자신의 전략에는 좋아요를 누를 수 없습니다.")
    exists = await db.scalar(
        select(StrategyLike).where(
            StrategyLike.strategy_id == strategy_id,
            StrategyLike.user_id == current.id,
        )
    )
    if exists is None:
        db.add(StrategyLike(strategy_id=strategy_id, user_id=current.id))
        await db.commit()
    return LikeOut(like_count=await _like_count(db, strategy_id), liked_by_me=True)


@router.delete("/{strategy_id}/like", response_model=LikeOut)
async def unlike_strategy(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """좋아요를 취소한다(없으면 무시)."""
    await db.execute(
        delete(StrategyLike).where(
            StrategyLike.strategy_id == strategy_id,
            StrategyLike.user_id == current.id,
        )
    )
    await db.commit()
    return LikeOut(like_count=await _like_count(db, strategy_id), liked_by_me=False)


@router.put("/{strategy_id}/featured-backtest", response_model=StrategyOut)
async def set_featured_backtest(
    strategy_id: int,
    payload: FeaturedBacktestIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략의 대표 백테스트를 지정/해제한다. 지정 시 해당 백테스트가 이 전략 소속이어야 한다."""
    s = await _get_owned(db, current, strategy_id)
    if payload.backtest_id is not None:
        bt = await db.scalar(
            select(Backtest).where(
                Backtest.id == payload.backtest_id,
                Backtest.strategy_id == strategy_id,
            )
        )
        if bt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "백테스트를 찾을 수 없습니다.")
    s.featured_backtest_id = payload.backtest_id
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
    """전략의 이름/설명/설정을 부분 갱신한다(전달된 필드만)."""
    s = await _get_owned(db, current, strategy_id)
    if payload.name is not None:
        s.name = payload.name
    if payload.description is not None:
        # 빈 문자열은 미설정(None)으로 정규화.
        s.description = payload.description.strip() or None
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
