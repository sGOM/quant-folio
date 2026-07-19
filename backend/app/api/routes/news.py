"""경제 뉴스 라우트 (docs/ROADMAP.md M3).

worker.ingest_news 가 적재한 news_articles 를 조회한다. 표시는 요약+원문 링크 방식
(본문 전재 없음). symbol 쿼리로 "이 종목의 관련 기사"(news_article_symbols 역인덱스)를
필터링한다 — 종목 상세/스크리너의 관련 기사 표시가 이 경로를 쓴다.

엔드포인트:
  GET /api/news                — 최신 기사 목록(발행시각 내림차순, symbol·제목·날짜 필터·페이지네이션).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import NewsArticle, NewsArticleSymbol, User

router = APIRouter(prefix="/api/news", tags=["news"])

# 발행시각은 UTC 로 저장되지만 사용자는 KST 날짜로 필터링한다.
_KST = timezone(timedelta(hours=9))


class NewsItemOut(BaseModel):
    id: int
    source: str
    title: str
    url: str
    summary: str | None
    published_at: datetime
    symbols: list[str]

    model_config = {"from_attributes": True}


class NewsListOut(BaseModel):
    items: list[NewsItemOut]
    has_more: bool


@router.get("", response_model=NewsListOut)
async def list_news(
    symbol: Annotated[str | None, Query(max_length=20)] = None,
    q: Annotated[str | None, Query(max_length=100, description="제목 검색어(부분일치)")] = None,
    date_from: Annotated[date | None, Query(description="발행일 시작(KST, 포함)")] = None,
    date_to: Annotated[date | None, Query(description="발행일 끝(KST, 포함)")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
    _current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NewsListOut:
    stmt = select(NewsArticle)
    if symbol:
        stmt = stmt.join(
            NewsArticleSymbol, NewsArticleSymbol.article_id == NewsArticle.id
        ).where(NewsArticleSymbol.symbol == symbol)
    if q and q.strip():
        # LIKE 와일드카드(%/_)는 리터럴로 취급해 제목 부분일치만 허용한다.
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(NewsArticle.title.ilike(f"%{escaped}%", escape="\\"))
    if date_from:
        stmt = stmt.where(
            NewsArticle.published_at >= datetime.combine(date_from, time.min, _KST)
        )
    if date_to:
        # KST 그 날의 끝까지 포함 → 다음날 0시(KST) 미만.
        stmt = stmt.where(
            NewsArticle.published_at
            < datetime.combine(date_to + timedelta(days=1), time.min, _KST)
        )
    # limit+1 로 다음 페이지 존재 여부를 추가 count 쿼리 없이 판정한다.
    stmt = stmt.order_by(NewsArticle.published_at.desc(), NewsArticle.id.desc()).limit(
        limit + 1
    ).offset(offset)
    articles = (await db.execute(stmt)).scalars().all()
    has_more = len(articles) > limit
    articles = articles[:limit]

    # 기사별 종목 태그를 한 번에 로드(N+1 회피).
    symbols_by_article: dict[int, list[str]] = {}
    if articles:
        rows = await db.execute(
            select(NewsArticleSymbol.article_id, NewsArticleSymbol.symbol).where(
                NewsArticleSymbol.article_id.in_([a.id for a in articles])
            )
        )
        for article_id, sym in rows.all():
            symbols_by_article.setdefault(article_id, []).append(sym)

    return NewsListOut(
        items=[
            NewsItemOut(
                id=a.id,
                source=a.source,
                title=a.title,
                url=a.url,
                summary=a.summary,
                published_at=a.published_at,
                symbols=sorted(symbols_by_article.get(a.id, [])),
            )
            for a in articles
        ],
        has_more=has_more,
    )
