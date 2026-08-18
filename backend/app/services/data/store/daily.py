"""stock_daily_snapshots 읽기/쓰기 — 거래일 × 종목 격자.

펀더멘털·시가총액·전종목 OHLCV 세 조회가 같은 행을 서로 다른 시점에 채운다. 그래서
upsert 는 **들어온 값이 NULL 이면 기존값을 보존**한다. 그러지 않으면 시총만 적재된
행을 펀더멘털 적재가 NULL 로 덮어 앞선 작업을 날린다.
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.local_store_db import LocalStoreSession, run_sync
from app.models.store import StockDailySnapshot
from app.services.data.store.coerce import (
    INTEGER,
    NUMERIC,
    TEXT,
    _TEXT_COLUMNS,
    coerce_value,
)

logger = logging.getLogger("app.services.data.store")

#: 테이블 컬럼 → 저장 타입. 변환 규칙 자체는 coerce 모듈이 갖는다.
_KINDS = {
    "per": NUMERIC, "pbr": NUMERIC, "div": NUMERIC,
    "open": NUMERIC, "high": NUMERIC, "low": NUMERIC, "close": NUMERIC,
    "change_pct": NUMERIC,
    "market_cap": INTEGER, "shares": INTEGER,
    "volume": INTEGER, "trading_value": INTEGER,
    "market": TEXT,
}


def write_daily(trade_day: date, df: pd.DataFrame, *, columns: dict[str, str]) -> None:
    """티커 인덱스 DataFrame 을 stock_daily_snapshots 에 upsert 한다.

    :param columns: {DataFrame 컬럼명: 테이블 컬럼명}. df 에 없는 키는 건너뛴다.
    """
    if df is None or df.empty:
        return

    present = {src: dst for src, dst in columns.items() if src in df.columns}
    if not present:
        return

    if df.index.has_duplicates:
        # 같은 (trade_date, symbol) 이 한 다중행 INSERT 안에 두 번 들어가면 Postgres 가
        # "ON CONFLICT DO UPDATE command cannot affect row a second time" 로 거부해
        # 그 값이 DataSourceError 가 아니므로 상위 예외 처리에 안 잡히고 호출자를
        # 그대로 크래시시킨다(예: 시장전환일 등으로 같은 티커가 KOSPI/KOSDAQ 양쪽
        # 응답에 함께 실려 concat 된 경우). INSERT 구성 전에 미리 정리한다.
        dupes = int(df.index.duplicated().sum())
        logger.warning(
            "stock_daily_snapshots 중복 티커 %d건 제거(마지막 값 사용): %s",
            dupes, trade_day,
        )
        df = df[~df.index.duplicated(keep="last")]

    rows: list[dict] = []
    for ticker, r in df.iterrows():
        row: dict = {"trade_date": trade_day, "symbol": str(ticker).zfill(6)}
        for src, dst in present.items():
            row[dst] = coerce_value(r[src], _KINDS.get(dst, TEXT))
        rows.append(row)
    if not rows:
        return

    run_sync(_upsert(rows, list(present.values())))
    logger.debug("stock_daily_snapshots upsert: %s rows=%d", trade_day, len(rows))


async def _upsert(rows: list[dict], target_cols: list[str]) -> None:
    async with LocalStoreSession() as db:
        stmt = pg_insert(StockDailySnapshot).values(rows)
        # 들어온 값이 NULL 이면 기존값을 보존한다 — 다른 소스가 채운 컬럼을 지우지 않기 위함.
        stmt = stmt.on_conflict_do_update(
            index_elements=["trade_date", "symbol"],
            set_={
                col: func.coalesce(
                    getattr(stmt.excluded, col), getattr(StockDailySnapshot, col)
                )
                for col in target_cols
            },
        )
        await db.execute(stmt)
        await db.commit()


def read_daily(
    trade_day: date,
    table_columns: list[str],
    *,
    out_columns: dict[str, str],
    markets: list[str] | None = None,
) -> pd.DataFrame:
    """그 거래일의 지정 컬럼을 티커 인덱스 DataFrame 으로 읽는다.

    :param out_columns: {테이블 컬럼명: 반환 DataFrame 컬럼명}
    :param markets: None 이 아니면 이 시장 목록으로만 필터링한다. KOSPI 행과 KOSDAQ
        행이 (trade_date, symbol) 같은 키공간에 섞여 저장되므로, 단일시장 조회는
        반드시 이 필터를 걸어야 한다(안 그러면 전 시장 적재 이후 결과가 오염된다).
    """
    records = run_sync(_select(trade_day, table_columns, markets))
    names = [out_columns[c] for c in table_columns]
    if not records:
        empty = pd.DataFrame(columns=names)
        empty.index.name = "티커"
        return empty

    data = {out_columns[c]: [rec[i + 1] for rec in records] for i, c in enumerate(table_columns)}
    out = pd.DataFrame(data, index=[rec[0] for rec in records])
    out.index.name = "티커"
    for c in table_columns:
        name = out_columns[c]
        if c not in _TEXT_COLUMNS:
            out[name] = pd.to_numeric(out[name], errors="coerce")
    return out


async def _select(
    trade_day: date, table_columns: list[str], markets: list[str] | None = None
) -> list[tuple]:
    cols = [StockDailySnapshot.symbol] + [
        getattr(StockDailySnapshot, c) for c in table_columns
    ]
    async with LocalStoreSession() as db:
        stmt = select(*cols).where(StockDailySnapshot.trade_date == trade_day)
        if markets:
            stmt = stmt.where(StockDailySnapshot.market.in_(markets))
        result = await db.execute(stmt)
        return [tuple(r) for r in result.all()]


def delete_daily(trade_day: date) -> None:
    """그 거래일 행 전체 삭제 — 테스트 정리·재적재용."""
    run_sync(_delete(trade_day))


async def _delete(trade_day: date) -> None:
    async with LocalStoreSession() as db:
        await db.execute(
            delete(StockDailySnapshot).where(StockDailySnapshot.trade_date == trade_day)
        )
        await db.commit()
