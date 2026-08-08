"""dart_financials 읽기/쓰기 — OpenDART 재무제표 원계정.

파생지표가 아니라 원계정을 그대로 담는 이유: derive_metrics·piotroski_f_score 가
바뀌면 저장된 파생값은 낡지만 원계정은 안 낡는다.

시장데이터와 달리 DART 는 정정공시가 있다. 그래서 접수일 + 90일이 지나야 확정으로
굳히고, 그 전에는 재조회를 허용한다(설계 §6).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import delete, func, select
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
                # accounts 만 무조건 덮는다 — 정정공시의 요점이 원계정 본문 갱신이라,
                # 새 값을 기존 값으로 되돌리면 정정을 반영하지 못한다.
                "accounts": stmt.excluded.accounts,
                # 접수번호·접수일은 나머지 리포지토리(daily/periods/indexes)와 같은
                # NULL 보존 upsert 를 쓴다 — 재조회분의 rcept_no 파싱이 실패하면 새
                # 값이 NULL 인데, 그대로 덮으면 이미 확보한 접수일이 지워진다.
                "rcept_no": func.coalesce(stmt.excluded.rcept_no, DartFinancial.rcept_no),
                "rcept_dt": func.coalesce(stmt.excluded.rcept_dt, DartFinancial.rcept_dt),
                # confirmed_at 에는 COALESCE 가 듣지 않는다 — confirmed_date() 가 접수일을
                # 모를 때도 폴백(사업연도말+1년)을 채워 돌려주므로 NULL 이 아예 도달하지
                # 않는다. 대신 **늦은 쪽**을 남긴다. 접수일을 잃은 재조회의 폴백이 원
                # 접수일+90일보다 이른 조합(예: 유예 중인 늦은 정정)에서 그대로 덮으면
                # 아직 정정 유예 중인 보고서를 확정으로 굳혀 정정 전 값이 영구히 박힌다.
                # confirmed_date() docstring 의 원칙("확정을 앞당기면 정정 전 값이 영구히
                # 굳으므로 늦추는 쪽이 안전하다")을 upsert 에서도 그대로 지키는 것이고,
                # 90일 규칙을 SQL 로 다시 구현하지 않아도 된다(그 규칙은 confirmed_date
                # 한 곳에만 있어야 한다).
                "confirmed_at": func.greatest(
                    stmt.excluded.confirmed_at, DartFinancial.confirmed_at
                ),
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
        # date.today()(컨테이너 TZ=UTC)를 그대로 쓴다 — cached_frame.is_final_date 와
        # 달리 여기서는 TZ 가 무관하다. confirmed_at 은 접수일+90일 또는 사업연도말+1년
        # 이라는 수개월 단위 지평이라, KST/UTC 하루 미만의 차이가 판정을 바꾸지 않는다.
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
