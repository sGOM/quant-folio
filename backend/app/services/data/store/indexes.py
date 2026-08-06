"""index_ohlcv·index_constituents 읽기/쓰기 — 지수 일봉과 PIT 지수구성."""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.local_store_db import LocalStoreSession, run_sync
from app.models.store import IndexConstituent, IndexOhlcv
from app.services.data.store.coerce import INTEGER, NUMERIC, TEXT, coerce_value

logger = logging.getLogger("app.services.data.store")

#: 지수 OHLCV 의 표준 컬럼 순서 — _fetch_index_ohlcv 의 한글→영문 변환 결과와 같다.
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "trading_value"]

#: 테이블 컬럼 → 저장 타입. 변환 규칙은 coerce 모듈이 갖는다(Task 5 에서 만든 공용 헬퍼).
_KINDS = {
    "open": NUMERIC, "high": NUMERIC, "low": NUMERIC, "close": NUMERIC,
    "volume": INTEGER, "trading_value": INTEGER,
}


def write_index_ohlcv(
    index_code: str, df: pd.DataFrame, *, index_name: str | None = None
) -> None:
    """날짜 인덱스 DataFrame 을 index_ohlcv 에 upsert 한다."""
    if df is None or df.empty:
        return
    rows: list[dict] = []
    for ts, r in df.iterrows():
        row: dict = {
            "index_code": index_code,
            "trade_date": pd.Timestamp(ts).date(),
            "index_name": index_name,
        }
        for col in OHLCV_COLUMNS:
            row[col] = (
                coerce_value(r[col], _KINDS.get(col, TEXT)) if col in df.columns else None
            )
        rows.append(row)

    run_sync(_upsert_ohlcv(rows))
    logger.debug("index_ohlcv upsert: %s rows=%d", index_code, len(rows))


async def _upsert_ohlcv(rows: list[dict]) -> None:
    async with LocalStoreSession() as db:
        stmt = pg_insert(IndexOhlcv).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["index_code", "trade_date"],
            set_={
                col: func.coalesce(getattr(stmt.excluded, col), getattr(IndexOhlcv, col))
                for col in OHLCV_COLUMNS + ["index_name"]
            },
        )
        await db.execute(stmt)
        await db.commit()


def read_index_ohlcv(index_code: str, start: date, end: date) -> pd.DataFrame:
    """그 지수의 [start, end] 일봉을 날짜 인덱스 DataFrame 으로 읽는다."""
    records = run_sync(_select_ohlcv(index_code, start, end))
    if not records:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    data = {
        col: [rec[i + 1] for rec in records] for i, col in enumerate(OHLCV_COLUMNS)
    }
    out = pd.DataFrame(data, index=pd.to_datetime([rec[0] for rec in records]))
    for col in OHLCV_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_index()


async def _select_ohlcv(index_code: str, start: date, end: date) -> list[tuple]:
    cols = [IndexOhlcv.trade_date] + [getattr(IndexOhlcv, c) for c in OHLCV_COLUMNS]
    async with LocalStoreSession() as db:
        result = await db.execute(
            select(*cols).where(
                IndexOhlcv.index_code == index_code,
                IndexOhlcv.trade_date >= start,
                IndexOhlcv.trade_date <= end,
            )
        )
        return [tuple(r) for r in result.all()]


def delete_index_ohlcv(index_code: str) -> None:
    """그 지수의 일봉 전체 삭제 — 테스트 정리용."""
    run_sync(_delete_ohlcv(index_code))


async def _delete_ohlcv(index_code: str) -> None:
    async with LocalStoreSession() as db:
        await db.execute(delete(IndexOhlcv).where(IndexOhlcv.index_code == index_code))
        await db.commit()


def write_constituents(index_code: str, base_date: date, symbols: list[str]) -> None:
    """그 시점 지수 구성종목을 저장한다(빈 목록이면 아무것도 쓰지 않는다).

    빈 목록을 '기록 없음'과 구분하는 일은 원장(external_fetches)이 맡는다.
    """
    if not symbols:
        return
    rows = [
        {"index_code": index_code, "base_date": base_date, "symbol": str(s).zfill(6)}
        for s in symbols
    ]
    run_sync(_upsert_constituents(rows))
    logger.debug("index_constituents upsert: %s %s n=%d", index_code, base_date, len(rows))


async def _upsert_constituents(rows: list[dict]) -> None:
    async with LocalStoreSession() as db:
        stmt = pg_insert(IndexConstituent).values(rows)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["index_code", "base_date", "symbol"]
        )
        await db.execute(stmt)
        await db.commit()


def read_constituents(index_code: str, base_date: date) -> list[str]:
    """그 시점 지수 구성종목 코드 목록. 적재되지 않았으면 빈 목록."""
    return run_sync(_select_constituents(index_code, base_date))


async def _select_constituents(index_code: str, base_date: date) -> list[str]:
    async with LocalStoreSession() as db:
        result = await db.execute(
            select(IndexConstituent.symbol).where(
                IndexConstituent.index_code == index_code,
                IndexConstituent.base_date == base_date,
            )
        )
        return [r[0] for r in result.all()]


def delete_constituents(index_code: str, base_date: date) -> None:
    """그 시점 구성종목 삭제 — 테스트 정리용."""
    run_sync(_delete_constituents(index_code, base_date))


async def _delete_constituents(index_code: str, base_date: date) -> None:
    async with LocalStoreSession() as db:
        await db.execute(
            delete(IndexConstituent).where(
                IndexConstituent.index_code == index_code,
                IndexConstituent.base_date == base_date,
            )
        )
        await db.commit()
