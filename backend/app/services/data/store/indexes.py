"""index_ohlcv·index_constituents 읽기/쓰기 — 지수 일봉과 PIT 지수구성."""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.local_store_db import LocalStoreSession, run_sync
from app.models.store import IndexConstituent, IndexOhlcv, IndexOhlcvCoverage
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
        # 커버리지도 함께 지운다. 행만 지우고 커버 구간이 남으면 "커버됐다는데 행이
        # 없는" 상태가 되고, 그 캐시키는 다음 호출부터 영구히 빈 결과를 돌려준다
        # (마이그레이션 0015 가 고친 것과 같은 형태의 함정).
        await db.execute(
            delete(IndexOhlcvCoverage).where(IndexOhlcvCoverage.index_code == index_code)
        )
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


def read_coverage(index_code: str) -> list[tuple[date, date]]:
    """그 지수의 확보 구간 목록을 (covered_from, covered_to) 오름차순으로 반환한다.

    저장된 구간은 전부 확정분이다(기록 시 마지막 확정일로 잘라 넣는다).
    """
    return run_sync(_select_coverage(index_code))


async def _select_coverage(index_code: str) -> list[tuple[date, date]]:
    async with LocalStoreSession() as db:
        result = await db.execute(
            select(IndexOhlcvCoverage.covered_from, IndexOhlcvCoverage.covered_to)
            .where(IndexOhlcvCoverage.index_code == index_code)
            .order_by(IndexOhlcvCoverage.covered_from)
        )
        return [(r[0], r[1]) for r in result.all()]


def merge_coverage(index_code: str, start: date, end: date) -> None:
    """[start, end] 를 확보 구간에 병합한다. end < start 면 아무것도 하지 않는다.

    겹치거나 하루 맞닿은 기존 구간을 흡수해 한 행으로 대체한다. 주말만큼 벌어진
    구간(금요일 끝 ↔ 월요일 시작)은 병합하지 않는다 — 그 사이에 거래일이 있었는지
    거래일 달력 없이 단정할 수 없기 때문이다. 대가는 구간이 잘게 쪼개지는 것뿐이고,
    잘못 병합해 없는 구간을 커버됐다고 주장하는 쪽이 비교할 수 없이 위험하다.
    """
    if end < start:
        return
    run_sync(_merge_coverage(index_code, start, end))


async def _merge_coverage(index_code: str, start: date, end: date) -> None:
    one = timedelta(days=1)
    async with LocalStoreSession() as db:
        # 아래 select→delete→insert 는 행 잠금이 없다. 같은 index_code 를 동시에
        # 병합하면 두 트랜잭션이 같은 기존 행을 읽고 각자 delete+insert 해, 나중에
        # 커밋한 쪽이 먼저 커밋한 쪽의 확장분을 자신의 낡은 읽기값으로 덮어쓸 수 있다
        # (유실 방향은 항상 좁아지는 쪽이라 거짓 커버리지는 안 생기지만, 불필요한
        # 재조회를 유발한다). 어드바이저리 락으로 같은 index_code 는 한 번에 하나씩만
        # 병합하게 만든다 — 트랜잭션 종료 시 자동 해제, 마이그레이션 불필요.
        await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(index_code))))
        result = await db.execute(
            select(
                IndexOhlcvCoverage.covered_from, IndexOhlcvCoverage.covered_to
            ).where(
                IndexOhlcvCoverage.index_code == index_code,
                # 양방향 조건이어야 한다. 한쪽만 보면(새.from <= 기존.to + 1일) 새
                # 구간이 기존 구간보다 앞설 때 병합이 누락된다.
                IndexOhlcvCoverage.covered_to >= start - one,
                IndexOhlcvCoverage.covered_from <= end + one,
            )
        )
        overlapping = [(r[0], r[1]) for r in result.all()]

        new_from = min([start, *(f for f, _ in overlapping)])
        new_to = max([end, *(t for _, t in overlapping)])

        if overlapping:
            # covered_from 이 PK 의 일부라 UPDATE 로는 경계를 못 옮긴다 — 삭제 후 삽입.
            await db.execute(
                delete(IndexOhlcvCoverage).where(
                    IndexOhlcvCoverage.index_code == index_code,
                    IndexOhlcvCoverage.covered_from.in_([f for f, _ in overlapping]),
                )
            )
        await db.execute(
            pg_insert(IndexOhlcvCoverage)
            .values(index_code=index_code, covered_from=new_from, covered_to=new_to)
            .on_conflict_do_update(
                index_elements=["index_code", "covered_from"],
                set_={"covered_to": new_to, "updated_at": func.now()},
            )
        )
        await db.commit()
