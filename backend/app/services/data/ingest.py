"""일봉 로컬 적재(C-1) — KOSPI200 + 등록 전략 유니버스를 price_ticks 에 증분 적재.

야간 배치(worker.ingest_daily_ohlcv, Celery beat)가 이 모듈의 build_universe·
ensure_ohlcv_coverage 를 묶어 실행한다. 백테스트·팩터 계산이 매번 pykrx 실시간 조회에
의존하던 구조를 로컬 우선(price_ticks)으로 바꿔, 외부 가용성 장애(KRX 지연·FDR/pykrx
불안정)가 곧바로 백테스트 가용성 저하로 이어지던 문제를 완화한다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Strategy
from app.services.data.errors import DataSourceError

logger = logging.getLogger(__name__)


async def build_universe(db: AsyncSession, index: str = "KOSPI200") -> list[str]:
    """야간 적재 대상 종목코드 집합을 만든다.

    KOSPI200(오늘 시점) 구성종목 ∪ 등록된 모든 전략의 config 에 등장하는 종목코드
    (단일종목 전략의 symbol, 리밸런싱 전략의 universe 고정 후보풀)를 합집합한다.
    universe_rule.source 가 지수(PIT 동적 유니버스)인 리밸런싱 전략은 그 시점마다
    구성종목이 달라지므로 여기서는 다루지 않는다 — 어차피 KOSPI200 조회로 큰 틀에서
    커버되고, 그 외 지수는 백테스트 시점에 pool_provider 가 온디맨드로 해소한다.
    """
    codes: set[str] = set()
    try:
        from app.services.data.krx_index import index_members

        codes.update(await asyncio.to_thread(index_members, date.today(), index))
    except DataSourceError as e:
        logger.error("KOSPI200 구성종목 조회 실패(그 부분만 제외): %s", e)

    rows = await db.scalars(select(Strategy.config))
    for cfg in rows:
        if not cfg:
            continue
        if cfg.get("type") == "rebalance":
            codes.update(cfg.get("universe") or [])
        else:
            sym = cfg.get("symbol")
            if sym:
                codes.add(sym)

    return sorted(codes)
