"""알림함 라우트 (docs/improvements.md §17 해소).

engine/alerts.py::publish_alert 가 적재한 alerts 테이블을 조회·확인 처리한다. 요청자
본인 알림(user_id=본인) 뿐 아니라 전역/운영 알림(user_id IS NULL — 배치 작업 실패 등)도
함께 반환한다.

엔드포인트:
  GET  /api/alerts               — 본인 + 전역 알림 목록(최신순, unread_only 필터).
  POST /api/alerts/{id}/read     — 단건 확인 처리.
  POST /api/alerts/read-all      — 전체 확인 처리.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import Alert, User

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertOut(BaseModel):
    id: int
    user_id: int | None
    strategy_id: int
    severity: str
    code: str | None
    message: str
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AlertListOut(BaseModel):
    items: list[AlertOut]
    unread_count: int
    has_more: bool


def _visible_filter(current: User):
    """요청자 본인 알림 또는 전역(user_id IS NULL) 알림만 노출."""
    return or_(Alert.user_id == current.id, Alert.user_id.is_(None))


@router.get("", response_model=AlertListOut)
async def list_alerts(
    unread_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertListOut:
    stmt = select(Alert).where(_visible_filter(current))
    if unread_only:
        stmt = stmt.where(Alert.is_read.is_(False))
    # 다음 페이지 존재 여부를 프론트가 추정("마지막 페이지 길이==limit")하지 않아도
    # 되도록 limit+1건을 조회해 has_more 를 정확히 계산하고 초과분은 잘라낸다.
    stmt = stmt.order_by(Alert.created_at.desc()).limit(limit + 1).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    unread_stmt = select(func.count()).select_from(Alert).where(
        _visible_filter(current), Alert.is_read.is_(False)
    )
    unread_count = (await db.execute(unread_stmt)).scalar_one()

    return AlertListOut(
        items=[AlertOut.model_validate(row) for row in rows],
        unread_count=unread_count,
        has_more=has_more,
    )


@router.post("/{alert_id}/read", response_model=AlertOut)
async def mark_alert_read(
    alert_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AlertOut:
    alert = await db.get(Alert, alert_id)
    if alert is None or (alert.user_id is not None and alert.user_id != current.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "알림을 찾을 수 없습니다.")
    alert.is_read = True
    await db.commit()
    await db.refresh(alert)
    return AlertOut.model_validate(alert)


@router.post("/read-all")
async def mark_all_alerts_read(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    stmt = (
        update(Alert)
        .where(_visible_filter(current), Alert.is_read.is_(False))
        .values(is_read=True)
    )
    result = await db.execute(stmt)
    await db.commit()
    return {"updated": result.rowcount}
