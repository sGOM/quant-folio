"""소형주 턴어라운드 스크리너 라우트.

  GET /api/screener/turnaround — 전 시장 스캔 후 하드 필터를 통과한 턴어라운드 후보

Redis 캐시(6시간). 블로킹 계산은 asyncio.to_thread 로 수행한다(metrics 라우트와 동일 패턴).
빈 결과(KRX 시총 조회 실패 등 일시적 장애)는 캐시하지 않는다 — 일시적 실패가 TTL(6h)
동안 굳어 정상 결과를 가리는 것을 방지한다(metrics.py sectors/stocks/panic 과 동일 정책).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis

from app.api.deps import get_current_user
from app.core.redis import get_redis
from app.models import User
from app.schemas.screener import TurnaroundScreenOut
from app.services.metrics import _last_business_day
from app.services.screener import screen_turnaround

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screener", tags=["screener"])

MarketParam = Literal["ALL", "KOSPI", "KOSDAQ"]
_CACHE_TTL = 6 * 3600


@router.get("/turnaround", response_model=TurnaroundScreenOut)
async def turnaround(
    _user: Annotated[User, Depends(get_current_user)],
    redis: Annotated[Redis, Depends(get_redis)],
    market: MarketParam = "ALL",
    top_n: int = Query(default=20, ge=1, le=100),
    smallcap_pct: float = Query(default=0.20, gt=0, le=1),
    surge: float = Query(default=1.5, ge=1, le=10),
    max_debt: float = Query(default=1.5, gt=0, le=10),
) -> TurnaroundScreenOut:
    """소형주 턴어라운드 후보를 스크리닝한다."""
    as_of = _last_business_day()
    cache_key = (
        f"screener:turnaround:{market}:{as_of.isoformat()}"
        f":{top_n}:{smallcap_pct}:{surge}:{max_debt}"
    )
    cached = await redis.get(cache_key)
    if cached:
        return TurnaroundScreenOut.model_validate_json(cached)

    result = await asyncio.to_thread(
        screen_turnaround, as_of, market,
        top_n=top_n, smallcap_pct=smallcap_pct, surge=surge, max_debt=max_debt,
    )
    # 빈 결과(KRX 시총 조회 실패 등 일시적 장애)는 캐시하지 않는다 — 일시적 실패가
    # TTL(6h) 동안 굳어 정상 데이터를 가리는 것을 방지한다.
    if result.items:
        await redis.set(cache_key, result.model_dump_json(), ex=_CACHE_TTL)
    else:
        logger.warning(
            "턴어라운드 스크리닝 결과가 비어 캐시를 건너뜀 (as_of=%s market=%s)", as_of, market
        )
    return result
