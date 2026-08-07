"""dart_financials 읽기/쓰기 — OpenDART 재무제표 원계정.

파생지표가 아니라 원계정을 그대로 담는 이유: derive_metrics·piotroski_f_score 가
바뀌면 저장된 파생값은 낡지만 원계정은 안 낡는다.

시장데이터와 달리 DART 는 정정공시가 있다. 그래서 접수일 + 90일이 지나야 확정으로
굳히고, 그 전에는 재조회를 허용한다(설계 §6).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.local_store_db import LocalStoreSession, run_sync
from app.models.store import DartFinancial

logger = logging.getLogger("app.services.data.store")

#: 정정공시 반영 유예(일). 이 기간이 지나면 불변으로 취급한다.
_CONFIRM_LAG_DAYS = 90


def confirmed_date(rcept_dt: date | None, bsns_year: int) -> date:
    """이 보고서를 불변으로 취급해도 되는 날짜.

    접수일을 알면 접수일 + 90일. 모르면 사업연도 말일 + 1년으로 보수적으로 잡는다
    (확정을 앞당기면 정정 전 값이 영구히 굳으므로, 늦추는 쪽이 안전하다).
    """
    if rcept_dt is not None:
        return rcept_dt + timedelta(days=_CONFIRM_LAG_DAYS)
    return date(bsns_year + 1, 12, 31)


def _parse_rcept(accounts: list[dict]) -> tuple[str | None, date | None]:
    """원계정에서 접수번호·접수일을 뽑는다.

    OpenDART 는 행마다 rcept_no(14자리, 앞 8자리가 접수일 YYYYMMDD)를 싣는다.
    행마다 같으므로 첫 유효값을 쓴다.
    """
    for row in accounts:
        raw = str(row.get("rcept_no") or "").strip()
        if len(raw) >= 8 and raw[:8].isdigit():
            try:
                return raw, date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
            except ValueError:
                continue
    return None, None


def write_accounts(
    corp_code: str, bsns_year: int, reprt_code: str, fs_div: str, accounts: list[dict]
) -> None:
    """원계정을 저장한다. 빈 목록은 저장하지 않는다.

    무자료(OpenDART status 013)는 이 함수가 아니라 호출자
    (`app.services.data.opendart.cached_accounts`)가 페치 원장(`external_fetches`,
    source="dart_accounts")에 `row_count=0` 으로 기록한다 — 이 정규화 테이블은
    원계정이 실제로 있을 때만 값을 갖는다.
    """
    if not accounts:
        return
    rcept_no, rcept_dt = _parse_rcept(accounts)
    run_sync(
        _upsert(
            {
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": reprt_code,
                "fs_div": fs_div,
                "accounts": accounts,
                "rcept_no": rcept_no,
                "rcept_dt": rcept_dt,
                "confirmed_at": confirmed_date(rcept_dt, bsns_year),
            }
        )
    )
    logger.debug(
        "dart_financials upsert: %s %s %s %s n=%d",
        corp_code, bsns_year, reprt_code, fs_div, len(accounts),
    )


async def _upsert(row: dict) -> None:
    async with LocalStoreSession() as db:
        stmt = pg_insert(DartFinancial).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["corp_code", "bsns_year", "reprt_code", "fs_div"],
            set_={
                "accounts": stmt.excluded.accounts,
                "rcept_no": stmt.excluded.rcept_no,
                "rcept_dt": stmt.excluded.rcept_dt,
                "confirmed_at": stmt.excluded.confirmed_at,
                "fetched_at": stmt.excluded.fetched_at,
            },
        )
        await db.execute(stmt)
        await db.commit()


def read_accounts(
    corp_code: str, bsns_year: int, reprt_code: str, fs_div: str
) -> tuple[list[dict], bool] | None:
    """(원계정, 확정여부)를 반환한다. 적재된 적이 없으면 None.

    확정여부가 False 면 호출자가 재조회해야 한다 — 정정공시가 아직 들어올 수 있다.
    """
    return run_sync(_select(corp_code, bsns_year, reprt_code, fs_div))


async def _select(
    corp_code: str, bsns_year: int, reprt_code: str, fs_div: str
) -> tuple[list[dict], bool] | None:
    async with LocalStoreSession() as db:
        row = await db.scalar(
            select(DartFinancial).where(
                DartFinancial.corp_code == corp_code,
                DartFinancial.bsns_year == bsns_year,
                DartFinancial.reprt_code == reprt_code,
                DartFinancial.fs_div == fs_div,
            )
        )
        if row is None:
            return None
        final = row.confirmed_at is not None and date.today() >= row.confirmed_at
        return list(row.accounts or []), final


def delete_accounts(
    corp_code: str, bsns_year: int, reprt_code: str, fs_div: str
) -> None:
    """해당 보고서 행 삭제 — 테스트 정리·강제 재적재용."""
    run_sync(_delete(corp_code, bsns_year, reprt_code, fs_div))


async def _delete(
    corp_code: str, bsns_year: int, reprt_code: str, fs_div: str
) -> None:
    async with LocalStoreSession() as db:
        await db.execute(
            delete(DartFinancial).where(
                DartFinancial.corp_code == corp_code,
                DartFinancial.bsns_year == bsns_year,
                DartFinancial.reprt_code == reprt_code,
                DartFinancial.fs_div == fs_div,
            )
        )
        await db.commit()
