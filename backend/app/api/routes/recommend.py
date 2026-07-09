"""추천 화면 라우트 — KOSPI200 팩터 가중 추천 API.

  GET /api/recommend/kospi200 — 현 시점 KOSPI200 구성종목 전체의 팩터별 세부점수·
    메타데이터를 반환한다. 종합점수 가중합·Top-N 선정은 프론트가 사용자 팩터 비중으로
    수행하므로(슬라이더 실시간 반영), 이 API 는 가중치 파라미터를 받지 않고 세부점수만
    내려준다. 무거운 KRX/OpenDART 조회는 as_of 당 1회만 하고 Redis 캐시(6시간)한다.

metrics/screener 라우트와 동일한 캐시·스레드풀 패턴을 따른다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.api.deps import get_current_user
from app.core.redis import get_redis
from app.models import User
from app.schemas.recommend import RecommendOut
from app.services.metrics import _last_business_day
from app.services.recommend import RECOMMEND_CACHE_TTL, compute_kospi200_scored

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recommend", tags=["recommend"])


@router.get(
    "/kospi200",
    response_model=RecommendOut,
    summary="KOSPI200 팩터 추천(세부점수)",
    description=(
        "현 시점 KOSPI200 구성종목 전체의 팩터별 세부점수(모멘텀·밸류·저변동·퀄리티·성장)와 "
        "메타데이터를 반환한다. 세부점수는 KOSPI200 크로스섹션 z-score 로 가중치와 무관하며, "
        "종합점수 가중합·Top-N 선정은 클라이언트가 사용자 비중으로 수행한다. "
        "결과는 as_of(전 영업일) 기준으로 Redis 에 캐시된다(TTL 6h)."
    ),
)
async def kospi200(
    _user: Annotated[User, Depends(get_current_user)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> RecommendOut:
    """KOSPI200 구성종목의 팩터별 세부점수를 반환한다."""
    as_of = await asyncio.to_thread(_last_business_day)
    cache_key = f"recommend:kospi200:{as_of.isoformat()}"

    try:
        cached = await redis.get(cache_key)
    except Exception:
        logger.warning("Redis get 실패 (key=%s)", cache_key, exc_info=True)
        cached = None
    if cached:
        try:
            return RecommendOut.model_validate_json(cached)
        except Exception:
            logger.warning("추천 캐시 역직렬화 실패, 재계산", exc_info=True)

    data = await asyncio.to_thread(compute_kospi200_scored, as_of)

    # 빈 결과(KRX 미인증·스로틀링 등)는 캐시하지 않는다 — 일시적 실패가 TTL 동안 굳는 것 방지.
    if data.items:
        try:
            await redis.setex(cache_key, RECOMMEND_CACHE_TTL, data.model_dump_json())
        except Exception:
            logger.warning("추천 캐시 저장 실패", exc_info=True)
    return data
