"""배치 작업 태스크 — 일봉 로컬 적재(C-1) 등.

Celery 태스크는 동기 함수라, 비동기 DB 세션을 쓰는 적재 로직은 asyncio.run 으로 감싼다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

from worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# 팩터 워밍업(모멘텀·52주고가·변동성)에 필요한 만큼 과거까지 커버한다.
_LOOKBACK_DAYS = 500


async def _ingest_daily_ohlcv_async() -> dict:
    from app.core.database import AsyncSessionLocal
    from app.services.data.ingest import build_universe
    from app.services.data.loader import ensure_ohlcv_coverage

    end = date.today()
    start = end - timedelta(days=_LOOKBACK_DAYS)
    start_dt = datetime.combine(start, time(0, 0), tzinfo=timezone.utc)
    end_dt = datetime.combine(end, time(23, 59, 59), tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        universe = await build_universe(db)
        failed: list[str] = []
        for sym in universe:
            try:
                await ensure_ohlcv_coverage(db, sym, start_dt, end_dt)
            except Exception as e:  # noqa: BLE001
                failed.append(sym)
                logger.warning("일봉 적재 실패(%s): %s", sym, e)

    result = {"universe": len(universe), "ok": len(universe) - len(failed), "failed": failed}
    logger.info("일봉 로컬 적재 완료: %s", result)
    return result


@celery_app.task(name="worker.ingest_daily_ohlcv")
def ingest_daily_ohlcv() -> dict:
    """KOSPI200 + 등록 전략 유니버스의 일봉을 price_ticks 에 증분 적재한다(C-1, 야간 배치).

    각 종목은 로컬에 이미 있는 만큼은 건너뛰고 부족분(신규 종목 전체 또는 최근 며칠)만
    외부 소스(FDR/pykrx)로 보충한다 — 매일 실행해도 가벼운 이유.
    """
    return asyncio.run(_ingest_daily_ohlcv_async())
