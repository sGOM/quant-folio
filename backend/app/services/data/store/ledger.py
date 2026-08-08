"""페치 원장 — "이 조회를 실제로 해봤는가"의 기록.

정규화 테이블 단독으로는 휴장일(0행)과 미적재(0행)를 구분할 수 없다. 조회 사실
자체를 여기 남겨야 둘이 갈린다. 이 구분이 무너지면 저장소가 §48 이 닫으려던
조용한 실패 모드를 그대로 재현한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.local_store_db import LocalStoreSession, run_sync
from app.models.store import ExternalFetch


@dataclass(frozen=True)
class LedgerEntry:
    """조회 1건의 결과 기록.

    :param row_count: 저장된 행 수. 0 은 "조회했고 정말 데이터가 없었다"는 뜻이다.
    :param final: 확정 여부. False 면 다음 호출에서 재조회한다(당일 시세·미확정 DART).
    """

    row_count: int
    final: bool


class Ledger(Protocol):
    """원장 구현 계약. 프로덕션은 SqlLedger, 테스트는 InMemoryLedger."""

    def get(self, source: str, cache_key: str) -> LedgerEntry | None:
        """기록을 반환한다. 조회한 적이 없으면 None."""
        ...

    def put(self, source: str, cache_key: str, *, row_count: int, final: bool) -> None:
        """기록을 남긴다(같은 키는 덮어쓴다)."""
        ...


class InMemoryLedger:
    """프로세스 메모리 원장 — 테스트 전용."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], LedgerEntry] = {}

    def get(self, source: str, cache_key: str) -> LedgerEntry | None:
        return self._rows.get((source, cache_key))

    def put(self, source: str, cache_key: str, *, row_count: int, final: bool) -> None:
        self._rows[(source, cache_key)] = LedgerEntry(row_count=row_count, final=final)


class SqlLedger:
    """external_fetches 테이블 원장.

    동기 함수다 — 호출자(metrics/fetch.py 계열)가 asyncio.to_thread 워커 스레드에서
    돌기 때문이다. NullPool 전용 엔진 위에서 run_sync 로 코루틴을 실행한다.
    """

    def get(self, source: str, cache_key: str) -> LedgerEntry | None:
        return run_sync(self._get(source, cache_key))

    def put(self, source: str, cache_key: str, *, row_count: int, final: bool) -> None:
        run_sync(self._put(source, cache_key, row_count=row_count, final=final))

    async def _get(self, source: str, cache_key: str) -> LedgerEntry | None:
        async with LocalStoreSession() as db:
            row = await db.scalar(
                select(ExternalFetch).where(
                    ExternalFetch.source == source,
                    ExternalFetch.cache_key == cache_key,
                )
            )
            if row is None:
                return None
            return LedgerEntry(row_count=row.row_count, final=row.final)

    async def _put(
        self, source: str, cache_key: str, *, row_count: int, final: bool
    ) -> None:
        async with LocalStoreSession() as db:
            stmt = pg_insert(ExternalFetch).values(
                source=source, cache_key=cache_key, row_count=row_count, final=final
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["source", "cache_key"],
                set_={
                    "row_count": stmt.excluded.row_count,
                    "final": stmt.excluded.final,
                    "fetched_at": stmt.excluded.fetched_at,
                },
            )
            await db.execute(stmt)
            await db.commit()


_default: Ledger | None = None


def default_ledger() -> Ledger:
    """기본 원장 구현(SqlLedger). 테스트는 이 함수를 monkeypatch 로 갈아끼운다."""
    global _default
    if _default is None:
        _default = SqlLedger()
    return _default
