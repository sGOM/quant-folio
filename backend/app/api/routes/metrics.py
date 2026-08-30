"""지표 화면 라우트 — 섹터·종목 지표 API.

엔드포인트:
  GET /api/metrics/sectors  — 지표가 좋은 섹터(업종) 목록
  GET /api/metrics/stocks   — 지표가 좋은 종목 목록

캐싱 전략:
  - Redis 키: metrics:{sectors|stocks}:{market}:{as_of ISO}
  - TTL: 약 6시간 (EOD 스냅샷은 장 마감 후 변하지 않음)
  - 캐시 미스 시 asyncio.to_thread 로 블로킹 계산 수행
  - 캐시는 sort/filter/limit 없이 전체 데이터를 저장 → 라우트에서 정렬·필터·제한 적용

코딩 스타일: symbols.py 라우트와 동일한 패턴을 따른다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis

from app.api.deps import get_current_user
from app.core.redis import get_redis
from app.models import User
from app.schemas.metrics import (
    PanicOut,
    SectorMetric,
    SectorsOut,
    StockMetric,
    StocksOut,
)
from app.services.metrics import (
    PANIC_CACHE_TTL,
    SECTORS_CACHE_TTL,
    STOCKS_CACHE_TTL,
    _last_business_day,
    compute_panic,
    compute_sectors,
    compute_stocks,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

MarketParam = Literal["ALL", "KOSPI", "KOSDAQ"]

# ─────────────────────── 정렬 헬퍼 ───────────────────────

_SECTOR_SORT_KEY = {
    "rs": lambda s: (s.rs_126 is None, -(s.rs_126 or 0.0)),
    "mom_6m": lambda s: (s.mom_6m is None, -(s.mom_6m or 0.0)),
    "mom_3m": lambda s: (s.mom_3m is None, -(s.mom_3m or 0.0)),
    "value_trend": lambda s: (s.value_trend is None, -(s.value_trend or 0.0)),
}

_STOCK_SORT_KEY = {
    "score": lambda s: (s.score is None, -(s.score or 0.0)),
    "mom_6m": lambda s: (s.mom_6m is None, -(s.mom_6m or 0.0)),
    "mom_3m": lambda s: (s.mom_3m is None, -(s.mom_3m or 0.0)),
    # PER/PBR: 낮을수록 우선, null을 뒤로
    "per": lambda s: (s.per is None or s.per <= 0, s.per or float("inf")),
    "pbr": lambda s: (s.pbr is None, s.pbr or float("inf")),
    # DIV: 높을수록 우선
    "div": lambda s: (s.div is None, -(s.div or 0.0)),
}


# ─────────────────────── 섹터 엔드포인트 ───────────────────────


@router.get(
    "/sectors",
    response_model=SectorsOut,
    summary="섹터(업종) 지표 조회",
    description=(
        "pykrx KRX 업종지수 기반 섹터 지표를 반환한다. "
        "모멘텀·RS·변동성·거래대금추세·MDD 등을 포함한다. "
        "결과는 as_of(전 영업일) + market 기준으로 Redis에 캐시된다(TTL 6h). "
        "비율(수익률·변동성·MDD)은 소수 단위."
    ),
)
async def list_sectors(
    market: Annotated[MarketParam, Query(description="시장 구분(ALL/KOSPI/KOSDAQ)")] = "ALL",
    sort: Annotated[
        Literal["rs", "mom_6m", "mom_3m", "value_trend"],
        Query(description="정렬 기준(기본: rs)")
    ] = "rs",
    _: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> SectorsOut:
    """지표가 좋은 섹터 목록을 반환한다.

    - 캐시 미스 시 pykrx를 통해 업종지수·구성종목·펀더멘털을 조회해 계산한다.
    - 계산은 블로킹이므로 스레드풀에서 실행 후 정렬을 적용한다.
    """
    as_of = await asyncio.to_thread(_last_business_day)
    cache_key = f"metrics:sectors:{market}:{as_of.isoformat()}"

    try:
        cached = await redis.get(cache_key)
    except Exception:
        logger.warning("Redis get 실패 (key=%s)", cache_key, exc_info=True)
        cached = None

    if cached:
        try:
            data = SectorsOut.model_validate_json(cached)
        except Exception:
            logger.warning("섹터 캐시 역직렬화 실패, 재계산", exc_info=True)
            data = None
    else:
        data = None

    if data is None:
        try:
            data = await asyncio.to_thread(compute_sectors, market, as_of)
        except Exception as exc:
            logger.error("섹터 지표 계산 실패: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="섹터 지표 계산 중 오류가 발생했습니다.",
            ) from exc

        # 빈 결과(KRX 스로틀링·데이터 미발행 등)는 캐시하지 않는다 — 일시적 실패가
        # TTL(6h) 동안 굳어 정상 데이터를 가리는 것을 방지.
        if data.items:
            try:
                await redis.setex(cache_key, SECTORS_CACHE_TTL, data.model_dump_json())
            except Exception:
                logger.warning("섹터 캐시 저장 실패", exc_info=True)
        else:
            logger.warning("섹터 계산 결과가 비어 캐시를 건너뜀 (as_of=%s market=%s)", as_of, market)

    # 정렬 적용 (null을 뒤로)
    sort_fn = _SECTOR_SORT_KEY.get(sort, _SECTOR_SORT_KEY["rs"])
    sorted_items: list[SectorMetric] = sorted(data.items, key=sort_fn)

    return SectorsOut(as_of=data.as_of, items=sorted_items)


# ─────────────────────── 종목 엔드포인트 ───────────────────────


@router.get(
    "/stocks",
    response_model=StocksOut,
    summary="종목 지표 조회",
    description=(
        "pykrx 일괄 조회 기반 종목 지표를 반환한다. "
        "가치·모멘텀·추세·리스크·유동성 지표와 종합 점수(score)를 포함한다. "
        "기술지표(RSI/vol/MDD/정배열)는 유동성·시총 필터 통과 후보군(≤200종목)에 대해서만 계산한다. "
        "비율은 소수, 배당수익률(div)은 퍼센트 단위."
    ),
)
async def list_stocks(
    market: Annotated[MarketParam, Query(description="시장 구분(ALL/KOSPI/KOSDAQ)")] = "ALL",
    sort: Annotated[
        Literal["score", "mom_6m", "mom_3m", "per", "pbr", "div"],
        Query(description="정렬 기준(기본: score)")
    ] = "score",
    min_value: Annotated[
        int,
        Query(ge=0, description="20일 평균 거래대금 하한(원, 기본 5억)")
    ] = 500_000_000,
    min_mcap: Annotated[
        int,
        Query(ge=0, description="시가총액 하한(원, 기본 1,000억)")
    ] = 100_000_000_000,
    limit: Annotated[int, Query(ge=1, le=200, description="반환 종목 수(기본 50, 최대 200)")] = 50,
    _: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> StocksOut:
    """지표가 좋은 종목 목록을 반환한다.

    - 캐시는 market + as_of 기준으로 저장(min_value/min_mcap/limit 제외).
      캐시 데이터는 기본 필터(5억/1,000억)로 산출되었으므로, 더 엄격한 조건은
      라우트에서 추가 필터링한다.
    - 종합 점수는 크로스섹션 내에서 계산되므로 limit에 따라 달라지지 않는다.
    """
    as_of = await asyncio.to_thread(_last_business_day)
    cache_key = f"metrics:stocks:{market}:{as_of.isoformat()}"

    try:
        cached = await redis.get(cache_key)
    except Exception:
        logger.warning("Redis get 실패 (key=%s)", cache_key, exc_info=True)
        cached = None

    if cached:
        try:
            data = StocksOut.model_validate_json(cached)
        except Exception:
            logger.warning("종목 캐시 역직렬화 실패, 재계산", exc_info=True)
            data = None
    else:
        data = None

    if data is None:
        try:
            data = await asyncio.to_thread(compute_stocks, market, as_of)
        except Exception as exc:
            logger.error("종목 지표 계산 실패: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="종목 지표 계산 중 오류가 발생했습니다.",
            ) from exc

        # 빈 결과(KRX 스로틀링·데이터 미발행 등)는 캐시하지 않는다.
        if data.items:
            try:
                await redis.setex(cache_key, STOCKS_CACHE_TTL, data.model_dump_json())
            except Exception:
                logger.warning("종목 캐시 저장 실패", exc_info=True)
        else:
            logger.warning("종목 계산 결과가 비어 캐시를 건너뜀 (as_of=%s market=%s)", as_of, market)

    # 요청 파라미터로 추가 필터 적용 (캐시 기본값보다 엄격한 경우)
    items: list[StockMetric] = data.items
    # 결측(None)은 기준 충족을 증명할 수 없으므로 더 엄격한 필터에서 탈락시킨다.
    if min_value > 500_000_000:
        items = [s for s in items if s.avg_value_20 is not None and s.avg_value_20 >= min_value]
    if min_mcap > 100_000_000_000:
        items = [s for s in items if s.market_cap is not None and s.market_cap >= min_mcap]

    # 정렬 (null을 뒤로)
    sort_fn = _STOCK_SORT_KEY.get(sort, _STOCK_SORT_KEY["score"])
    items = sorted(items, key=sort_fn)

    # limit 적용
    items = items[:limit]

    return StocksOut(as_of=data.as_of, count=len(items), items=items)


# ─────────────────────── 패닉셀 엔드포인트 ───────────────────────


@router.get(
    "/panic",
    response_model=PanicOut,
    summary="패닉셀(투매) 지표 조회",
    description=(
        "시장(KOSPI/KOSDAQ) 레벨 패닉셀(자본항복) 판정을 반환한다. "
        "지수 급락·거래대금 폭증·브레드스 붕괴·변동성 급등을 결합해 0~100 점수와 "
        "'정상/주의/경계/패닉' 라벨을 산출한다(게이팅으로 단순 조정 오탐 억제). "
        "직전 확정 영업일(as_of) 종가 기준이며 장중 실시간 신호가 아니다. "
        "결과는 as_of + market 기준으로 Redis에 캐시된다(TTL 6h)."
    ),
)
async def get_panic(
    market: Annotated[MarketParam, Query(description="시장 구분(ALL/KOSPI/KOSDAQ)")] = "ALL",
    _: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> PanicOut:
    """패닉셀 시장 지표를 반환한다.

    - 캐시 미스 시 pykrx로 지수 OHLCV·전 종목 등락률을 조회해 계산한다(블로킹 → 스레드풀).
    - 빈 결과(조회 실패)는 캐시하지 않아 일시적 실패가 TTL 동안 굳는 것을 방지한다.
    """
    as_of = await asyncio.to_thread(_last_business_day)
    cache_key = f"metrics:panic:{market}:{as_of.isoformat()}"

    try:
        cached = await redis.get(cache_key)
    except Exception:
        logger.warning("Redis get 실패 (key=%s)", cache_key, exc_info=True)
        cached = None

    if cached:
        try:
            return PanicOut.model_validate_json(cached)
        except Exception:
            logger.warning("패닉 캐시 역직렬화 실패, 재계산", exc_info=True)

    try:
        data = await asyncio.to_thread(compute_panic, market, as_of)
    except Exception as exc:
        logger.error("패닉 지표 계산 실패: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="패닉셀 지표 계산 중 오류가 발생했습니다.",
        ) from exc

    if data.items:
        try:
            await redis.setex(cache_key, PANIC_CACHE_TTL, data.model_dump_json())
        except Exception:
            logger.warning("패닉 캐시 저장 실패", exc_info=True)
    else:
        logger.warning("패닉 계산 결과가 비어 캐시를 건너뜀 (as_of=%s market=%s)", as_of, market)

    return data
