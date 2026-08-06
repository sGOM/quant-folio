"""stock_period_stats 읽기/쓰기 — 기간키(start~end) 종목 통계.

기간 등락률은 일봉에서 재유도할 수 없다. pykrx 기간 등락률은 수정주가 기준이라
price_ticks 종가로 다시 계산하면 액면분할·유상증자 구간에서 값이 갈린다. 원본 그대로
보관하는 이유다.

investors 는 투자자군 조합(정렬 후 ',' 조인). 등락률 행은 '', 순매수 행은
'기관합계,외국인' 처럼 서로 다른 키를 써서 덮어쓰지 않는다.
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.local_store_db import LocalStoreSession, run_sync
from app.models.store import StockPeriodStat
from app.services.data.store.coerce import INTEGER, NUMERIC, TEXT, coerce_value

logger = logging.getLogger("app.services.data.store")

#: 테이블 컬럼 → 저장 타입. 변환 규칙은 coerce 모듈이 갖는다(Task 5 에서 만든 공용 헬퍼).
_KINDS = {
    "change_pct": NUMERIC, "open": NUMERIC, "close": NUMERIC,
    "net_buy_value": NUMERIC,
    "volume": INTEGER, "trading_value": INTEGER,
    "market": TEXT,
}


def write_periods(
    start: date, end: date, investors: str, df: pd.DataFrame, *, columns: dict[str, str]
) -> None:
    """티커 인덱스 DataFrame 을 stock_period_stats 에 upsert 한다."""
    if df is None or df.empty:
        return
    present = {src: dst for src, dst in columns.items() if src in df.columns}
    if not present:
        return

    rows: list[dict] = []
    for ticker, r in df.iterrows():
        row: dict = {
            "start_date": start, "end_date": end,
            "investors": investors, "symbol": str(ticker).zfill(6),
        }
        for src, dst in present.items():
            row[dst] = coerce_value(r[src], _KINDS.get(dst, TEXT))
        rows.append(row)

    run_sync(_upsert(rows, list(present.values())))
    logger.debug("stock_period_stats upsert: %s~%s rows=%d", start, end, len(rows))


async def _upsert(rows: list[dict], target_cols: list[str]) -> None:
    async with LocalStoreSession() as db:
        stmt = pg_insert(StockPeriodStat).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["start_date", "end_date", "investors", "symbol"],
            set_={
                col: func.coalesce(
                    getattr(stmt.excluded, col), getattr(StockPeriodStat, col)
                )
                for col in target_cols
            },
        )
        await db.execute(stmt)
        await db.commit()


def read_periods(
    start: date, end: date, investors: str,
    table_columns: list[str], *, out_columns: dict[str, str],
) -> pd.DataFrame:
    """그 기간·투자자군의 지정 컬럼을 티커 인덱스 DataFrame 으로 읽는다."""
    records = run_sync(_select(start, end, investors, table_columns))
    names = [out_columns[c] for c in table_columns]
    if not records:
        empty = pd.DataFrame(columns=names)
        empty.index.name = "티커"
        return empty

    data = {
        out_columns[c]: [rec[i + 1] for rec in records]
        for i, c in enumerate(table_columns)
    }
    out = pd.DataFrame(data, index=[rec[0] for rec in records])
    out.index.name = "티커"
    for c in table_columns:
        name = out_columns[c]
        if c != "market":
            out[name] = pd.to_numeric(out[name], errors="coerce")
    return out


async def _select(
    start: date, end: date, investors: str, table_columns: list[str]
) -> list[tuple]:
    cols = [StockPeriodStat.symbol] + [getattr(StockPeriodStat, c) for c in table_columns]
    async with LocalStoreSession() as db:
        result = await db.execute(
            select(*cols).where(
                StockPeriodStat.start_date == start,
                StockPeriodStat.end_date == end,
                StockPeriodStat.investors == investors,
            )
        )
        return [tuple(r) for r in result.all()]


def delete_periods(start: date, end: date, investors: str) -> None:
    """그 기간·투자자군 행 전체 삭제 — 테스트 정리·재적재용."""
    run_sync(_delete(start, end, investors))


async def _delete(start: date, end: date, investors: str) -> None:
    async with LocalStoreSession() as db:
        await db.execute(
            delete(StockPeriodStat).where(
                StockPeriodStat.start_date == start,
                StockPeriodStat.end_date == end,
                StockPeriodStat.investors == investors,
            )
        )
        await db.commit()
