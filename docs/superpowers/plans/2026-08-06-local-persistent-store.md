# 확정 과거 데이터 로컬 영구 저장 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 확정된 과거 시장·재무 데이터를 Postgres 에 한 번 저장하고 이후로는 로컬에서만 읽어, 외부 소스 장애가 백테스트를 오염시키지 못하게 한다.

**Architecture:** 조회키가 같은 6종 데이터를 5개 정규화 테이블로 접고, 별도 `external_fetches` 원장이 "미적재"와 "데이터 없음"을 가른다. 조회는 `cached_frame(source, cache_key, ...)` 한 진입점을 지나며, 로컬에 없을 때만 외부를 1회 호출하고 실패하면 `DataSourceError` 를 그대로 올린다. `metrics/fetch.py` 는 살아있는 메인 루프 아래 `asyncio.to_thread` 로 도는 동기 코드라, 전역 async 엔진 대신 `NullPool` 전용 엔진을 쓴다.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0(asyncio) + asyncpg, PostgreSQL + TimescaleDB, Alembic, pandas, pytest, Celery(beat)

## Global Constraints

- 커밋 메시지·주석·docstring·문서는 **한국어**.
- 커밋 트레일러: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- 테스트는 컨테이너 안에서: `docker compose exec -T web pytest`
- **테스트는 실제 KRX/DART/KOFIA 를 절대 호출하지 않는다.** `tests/conftest.py` 가 `KRX_ID`/`KRX_PW`(및 `_FILE` 변형)를 비우는 이유가 이것이다 — 과거 계정 ID 가 테스트 출력에 유출된 사고가 있었다.
- 기본 테스트 스위트는 **실제 DB 에 접속하지 않는다**. 기존 테스트가 전부 `tests/conftest.py` 의 `FakeDB` 인메모리 대역을 쓴다. SQL 리포지토리 검증은 `QF_DB_TESTS=1` 이 설정됐을 때만 도는 통합 테스트로 분리한다.
- `web`/`engine`/`worker` 는 핫리로드 없음 → 코드 변경 후 `docker compose restart <svc>`.
- 새 마이그레이션은 `0013` 부터. `down_revision = "0012"`.
- 설계 근거는 `docs/superpowers/specs/2026-08-06-local-persistent-store-design.md`. 계획과 스펙이 어긋나면 스펙이 우선이다.
- 신규 파이썬 의존성 추가 금지. `requirements.txt` 에 asyncpg 만 있고 psycopg2 는 없다 — 동기 드라이버를 끌어들이지 않는다.

## File Structure

| 파일 | 책임 |
|---|---|
| `backend/app/core/local_store_db.py` (신규) | 로컬 스토어 전용 `NullPool` async 엔진·세션팩토리·동기 진입점 `run_sync` |
| `backend/app/models/store.py` (신규) | 6개 ORM 모델(5테이블 + 원장) |
| `backend/app/models/__init__.py` (수정) | 새 모델 re-export — `Base.metadata` 에 등록되어야 alembic 이 본다 |
| `backend/alembic/versions/0013_local_store.py` (신규) | 6테이블 생성 + `stock_daily_snapshots` hypertable 변환 |
| `backend/app/services/data/store/ledger.py` (신규) | `LedgerEntry`·`Ledger` 프로토콜·`InMemoryLedger`·`SqlLedger` |
| `backend/app/services/data/store/coerce.py` (신규) | DataFrame 값 → DB 컬럼 타입 변환. 리포지토리 3종이 공유한다 |
| `backend/app/services/data/store/frame.py` (신규) | `cached_frame` 4상태 코어 + `is_final_date` |
| `backend/app/services/data/store/daily.py` (신규) | `stock_daily_snapshots` 읽기/쓰기(NULL 보존 upsert) |
| `backend/app/services/data/store/periods.py` (신규) | `stock_period_stats` 읽기/쓰기 |
| `backend/app/services/data/store/indexes.py` (신규) | `index_ohlcv`·`index_constituents` 읽기/쓰기 |
| `backend/app/services/data/store/dart_store.py` (신규) | `dart_financials` 읽기/쓰기 + 90일 확정 유예 |
| `backend/app/services/data/store/__init__.py` (신규) | 공개 API re-export |
| `backend/app/services/metrics/fetch.py` (수정) | 8개 조회 함수를 스토어 경유로 전환, `_fetch_per_market` 예외 전파 |
| `backend/app/services/data/krx_index.py` (수정) | `index_members` 스토어 경유 |
| `backend/app/services/data/opendart.py` (수정) | `single_company_accounts` 스토어 경유 |
| `backend/worker/tasks.py` (수정) | 야간 배치 `ingest_daily_snapshots` |
| `backend/worker/celery_app.py` (수정) | beat 스케줄 등록 |

**테스트 파일**

| 파일 | 대상 |
|---|---|
| `backend/tests/test_local_store_db.py` (신규) | `run_sync` 루프 가드 |
| `backend/tests/test_local_store_frame.py` (신규) | `cached_frame` 4상태 6시나리오 |
| `backend/tests/test_local_store_repo.py` (신규) | SQL 리포지토리 통합(`QF_DB_TESTS=1` 에서만) |
| `backend/tests/test_fetch_store_wiring.py` (신규) | `metrics/fetch.py` 배선 + 예외 전파 |

---

### Task 1: 전용 NullPool 엔진과 동기 진입점

**Files:**
- Create: `backend/app/core/local_store_db.py`
- Test: `backend/tests/test_local_store_db.py`

**Interfaces:**
- Consumes: `app.core.config.settings.DATABASE_URL`
- Produces:
  - `local_store_engine` — `AsyncEngine` (NullPool)
  - `LocalStoreSession` — `async_sessionmaker[AsyncSession]`
  - `run_sync(coro: Coroutine[Any, Any, T]) -> T` — 워커 스레드 전용 동기 진입점. 이벤트루프 안에서 호출하면 `RuntimeError`.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_local_store_db.py`:

```python
"""로컬 스토어 전용 엔진의 동기 진입점 가드 검증.

metrics/fetch.py 는 살아있는 메인 루프 아래 asyncio.to_thread 로 도는 동기 코드다.
run_sync 가 이벤트루프 안에서 호출되면 asyncio.run 이 곧바로 터지는데, 그 지점이
스토어 내부 깊은 곳이면 원인 파악이 어렵다. 진입점에서 명시적으로 막는다.
"""
import asyncio

import pytest

from app.core.local_store_db import run_sync


async def _answer() -> int:
    return 42


def test_run_sync_동기_컨텍스트에서_코루틴을_실행한다():
    assert run_sync(_answer()) == 42


@pytest.mark.asyncio
async def test_run_sync_이벤트루프_안에서는_거부한다():
    coro = _answer()
    with pytest.raises(RuntimeError, match="이벤트루프"):
        run_sync(coro)
    coro.close()  # "never awaited" 경고 방지


def test_run_sync_는_워커_스레드에서도_동작한다():
    """to_thread 로 넘어간 워커 스레드에는 실행 중인 루프가 없어 통과해야 한다."""
    result: list[int] = []

    async def _outer():
        result.append(await asyncio.to_thread(lambda: run_sync(_answer())))

    asyncio.run(_outer())
    assert result == [42]
```

- [ ] **Step 2: 실패 확인**

Run: `docker compose exec -T web pytest tests/test_local_store_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.local_store_db'`

- [ ] **Step 3: 구현**

`backend/app/core/local_store_db.py`:

```python
"""로컬 영구 저장소 전용 DB 엔진 — 동기 스레드에서 안전하게 쓰기 위한 별도 풀.

왜 app.core.database 의 전역 엔진을 쓰지 않는가:

metrics/fetch.py 는 동기 함수이고 호출자가 asyncio.to_thread 로 실행한다. 즉 메인
이벤트루프가 살아있는 채로 워커 스레드에서 돈다. 그 스레드에서 asyncio.run 을 쓰면
루프가 매번 새로 만들어지는데, asyncpg 커넥션은 루프에 묶여 있어 전역 풀에 남은
커넥션을 다음 루프가 재사용하면 "Future attached to a different loop" 로 죽는다
(worker/tasks.py:21-39 가 겪고 dispose 로 막은 잠복 버그).

worker 의 해법(실행 끝에 engine.dispose())은 여기서 못 쓴다 — 메인 루프가 쓰던
커넥션까지 끊어버린다. 그래서 NullPool 전용 엔진을 따로 둔다. 풀링을 하지 않으므로
매 호출이 제 루프의 새 커넥션을 열고 닫아, 교차 루프 재사용이 원천적으로 불가능하다.
호출 빈도가 리밸런싱 날짜 단위라 커넥션 수립 비용은 무시할 수준이다.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings

T = TypeVar("T")

local_store_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    poolclass=NullPool,
)

LocalStoreSession = async_sessionmaker(
    bind=local_store_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """동기 컨텍스트(워커 스레드)에서 스토어 코루틴을 실행한다.

    이벤트루프 안에서 부르면 asyncio.run 이 어차피 터지지만, 그 예외는 스토어 내부
    깊은 곳에서 나와 원인이 흐려진다. 진입점에서 무엇이 잘못됐는지 말하고 막는다.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # 실행 중인 루프 없음 = 정상 경로
    else:
        raise RuntimeError(
            "run_sync 는 이벤트루프 안에서 호출할 수 없다. "
            "async 컨텍스트라면 코루틴을 직접 await 하라."
        )
    return asyncio.run(coro)
```

- [ ] **Step 4: 통과 확인**

Run: `docker compose exec -T web pytest tests/test_local_store_db.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: 커밋**

```bash
git add backend/app/core/local_store_db.py backend/tests/test_local_store_db.py
git commit -m "feat: 로컬 스토어 전용 NullPool 엔진과 동기 진입점을 만든다

metrics/fetch.py 는 메인 루프가 살아있는 채로 to_thread 워커 스레드에서 돈다.
전역 엔진의 풀은 루프에 묶인 asyncpg 커넥션을 남겨 다음 루프에서 터지고, worker 의
engine.dispose 해법은 메인 루프 커넥션까지 끊어 여기선 쓸 수 없다. NullPool 전용
엔진으로 교차 루프 재사용을 원천 차단한다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: ORM 모델과 마이그레이션

**Files:**
- Create: `backend/app/models/store.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/0013_local_store.py`

**Interfaces:**
- Consumes: `app.models.base.Base`
- Produces: 모델 클래스 `StockDailySnapshot`, `StockPeriodStat`, `IndexOhlcv`, `IndexConstituent`, `DartFinancial`, `ExternalFetch` — 이후 모든 리포지토리 태스크가 이 이름과 컬럼명을 그대로 쓴다.

- [ ] **Step 1: 모델 작성**

`backend/app/models/store.py`:

```python
"""확정 과거 데이터의 로컬 영구 저장소 모델.

설계: docs/superpowers/specs/2026-08-06-local-persistent-store-design.md

조회키가 같은 데이터끼리 접었다. 펀더멘털·시가총액·전종목 OHLCV 는 셋 다
(거래일 × 종목) 격자라 stock_daily_snapshots 한 장에 들어간다. 기간 등락률·순매수는
기간키(start~end)라 별도다 — pykrx 기간 등락률은 수정주가 기준이라 price_ticks
종가로 재계산하면 액면분할·유상증자 구간에서 값이 갈리므로 원본을 그대로 보관한다.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StockDailySnapshot(Base):
    """거래일 × 종목 격자 — 펀더멘털 + 시가총액 + 전종목 OHLCV.

    세 소스가 서로 다른 시점에 채우므로 전 컬럼 nullable 이고, upsert 는 들어온 값이
    NULL 이면 기존값을 보존한다(시총만 적재된 행을 펀더멘털 적재가 지우면 안 된다).
    """

    __tablename__ = "stock_daily_snapshots"
    __table_args__ = (Index("ix_stock_daily_snapshots_symbol_date", "symbol", "trade_date"),)

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)
    market: Mapped[str | None] = mapped_column(String(20), nullable=True)

    per: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    div: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    market_cap: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trading_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockPeriodStat(Base):
    """기간키(start~end) 종목 통계 — 기간 등락률과 투자자 순매수.

    investors 는 투자자군 조합을 정렬 후 ',' 로 이은 문자열(기본 "기관합계,외국인").
    조합이 다르면 다른 행이다. 등락률만 조회한 행은 investors=''.
    """

    __tablename__ = "stock_period_stats"
    __table_args__ = (
        Index("ix_stock_period_stats_symbol", "symbol"),
        Index("ix_stock_period_stats_range", "start_date", "end_date"),
    )

    start_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    investors: Mapped[str] = mapped_column(String(100), primary_key=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)

    market: Mapped[str | None] = mapped_column(String(20), nullable=True)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trading_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    net_buy_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IndexOhlcv(Base):
    """지수 일봉 — 업종지수·KOSPI/KOSDAQ 대표지수."""

    __tablename__ = "index_ohlcv"

    index_code: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)

    index_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trading_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IndexConstituent(Base):
    """PIT 지수구성 — base_date 시점의 지수 편입 종목."""

    __tablename__ = "index_constituents"
    __table_args__ = (Index("ix_index_constituents_code_date", "index_code", "base_date"),)

    index_code: Mapped[str] = mapped_column(String(40), primary_key=True, nullable=False)
    base_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DartFinancial(Base):
    """OpenDART 재무제표 원계정.

    파생지표(derive_metrics·piotroski_f_score)가 아니라 원계정 리스트를 그대로 담는다.
    파생 코드가 바뀌면 저장된 파생값은 낡지만 원계정은 안 낡는다.

    confirmed_at 이후로는 불변으로 취급한다(정정공시 반영 유예 = 접수일 + 90일).
    """

    __tablename__ = "dart_financials"

    corp_code: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)
    bsns_year: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    reprt_code: Mapped[str] = mapped_column(String(10), primary_key=True, nullable=False)
    fs_div: Mapped[str] = mapped_column(String(10), primary_key=True, nullable=False)

    accounts: Mapped[list] = mapped_column(JSONB, nullable=False)
    rcept_no: Mapped[str | None] = mapped_column(String(30), nullable=True)
    rcept_dt: Mapped[date | None] = mapped_column(Date, nullable=True)
    confirmed_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExternalFetch(Base):
    """페치 원장 — "이 조회를 실제로 해봤는가"의 유일한 기록.

    정규화 테이블만으로는 "휴장일이라 0행"과 "아직 적재 안 됨"이 똑같이 0행이라
    구분이 불가능하다. 구분하지 못하면 §48 이 닫으려던 조용한 실패 모드를 이 저장소가
    그대로 재현한다. 조회 사실 자체를 여기 남겨 둘을 가른다.

    final=False 는 "저장은 했지만 아직 확정 아님"(당일 시세·미확정 DART)이라 다음
    호출에서 재조회된다.
    """

    __tablename__ = "external_fetches"

    source: Mapped[str] = mapped_column(String(40), primary_key=True, nullable=False)
    cache_key: Mapped[str] = mapped_column(String(200), primary_key=True, nullable=False)

    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    final: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 2: `app/models/__init__.py` 에 등록**

`Base.metadata` 에 붙어야 alembic 이 인식한다. `from app.models.models import (...)` 블록 **다음 줄**에 추가:

```python
from app.models.store import (
    DartFinancial,
    ExternalFetch,
    IndexConstituent,
    IndexOhlcv,
    StockDailySnapshot,
    StockPeriodStat,
)
```

그리고 `__all__` 리스트 마지막 `"NewsArticleSymbol",` 다음에 추가:

```python
    "StockDailySnapshot",
    "StockPeriodStat",
    "IndexOhlcv",
    "IndexConstituent",
    "DartFinancial",
    "ExternalFetch",
```

- [ ] **Step 3: 마이그레이션 작성**

`backend/alembic/versions/0013_local_store.py`:

```python
"""확정 과거 데이터 로컬 영구 저장소 6테이블 추가.

설계: docs/superpowers/specs/2026-08-06-local-persistent-store-design.md

stock_daily_snapshots 는 종목수×거래일 규모(2,800종목 × 250일 × 10년 ≈ 7백만 행)라
price_ticks 와 같이 trade_date 기준 hypertable 로 파티셔닝한다.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- stock_daily_snapshots (거래일 × 종목 격자) ---
    op.create_table(
        "stock_daily_snapshots",
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(20), nullable=True),
        sa.Column("per", sa.Numeric(18, 4), nullable=True),
        sa.Column("pbr", sa.Numeric(18, 4), nullable=True),
        sa.Column("div", sa.Numeric(18, 4), nullable=True),
        sa.Column("market_cap", sa.BigInteger, nullable=True),
        sa.Column("shares", sa.BigInteger, nullable=True),
        sa.Column("open", sa.Numeric(18, 4), nullable=True),
        sa.Column("high", sa.Numeric(18, 4), nullable=True),
        sa.Column("low", sa.Numeric(18, 4), nullable=True),
        sa.Column("close", sa.Numeric(18, 4), nullable=True),
        sa.Column("volume", sa.BigInteger, nullable=True),
        sa.Column("trading_value", sa.BigInteger, nullable=True),
        sa.Column("change_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("trade_date", "symbol"),
    )
    op.create_index(
        "ix_stock_daily_snapshots_symbol_date",
        "stock_daily_snapshots",
        ["symbol", "trade_date"],
    )
    op.execute(
        "SELECT create_hypertable('stock_daily_snapshots', 'trade_date', "
        "if_not_exists => TRUE, migrate_data => TRUE);"
    )

    # --- stock_period_stats (기간키) ---
    op.create_table(
        "stock_period_stats",
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("investors", sa.String(100), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(20), nullable=True),
        sa.Column("change_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column("open", sa.Numeric(18, 4), nullable=True),
        sa.Column("close", sa.Numeric(18, 4), nullable=True),
        sa.Column("volume", sa.BigInteger, nullable=True),
        sa.Column("trading_value", sa.BigInteger, nullable=True),
        sa.Column("net_buy_value", sa.Numeric(24, 2), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("start_date", "end_date", "investors", "symbol"),
    )
    op.create_index("ix_stock_period_stats_symbol", "stock_period_stats", ["symbol"])
    op.create_index(
        "ix_stock_period_stats_range", "stock_period_stats", ["start_date", "end_date"]
    )

    # --- index_ohlcv ---
    op.create_table(
        "index_ohlcv",
        sa.Column("index_code", sa.String(20), nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("index_name", sa.String(100), nullable=True),
        sa.Column("open", sa.Numeric(18, 4), nullable=True),
        sa.Column("high", sa.Numeric(18, 4), nullable=True),
        sa.Column("low", sa.Numeric(18, 4), nullable=True),
        sa.Column("close", sa.Numeric(18, 4), nullable=True),
        sa.Column("volume", sa.BigInteger, nullable=True),
        sa.Column("trading_value", sa.BigInteger, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("index_code", "trade_date"),
    )

    # --- index_constituents (PIT 지수구성) ---
    op.create_table(
        "index_constituents",
        sa.Column("index_code", sa.String(40), nullable=False),
        sa.Column("base_date", sa.Date, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("index_code", "base_date", "symbol"),
    )
    op.create_index(
        "ix_index_constituents_code_date", "index_constituents", ["index_code", "base_date"]
    )

    # --- dart_financials ---
    op.create_table(
        "dart_financials",
        sa.Column("corp_code", sa.String(20), nullable=False),
        sa.Column("bsns_year", sa.Integer, nullable=False),
        sa.Column("reprt_code", sa.String(10), nullable=False),
        sa.Column("fs_div", sa.String(10), nullable=False),
        sa.Column("accounts", postgresql.JSONB, nullable=False),
        sa.Column("rcept_no", sa.String(30), nullable=True),
        sa.Column("rcept_dt", sa.Date, nullable=True),
        sa.Column("confirmed_at", sa.Date, nullable=True),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("corp_code", "bsns_year", "reprt_code", "fs_div"),
    )

    # --- external_fetches (페치 원장) ---
    op.create_table(
        "external_fetches",
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("cache_key", sa.String(200), nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("final", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("source", "cache_key"),
    )


def downgrade() -> None:
    op.drop_table("external_fetches")
    op.drop_table("dart_financials")
    op.drop_index("ix_index_constituents_code_date", table_name="index_constituents")
    op.drop_table("index_constituents")
    op.drop_table("index_ohlcv")
    op.drop_index("ix_stock_period_stats_range", table_name="stock_period_stats")
    op.drop_index("ix_stock_period_stats_symbol", table_name="stock_period_stats")
    op.drop_table("stock_period_stats")
    op.drop_index(
        "ix_stock_daily_snapshots_symbol_date", table_name="stock_daily_snapshots"
    )
    op.drop_table("stock_daily_snapshots")
```

- [ ] **Step 4: 마이그레이션 적용·역적용 검증**

```bash
docker compose exec -T web alembic upgrade head
docker compose exec -T web alembic current
```
Expected: `0013 (head)`

역적용도 확인한다(hypertable 은 drop 이 막히는 경우가 있어 실제로 돌려봐야 안다):

```bash
docker compose exec -T web alembic downgrade 0012
docker compose exec -T web alembic upgrade head
```
Expected: 둘 다 에러 없이 완료, 최종 `alembic current` 가 `0013 (head)`

- [ ] **Step 5: 모델 메타데이터 정합 확인**

autogenerate 가 추가 diff 를 만들지 않아야 모델과 마이그레이션이 일치한다.

```bash
docker compose exec -T web alembic revision --autogenerate -m "정합확인" 2>&1 | tail -20
```
Expected: 생성된 파일의 `upgrade()` 가 `pass` 뿐. 확인 후 **반드시 삭제**:
```bash
docker compose exec -T web sh -c 'rm -f /app/alembic/versions/*정합확인*.py'
git status --short backend/alembic/versions/
```
Expected: `0013_local_store.py` 외 신규 파일 없음

- [ ] **Step 6: 커밋**

```bash
git add backend/app/models/store.py backend/app/models/__init__.py backend/alembic/versions/0013_local_store.py
git commit -m "feat: 로컬 영구 저장소 6테이블 모델과 마이그레이션을 추가한다

조회키가 같은 데이터끼리 접어 5테이블로 만들고, 페치 원장(external_fetches)을 따로
둔다. 원장이 없으면 '휴장일이라 0행'과 '아직 적재 안 됨'이 구분되지 않아 §48 이
닫으려던 조용한 실패 모드가 그대로 재현된다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: 페치 원장 — 프로토콜과 인메모리 구현

**Files:**
- Create: `backend/app/services/data/store/__init__.py`
- Create: `backend/app/services/data/store/ledger.py`
- Test: `backend/tests/test_local_store_frame.py` (이 태스크에서 만들고 Task 4 가 이어 쓴다)

**Interfaces:**
- Consumes: `app.models.store.ExternalFetch`, `app.core.local_store_db.LocalStoreSession`, `run_sync`
- Produces:
  - `LedgerEntry(row_count: int, final: bool)` — frozen dataclass
  - `Ledger` — Protocol: `get(source, cache_key) -> LedgerEntry | None`, `put(source, cache_key, *, row_count, final) -> None`
  - `InMemoryLedger()` — 테스트용, `Ledger` 만족
  - `SqlLedger()` — `external_fetches` 백엔드, `Ledger` 만족
  - `default_ledger() -> Ledger` — 기본 구현 반환(테스트가 `monkeypatch` 로 갈아끼운다)

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_local_store_frame.py`:

```python
"""로컬 영구 저장소의 원장·조회 계약 검증.

실제 DB 를 쓰지 않는다. 원장은 InMemoryLedger, 로컬 저장소는 dict 대역으로 갈음하고
"몇 번 외부를 호출했는가"만 본다 — 이 계층의 책임이 그것뿐이기 때문이다.
"""
from app.services.data.store.ledger import InMemoryLedger, LedgerEntry


def test_인메모리_원장은_기록이_없으면_None_을_준다():
    ledger = InMemoryLedger()
    assert ledger.get("fundamentals", "20190312|KOSPI") is None


def test_인메모리_원장은_기록을_되돌려준다():
    ledger = InMemoryLedger()
    ledger.put("fundamentals", "20190312|KOSPI", row_count=812, final=True)
    assert ledger.get("fundamentals", "20190312|KOSPI") == LedgerEntry(
        row_count=812, final=True
    )


def test_인메모리_원장은_0행_기록과_미기록을_구분한다():
    """이 구분이 이 저장소의 존재 이유다 — 휴장일과 미적재가 같은 값이면 안 된다."""
    ledger = InMemoryLedger()
    ledger.put("fundamentals", "20190101|KOSPI", row_count=0, final=True)
    assert ledger.get("fundamentals", "20190101|KOSPI") == LedgerEntry(
        row_count=0, final=True
    )
    assert ledger.get("fundamentals", "20190102|KOSPI") is None


def test_인메모리_원장은_같은_키를_덮어쓴다():
    ledger = InMemoryLedger()
    ledger.put("index_ohlcv", "1001|20260806", row_count=1, final=False)
    ledger.put("index_ohlcv", "1001|20260806", row_count=1, final=True)
    assert ledger.get("index_ohlcv", "1001|20260806").final is True
```

- [ ] **Step 2: 실패 확인**

Run: `docker compose exec -T web pytest tests/test_local_store_frame.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.data.store'`

- [ ] **Step 3: 구현**

`backend/app/services/data/store/__init__.py`:

```python
"""확정 과거 데이터의 로컬 영구 저장소.

설계: docs/superpowers/specs/2026-08-06-local-persistent-store-design.md

공개 진입점은 frame.cached_frame 하나다. 나머지 모듈(ledger·daily·periods·indexes·
dart_store)은 그 뒤에서 테이블별 읽기/쓰기를 담당한다.
"""
from app.services.data.store.ledger import (
    InMemoryLedger,
    Ledger,
    LedgerEntry,
    SqlLedger,
    default_ledger,
)

__all__ = [
    "Ledger",
    "LedgerEntry",
    "InMemoryLedger",
    "SqlLedger",
    "default_ledger",
]
```

`backend/app/services/data/store/ledger.py`:

```python
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
```

- [ ] **Step 4: 통과 확인**

Run: `docker compose exec -T web pytest tests/test_local_store_frame.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/store/ backend/tests/test_local_store_frame.py
git commit -m "feat: 페치 원장을 도입해 미적재와 데이터 없음을 가른다

정규화 테이블만으로는 휴장일 0행과 미적재 0행이 같은 값이라 구분이 불가능하다.
조회 사실 자체를 external_fetches 에 남겨 둘을 가른다. 테스트용 인메모리 구현과
프로덕션 SQL 구현을 같은 Protocol 뒤에 둔다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `cached_frame` 4상태 코어

**Files:**
- Create: `backend/app/services/data/store/frame.py`
- Modify: `backend/app/services/data/store/__init__.py`
- Test: `backend/tests/test_local_store_frame.py` (Task 3 의 파일에 이어 쓴다)

**Interfaces:**
- Consumes: `Ledger`, `LedgerEntry`, `default_ledger` (Task 3), `app.services.data.errors.DataSourceError`
- Produces:
  - `cached_frame(source, cache_key, *, read_local, fetch_remote, write_local, is_final, ledger=None) -> pd.DataFrame`
    `is_final` 은 `bool | Callable[[], bool]`. **콜러블 형태가 필수다** — 확정 여부가 `fetch_remote()` 실행 결과(부분 실패 여부)에 달린 호출자가 있고(Task 6·7), 값으로 받으면 `fetch_remote()` 가 돌기 **전에** 평가되어 항상 틀린다. `write_local()` 다음, `ledger.put()` 직전에 호출해 평가한다.
  - `is_final_date(last_day: date, *, today: date | None = None) -> bool`
  - `make_cache_key(*parts: object) -> str`

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_local_store_frame.py` **끝에 이어 붙인다**:

```python
from datetime import date

import pandas as pd
import pytest

from app.services.data.errors import SourceAuthError
from app.services.data.store.frame import cached_frame, is_final_date, make_cache_key


class _Spy:
    """외부 호출 횟수와 로컬 저장을 추적하는 대역."""

    def __init__(self, remote: pd.DataFrame | Exception):
        self.remote = remote
        self.calls = 0
        self.stored: pd.DataFrame | None = None

    def fetch_remote(self) -> pd.DataFrame:
        self.calls += 1
        if isinstance(self.remote, Exception):
            raise self.remote
        return self.remote

    def write_local(self, df: pd.DataFrame) -> None:
        self.stored = df

    def read_local(self) -> pd.DataFrame:
        return self.stored if self.stored is not None else pd.DataFrame()


def _frame(n: int) -> pd.DataFrame:
    return pd.DataFrame({"PER": [10.0] * n})


def test_미적재면_외부를_한_번_호출하고_저장한다():
    ledger = InMemoryLedger()
    spy = _Spy(_frame(3))

    out = cached_frame(
        "fundamentals", "20190312|KOSPI",
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=True, ledger=ledger,
    )

    assert spy.calls == 1
    assert len(out) == 3
    assert ledger.get("fundamentals", "20190312|KOSPI") == LedgerEntry(
        row_count=3, final=True
    )


def test_확정_적재분은_외부를_호출하지_않는다():
    ledger = InMemoryLedger()
    spy = _Spy(_frame(3))
    kwargs = dict(
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=True, ledger=ledger,
    )

    cached_frame("fundamentals", "20190312|KOSPI", **kwargs)
    out = cached_frame("fundamentals", "20190312|KOSPI", **kwargs)

    assert spy.calls == 1  # 두 번째 호출은 로컬에서 읽었다
    assert len(out) == 3


def test_확정된_0행은_빈_결과를_주되_재조회하지_않는다():
    """휴장일이 매번 외부를 두드리면 저장소의 목적이 무너진다."""
    ledger = InMemoryLedger()
    spy = _Spy(pd.DataFrame())
    kwargs = dict(
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=True, ledger=ledger,
    )

    cached_frame("fundamentals", "20190101|KOSPI", **kwargs)
    out = cached_frame("fundamentals", "20190101|KOSPI", **kwargs)

    assert spy.calls == 1
    assert out.empty


def test_외부_실패는_빈_프레임이_아니라_예외로_전파된다():
    """§48 의 핵심 계약 — 실패가 값이 되면 호출자가 무시할 수 있다."""
    ledger = InMemoryLedger()
    spy = _Spy(SourceAuthError("krx", "로그인 차단"))

    with pytest.raises(SourceAuthError):
        cached_frame(
            "fundamentals", "20190312|KOSPI",
            read_local=spy.read_local, fetch_remote=spy.fetch_remote,
            write_local=spy.write_local, is_final=True, ledger=ledger,
        )

    assert ledger.get("fundamentals", "20190312|KOSPI") is None  # 실패는 기록하지 않는다


def test_미확정_적재분은_다음_호출에서_재조회된다():
    """당일 시세·미확정 DART 는 저장하되 굳히지 않는다."""
    ledger = InMemoryLedger()
    spy = _Spy(_frame(2))
    kwargs = dict(
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=False, ledger=ledger,
    )

    cached_frame("index_ohlcv", "1001|20260806", **kwargs)
    cached_frame("index_ohlcv", "1001|20260806", **kwargs)

    assert spy.calls == 2


def test_ledger_생략시_기본_구현을_쓴다(monkeypatch):
    """프로덕션 경로가 default_ledger 를 경유하는지 확인한다."""
    fake = InMemoryLedger()
    monkeypatch.setattr(
        "app.services.data.store.frame.default_ledger", lambda: fake
    )
    spy = _Spy(_frame(1))

    cached_frame(
        "market_cap", "20190312|KOSPI",
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=True,
    )

    assert fake.get("market_cap", "20190312|KOSPI") == LedgerEntry(row_count=1, final=True)


def test_is_final_은_콜러블도_받는다():
    """확정 여부가 조회 결과에 달린 호출자가 있다 — 부분 실패면 굳히면 안 된다."""
    ledger = InMemoryLedger()
    spy = _Spy(_frame(2))

    cached_frame(
        "price_change", "20190101|20190131|KOSPI",
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=lambda: True, ledger=ledger,
    )

    assert ledger.get("price_change", "20190101|20190131|KOSPI").final is True


def test_is_final_콜러블은_외부조회_뒤에_평가된다():
    """값으로 미리 평가하면 fetch_remote 결과에 의존하는 판단이 항상 틀린다."""
    ledger = InMemoryLedger()
    complete = False

    def _fetch():
        nonlocal complete
        complete = True  # 조회가 끝나야 확정 여부가 정해진다
        return _frame(1)

    cached_frame(
        "price_change", "20190201|20190228|KOSPI",
        read_local=lambda: pd.DataFrame(), fetch_remote=_fetch,
        write_local=lambda df: None, is_final=lambda: complete, ledger=ledger,
    )

    assert ledger.get("price_change", "20190201|20190228|KOSPI").final is True


def test_is_final_date_는_전일까지만_확정으로_본다():
    today = date(2026, 8, 6)
    assert is_final_date(date(2026, 8, 5), today=today) is True
    assert is_final_date(date(2026, 8, 6), today=today) is False
    assert is_final_date(date(2026, 8, 7), today=today) is False


def test_make_cache_key_는_결정적이다():
    assert make_cache_key("20190312", "KOSPI") == "20190312|KOSPI"
    assert make_cache_key("20190312", ["KOSDAQ", "KOSPI"]) == "20190312|KOSDAQ,KOSPI"
    # 순서가 달라도 같은 키가 나와야 한다 — 시장 목록은 정렬된다.
    assert make_cache_key("20190312", ["KOSPI", "KOSDAQ"]) == make_cache_key(
        "20190312", ["KOSDAQ", "KOSPI"]
    )
```

- [ ] **Step 2: 실패 확인**

Run: `docker compose exec -T web pytest tests/test_local_store_frame.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.data.store.frame'`

- [ ] **Step 3: 구현**

`backend/app/services/data/store/frame.py`:

```python
"""로컬 우선 조회의 단일 진입점 — 4상태를 명시적으로 가른다.

§48 이 실패/데이터없음/미설정 셋을 갈랐다면, 로컬 저장소는 네 번째 상태
"아직 적재 안 됨"을 추가한다. 이것이 "데이터 없음"으로 뭉개지면 휴장일마다 외부를
두드리거나(성능), 반대로 미적재를 빈 결과로 오인해 백테스트가 빈 패널 위에서
'성공'한다(정확성) — 후자가 §44-1·§47 에서 실제로 난 사고다.

| 원장 상태            | 동작                                          |
|---------------------|-----------------------------------------------|
| final=True 기록 있음 | read_local() 반환. 0행이면 0행 그대로(진짜 없음) |
| 기록 없음/final=False| fetch_remote() 1회 → write_local() + 원장 기록  |
| fetch_remote() 실패  | DataSourceError 그대로 raise(값으로 삼키지 않음) |
| 소스 미설정          | 여기 오기 전에 통과(§48 미설정 경로 불변)        |
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import pandas as pd

from app.services.data.store.ledger import Ledger, default_ledger

logger = logging.getLogger("app.services.data.store")


def make_cache_key(*parts: object) -> str:
    """조회 인자를 결정적인 문자열로 직렬화한다.

    같은 인자가 언제나 같은 키를 만들어야 원장이 제 구실을 한다. 목록형 인자
    (시장 목록·투자자군)는 호출 순서가 달라도 같은 조회이므로 정렬한다.
    """
    out: list[str] = []
    for p in parts:
        if isinstance(p, (list, tuple, set, frozenset)):
            out.append(",".join(sorted(str(x) for x in p)))
        elif isinstance(p, date):
            out.append(p.strftime("%Y%m%d"))
        else:
            out.append(str(p))
    return "|".join(out)


def is_final_date(last_day: date, *, today: date | None = None) -> bool:
    """그 날짜의 시장데이터를 영구 확정으로 봐도 되는가.

    전일까지만 확정으로 본다. 당일분은 장중 값이 계속 바뀌므로 굳히면 안 된다.
    """
    ref = today or date.today()
    return last_day < ref


def cached_frame(
    source: str,
    cache_key: str,
    *,
    read_local: Callable[[], pd.DataFrame],
    fetch_remote: Callable[[], pd.DataFrame],
    write_local: Callable[[pd.DataFrame], None],
    is_final: bool | Callable[[], bool],
    ledger: Ledger | None = None,
) -> pd.DataFrame:
    """로컬에 있으면 로컬에서, 없으면 외부에서 1회 가져와 영구 저장한다.

    :param source: 조회 종류(fundamentals·market_cap·index_ohlcv 등)
    :param cache_key: make_cache_key 로 만든 결정적 인자 키
    :param is_final: 이 결과를 영구 확정으로 굳혀도 되는가(당일분·미확정 DART 는 False).
        콜러블을 넘기면 `fetch_remote()` 가 끝난 **뒤에** 평가한다 — 확정 여부가 조회
        결과(부분 실패 여부)에 달린 호출자가 있어, 값으로 미리 평가하면 항상 틀린다.
    :raises DataSourceError: 외부 조회가 실패했을 때. **빈 프레임으로 삼키지 않는다.**
    """
    led = ledger if ledger is not None else default_ledger()

    entry = led.get(source, cache_key)
    if entry is not None and entry.final:
        return read_local()

    df = fetch_remote()  # 실패는 DataSourceError 로 그대로 올라간다
    if df is None:
        df = pd.DataFrame()

    write_local(df)
    # 확정 여부는 조회가 끝난 지금 평가한다 — 부분 실패 여부에 달린 호출자가 있다.
    final = is_final() if callable(is_final) else is_final
    led.put(source, cache_key, row_count=len(df), final=final)
    logger.debug(
        "로컬 적재: %s %s rows=%d final=%s", source, cache_key, len(df), final
    )
    return df
```

`backend/app/services/data/store/__init__.py` 의 import 블록과 `__all__` 에 추가:

```python
from app.services.data.store.frame import cached_frame, is_final_date, make_cache_key
```

`__all__` 에 `"cached_frame"`, `"is_final_date"`, `"make_cache_key"` 추가.

- [ ] **Step 4: 통과 확인**

Run: `docker compose exec -T web pytest tests/test_local_store_frame.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/store/frame.py backend/app/services/data/store/__init__.py backend/tests/test_local_store_frame.py
git commit -m "feat: 로컬 우선 조회의 4상태 진입점 cached_frame 을 만든다

§48 의 실패/데이터없음/미설정 셋에 '아직 적재 안 됨'을 더해 넷을 가른다. 미적재가
데이터 없음으로 뭉개지면 §44-1·§47 에서 난 사고 — 빈 패널 위에서 백테스트가
'성공'하는 — 가 그대로 재발한다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `stock_daily_snapshots` 리포지토리

**Files:**
- Create: `backend/app/services/data/store/coerce.py`
- Create: `backend/app/services/data/store/daily.py`
- Modify: `backend/app/services/data/store/__init__.py`
- Test: `backend/tests/test_local_store_repo.py`

**Interfaces:**
- Consumes: `app.models.store.StockDailySnapshot`, `LocalStoreSession`, `run_sync`
- Produces:
  - `coerce_value(value: object, kind: str) -> object | None` — `kind` 는 `"numeric"`·`"integer"`·`"text"`. **Task 7·8 의 리포지토리도 이것을 쓴다 — 모듈마다 다시 정의하지 말 것.**
  - `write_daily(trade_day: date, df: pd.DataFrame, *, columns: dict[str, str]) -> None`
    `df` 는 티커 인덱스, `columns` 는 `{DataFrame 컬럼명: 테이블 컬럼명}` 매핑(예: `{"PER": "per", "market": "market"}`).
  - `read_daily(trade_day: date, table_columns: list[str], *, out_columns: dict[str, str]) -> pd.DataFrame`
    티커 인덱스에 `out_columns` 로 되돌린 DataFrame 을 반환. 행이 없으면 `out_columns` 값들을 컬럼으로 갖는 빈 프레임.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_local_store_repo.py`:

```python
"""로컬 저장소 SQL 리포지토리 통합 테스트.

기본 스위트는 실제 DB 를 쓰지 않는다(다른 테스트가 전부 FakeDB 대역). 이 파일만
실제 Postgres 를 필요로 하므로 QF_DB_TESTS=1 일 때만 돈다.

실행:
  docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v
"""
import os
from datetime import date

import pandas as pd
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("QF_DB_TESTS") != "1",
    reason="실제 DB 가 필요하다 — QF_DB_TESTS=1 로 실행",
)

from app.services.data.store import daily  # noqa: E402

_DAY = date(1990, 1, 2)  # 실데이터와 겹치지 않는 과거 일자


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    daily.delete_daily(_DAY)


def test_쓰고_읽으면_같은_값이_나온다():
    df = pd.DataFrame({"PER": [10.5, 20.0], "market": ["KOSPI", "KOSPI"]},
                      index=["005930", "000660"])
    daily.write_daily(_DAY, df, columns={"PER": "per", "market": "market"})

    out = daily.read_daily(_DAY, ["per", "market"], out_columns={"per": "PER", "market": "market"})

    assert sorted(out.index) == ["000660", "005930"]
    assert float(out.loc["005930", "PER"]) == pytest.approx(10.5)
    assert out.loc["005930", "market"] == "KOSPI"


def test_다른_소스가_같은_행의_다른_컬럼을_채운다():
    """시총 적재가 먼저 오고 펀더멘털이 나중에 와도 서로 지우지 않아야 한다."""
    cap = pd.DataFrame({"시가총액": [500_000]}, index=["005930"])
    daily.write_daily(_DAY, cap, columns={"시가총액": "market_cap"})

    fund = pd.DataFrame({"PER": [10.5]}, index=["005930"])
    daily.write_daily(_DAY, fund, columns={"PER": "per"})

    out = daily.read_daily(
        _DAY, ["per", "market_cap"], out_columns={"per": "PER", "market_cap": "시가총액"}
    )
    assert float(out.loc["005930", "PER"]) == pytest.approx(10.5)
    assert int(out.loc["005930", "시가총액"]) == 500_000  # 앞선 적재가 살아있다


def test_행이_없으면_요청한_컬럼의_빈_프레임을_준다():
    out = daily.read_daily(_DAY, ["per"], out_columns={"per": "PER"})
    assert out.empty
    assert list(out.columns) == ["PER"]
```

- [ ] **Step 2: 실패 확인**

Run: `docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'daily'`

- [ ] **Step 3: 공유 변환 헬퍼 구현**

`backend/app/services/data/store/coerce.py`:

```python
"""DataFrame 값 → DB 컬럼 타입 변환.

리포지토리 3종(daily·periods·indexes)이 같은 변환 규칙을 쓴다. 다른 것은 어떤
컬럼이 어떤 타입인가뿐이라, 규칙은 여기 한 벌만 두고 컬럼→종류 매핑만 각자 갖는다.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

import pandas as pd

#: 컬럼 종류 — NUMERIC / BigInteger / String 에 대응.
NUMERIC = "numeric"
INTEGER = "integer"
TEXT = "text"


def coerce_value(value: object, kind: str) -> object | None:
    """DataFrame 셀 값을 저장 타입으로 변환한다. 결측·변환 불가는 None.

    pykrx 는 결측을 NaN·None·빈 문자열로 섞어 돌려주고, 컬럼 하나에 숫자와 문자열이
    섞여 오는 경우도 있다. 변환 실패를 예외로 올리지 않고 None 으로 떨어뜨리는 이유는
    종목 한 개의 이상값이 그 날짜 전체 적재를 막으면 안 되기 때문이다.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass  # pd.isna 가 스칼라를 못 주는 값(배열 등) — 아래 변환에서 걸러진다
    if kind == NUMERIC:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if kind == INTEGER:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return str(value)
```

- [ ] **Step 4: 리포지토리 구현**

`backend/app/services/data/store/daily.py`:

```python
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
from app.services.data.store.coerce import INTEGER, NUMERIC, TEXT, coerce_value

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
    trade_day: date, table_columns: list[str], *, out_columns: dict[str, str]
) -> pd.DataFrame:
    """그 거래일의 지정 컬럼을 티커 인덱스 DataFrame 으로 읽는다.

    :param out_columns: {테이블 컬럼명: 반환 DataFrame 컬럼명}
    """
    records = run_sync(_select(trade_day, table_columns))
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
        if c not in ("market",):
            out[name] = pd.to_numeric(out[name], errors="coerce")
    return out


async def _select(trade_day: date, table_columns: list[str]) -> list[tuple]:
    cols = [StockDailySnapshot.symbol] + [
        getattr(StockDailySnapshot, c) for c in table_columns
    ]
    async with LocalStoreSession() as db:
        result = await db.execute(
            select(*cols).where(StockDailySnapshot.trade_date == trade_day)
        )
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
```

`backend/app/services/data/store/__init__.py` 에 추가:

```python
from app.services.data.store import daily
from app.services.data.store.coerce import coerce_value
```
`__all__` 에 `"daily"`, `"coerce_value"` 추가.

- [ ] **Step 5: 통과 확인**

```bash
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v
```
Expected: PASS (3 passed)

기본 스위트에서 건너뛰는지도 확인:
```bash
docker compose exec -T web pytest tests/test_local_store_repo.py -v
```
Expected: 3 skipped

- [ ] **Step 6: 커밋**

```bash
git add backend/app/services/data/store/coerce.py backend/app/services/data/store/daily.py backend/app/services/data/store/__init__.py backend/tests/test_local_store_repo.py
git commit -m "feat: stock_daily_snapshots 리포지토리를 추가한다

펀더멘털·시총·전종목 OHLCV 가 같은 (거래일, 종목) 행을 서로 다른 시점에 채우므로
upsert 는 들어온 값이 NULL 이면 기존값을 보존한다. 실제 DB 가 필요한 테스트라
QF_DB_TESTS=1 에서만 돌게 분리했다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: `metrics/fetch.py` 를 스토어 경유로 전환 (펀더멘털·시총·전종목 OHLCV)

이 태스크가 §47 사고의 직접 원인 — `_fetch_per_market` 의 `except Exception → 빈 프레임` — 을 제거한다.

**Files:**
- Modify: `backend/app/services/metrics/fetch.py:38-68` (`_fetch_per_market`), `:79-131` (`_fetch_fundamentals`·`_fetch_market_cap`), `:203-220` (`_fetch_market_ohlcv_snapshot`)
- Test: `backend/tests/test_fetch_store_wiring.py`

**Interfaces:**
- Consumes: `cached_frame`, `make_cache_key`, `is_final_date` (Task 4), `daily.write_daily`/`read_daily` (Task 5), `app.services.data.errors.{DataSourceError, SourceUnavailableError, representative, stop_aggregate, note_failure}`
- Produces: `_fetch_fundamentals`·`_fetch_market_cap`·`_fetch_market_ohlcv_snapshot` 의 시그니처는 **그대로**. 달라지는 것은 (a) 전량 실패 시 빈 프레임 대신 `DataSourceError` raise, (b) 확정 과거분은 로컬에서 읽음.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_fetch_store_wiring.py`:

```python
"""metrics/fetch.py 의 로컬 스토어 배선과 실패 전파 검증.

pykrx 는 호출하지 않는다 — _pykrx_stock 을 대역으로 갈아끼운다.
스토어도 인메모리 원장 + dict 저장소로 대역해 실제 DB 를 쓰지 않는다.
"""
from datetime import date

import pandas as pd
import pytest

from app.services.data.errors import DataSourceError, SourceUnavailableError
from app.services.data.store.ledger import InMemoryLedger
from app.services.metrics import fetch as F


class _FakeStock:
    """pykrx.stock 대역 — 호출 횟수를 세고 지정된 응답/예외를 돌려준다."""

    def __init__(self, per_market: dict[str, pd.DataFrame | Exception]):
        self.per_market = per_market
        self.calls: list[str] = []

    def get_market_fundamental(self, ymd, market=None, **kw):
        self.calls.append(market)
        val = self.per_market[market]
        if isinstance(val, Exception):
            raise val
        return val


@pytest.fixture
def _store(monkeypatch):
    """스토어를 인메모리로 대역하고, 저장된 프레임을 그대로 돌려주게 한다."""
    ledger = InMemoryLedger()
    saved: dict[tuple, pd.DataFrame] = {}

    monkeypatch.setattr(F, "_store_ledger", lambda: ledger)
    monkeypatch.setattr(F, "_store_write_daily", lambda day, df, columns: saved.__setitem__((day, tuple(columns)), df))
    monkeypatch.setattr(F, "_store_read_daily", lambda day, cols, out_columns: saved.get((day, tuple(out_columns)), pd.DataFrame()))
    F._FUND_CACHE.clear()
    yield ledger, saved
    F._FUND_CACHE.clear()


def _fund_frame():
    return pd.DataFrame(
        {"PER": [10.0], "PBR": [1.0], "DIV": [2.0]}, index=["005930"]
    )


def test_전량_실패는_예외로_전파된다(_store, monkeypatch):
    """§47 사고의 직접 원인 — 전량 실패가 빈 프레임이 되어 백테스트가 '성공'했다."""
    boom = SourceUnavailableError("krx", "차단")
    fake = _FakeStock({"KOSPI": boom, "KOSDAQ": boom})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    with pytest.raises(DataSourceError):
        F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])


def test_일부_시장만_실패하면_성공분을_돌려준다(_store, monkeypatch):
    fake = _FakeStock({"KOSPI": _fund_frame(), "KOSDAQ": SourceUnavailableError("krx", "일시장애")})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    out = F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])
    assert len(out) == 1


def test_부분_실패는_확정으로_굳히지_않는다(_store, monkeypatch):
    """다음 호출에서 빠진 시장을 보완할 수 있어야 한다."""
    ledger, _ = _store
    fake = _FakeStock({"KOSPI": _fund_frame(), "KOSDAQ": SourceUnavailableError("krx", "일시장애")})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])
    entry = ledger.get("fundamentals", "20190312|KOSDAQ,KOSPI")
    assert entry is None or entry.final is False


def test_확정_적재분은_pykrx_를_다시_부르지_않는다(_store, monkeypatch):
    fake = _FakeStock({"KOSPI": _fund_frame(), "KOSDAQ": _fund_frame()})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])
    F._FUND_CACHE.clear()  # 프로세스 내 1차 캐시를 비워 2차(로컬)만 남긴다
    F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])

    assert len(fake.calls) == 2  # 첫 호출의 두 시장뿐
```

- [ ] **Step 2: 실패 확인**

Run: `docker compose exec -T web pytest tests/test_fetch_store_wiring.py -v`
Expected: FAIL — `AttributeError: module 'app.services.metrics.fetch' has no attribute '_store_ledger'`

- [ ] **Step 3: 구현**

`backend/app/services/metrics/fetch.py` 상단 import 블록(22행 `from app.services.data.loader import ...` 아래)에 추가:

```python
from app.services.data.errors import (
    DataSourceError,
    SourceUnavailableError,
    note_failure,
    representative,
    stop_aggregate,
)
from app.services.data.store import daily as _daily
from app.services.data.store.frame import cached_frame, is_final_date, make_cache_key
from app.services.data.store.ledger import default_ledger


# 스토어 접근을 모듈 수준 얇은 래퍼로 감싼다 — 테스트가 실제 DB 없이 갈아끼울 수 있게.
def _store_ledger():
    return default_ledger()


def _store_write_daily(day, df, columns):
    _daily.write_daily(day, df, columns=columns)


def _store_read_daily(day, cols, out_columns):
    return _daily.read_daily(day, cols, out_columns=out_columns)


def _ymd_to_date(ymd: str) -> date:
    """'20190312' → date(2019, 3, 12)."""
    return datetime.strptime(ymd, "%Y%m%d").date()
```

파일 상단 import 에 `from datetime import date, datetime` 을 추가한다.

`_fetch_per_market`(38-68행)를 통째로 아래로 교체한다:

```python
def _fetch_per_market(
    fetch_one: Callable[[Any, str], pd.DataFrame | None],
    mkts: list[str],
    *,
    what: str,
    when: str,
    source: str,
    empty_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, bool]:
    """시장별 조회를 "루프 → 실패 수집 → concat" 골격으로 일반화한다.

    이전 판은 개별 시장 실패를 경고만 남기고 삼켜, **전 시장이 실패해도 빈 프레임을
    반환**했다. §44-1(KRX 차단)·§47(폐기된 검증)에서 백테스트가 빈 패널 위에서
    '성공'하며 무의미한 수치를 낸 원인이 이것이다.

    이제 전량 실패는 `representative()` 가 고른 대표 예외로 raise 한다. 일부만
    실패하면 성공분을 돌려주되 `complete=False` 로 알려, 호출자가 그 결과를 확정으로
    굳히지 않게 한다(다음 호출에서 빠진 시장을 보완할 수 있어야 한다).

    :param source: 쿨다운·집계 단락 판정에 쓰는 소스 식별자
    :returns: (합친 프레임, 전 시장 성공 여부)
    """
    stock = _pykrx_stock()
    frames: list[pd.DataFrame] = []
    errors: list[DataSourceError] = []
    ok = 0

    for mkt in mkts:
        if stop_aggregate(source, errors, ok):
            logger.warning("%s 집계 단락 (%s %s) — 남은 시장 건너뜀", what, mkt, when)
            break
        try:
            df = fetch_one(stock, mkt)
            ok += 1
            if df is None or df.empty:
                continue
            frames.append(df)
        except DataSourceError as e:
            logger.warning("%s 조회 실패 (%s %s): %s", what, mkt, when, e)
            note_failure(e)
            errors.append(e)
        except Exception as e:  # noqa: BLE001 - pykrx 는 임의 예외를 던진다
            wrapped = SourceUnavailableError(source, f"{what} 조회 실패({mkt} {when}): {e}")
            logger.warning("%s 조회 실패 (%s %s)", what, mkt, when, exc_info=True)
            note_failure(wrapped)
            errors.append(wrapped)

    if errors and ok == 0:
        raise representative(errors)

    complete = not errors and ok == len(mkts)
    if not frames:
        empty = pd.DataFrame(columns=empty_columns) if empty_columns else pd.DataFrame()
        return empty, complete
    return pd.concat(frames), complete
```

`_fetch_fundamentals`(79-115행)를 아래로 교체한다:

```python
def _fetch_fundamentals(as_of_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """전 종목 펀더멘털(PER/PBR/DIV)을 조회한다 — 로컬 우선.

    컬럼: PER, PBR, DIV + market. 티커 인덱스.
    PER=0 은 적자(undefined)로 간주해 NaN 으로 바꾼다.

    2단 캐시다. 1차는 프로세스 내 LRU(_FUND_CACHE, DB 왕복도 아낀다), 2차는
    stock_daily_snapshots. 확정된 과거 일자는 최초 1회만 pykrx 를 탄다.

    :raises DataSourceError: 전 시장 조회가 실패했을 때(빈 프레임을 돌려주지 않는다).
    """
    key = (as_of_ymd, tuple(sorted(mkts)))
    cached = _FUND_CACHE.get(key)
    if cached is not None:
        _FUND_CACHE.move_to_end(key)
        return cached.copy()

    day = _ymd_to_date(as_of_ymd)
    cols = {"PER": "per", "PBR": "pbr", "DIV": "div", "market": "market"}
    out_cols = {"per": "PER", "pbr": "PBR", "div": "DIV", "market": "market"}
    complete = True

    def _one(stock, mkt: str) -> pd.DataFrame | None:
        df = stock.get_market_fundamental(as_of_ymd, market=mkt)
        if df is None or df.empty:
            return None
        df = df[["PER", "PBR", "DIV"]].copy()
        df["market"] = mkt
        df.loc[df["PER"] <= 0, "PER"] = np.nan
        return df

    def _remote() -> pd.DataFrame:
        nonlocal complete
        df, complete = _fetch_per_market(
            _one, mkts, what="펀더멘털", when=as_of_ymd, source="krx",
            empty_columns=["PER", "PBR", "DIV", "market"],
        )
        return df

    result = cached_frame(
        "fundamentals",
        make_cache_key(as_of_ymd, mkts),
        read_local=lambda: _store_read_daily(day, list(out_cols), out_cols),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_daily(day, df, cols),
        # 부분 실패는 확정으로 굳히지 않는다 — 다음 호출에서 빠진 시장을 보완해야 한다.
        # 콜러블로 넘기는 이유: complete 는 _remote() 가 돈 뒤에야 정해진다.
        is_final=lambda: is_final_date(day) and complete,
        ledger=_store_ledger(),
    )

    if result.empty:
        return result

    _FUND_CACHE[key] = result.copy()
    _FUND_CACHE.move_to_end(key)
    if len(_FUND_CACHE) > _FUND_CACHE_MAX:
        _FUND_CACHE.popitem(last=False)
    return result
```

> **주의**: `is_final` 을 반드시 **콜러블로** 넘겨라. `complete` 는 `_remote()` 가
> 실행된 뒤에야 정해지는데, 값으로 넘기면 `cached_frame` 호출 시점에 — 즉
> `fetch_remote()` 가 돌기 전에 — 평가되어 언제나 초기값 `True` 가 박힌다. 그러면
> 부분 실패가 확정으로 굳어 빠진 시장이 영구히 채워지지 않는다.
> Task 4 의 `cached_frame` 이 이미 `bool | Callable[[], bool]` 을 받고
> `ledger.put()` 직전에 평가하므로, 호출부만 `lambda:` 로 감싸면 된다.

`_fetch_market_cap`(118-131행)을 아래로 교체한다:

```python
def _fetch_market_cap(as_of_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """전 종목 시가총액·상장주식수·거래대금을 조회한다 — 로컬 우선.

    컬럼: 시가총액, 거래량, 거래대금, 상장주식수. 티커 인덱스.

    :raises DataSourceError: 전 시장 조회가 실패했을 때.
    """
    day = _ymd_to_date(as_of_ymd)
    cols = {
        "시가총액": "market_cap", "거래량": "volume",
        "거래대금": "trading_value", "상장주식수": "shares",
    }
    out_cols = {
        "market_cap": "시가총액", "volume": "거래량",
        "trading_value": "거래대금", "shares": "상장주식수",
    }
    complete = True

    def _one(stock, mkt: str) -> pd.DataFrame | None:
        df = stock.get_market_cap(as_of_ymd, market=mkt)
        if df is None or df.empty:
            return None
        # 필요 컬럼만 선택 (pykrx 버전 차이 대비)
        keep = [c for c in ["시가총액", "거래량", "거래대금", "상장주식수"] if c in df.columns]
        return df[keep]

    def _remote() -> pd.DataFrame:
        nonlocal complete
        df, complete = _fetch_per_market(
            _one, mkts, what="시가총액", when=as_of_ymd, source="krx",
        )
        return df

    return cached_frame(
        "market_cap",
        make_cache_key(as_of_ymd, mkts),
        read_local=lambda: _store_read_daily(day, list(out_cols), out_cols),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_daily(day, df, cols),
        is_final=lambda: is_final_date(day) and complete,
        ledger=_store_ledger(),
    )
```

`_fetch_market_ohlcv_snapshot`(203-220행)을 아래로 교체한다:

```python
def _fetch_market_ohlcv_snapshot(date_ymd: str, mkt: str) -> pd.DataFrame | None:
    """단일 거래일의 전 종목 OHLCV 스냅샷을 조회한다 — 로컬 우선.

    컬럼: 시가/고가/저가/종가/거래량/거래대금/등락률. 티커 인덱스.
    패닉셀 S9(신저가 브레드스)가 날짜를 훑으며 부르므로 로컬 적재 효과가 가장 크다.

    :raises DataSourceError: 조회가 실패했을 때(이전 판은 None 을 돌려줬다).
    """
    day = _ymd_to_date(date_ymd)
    cols = {
        "시가": "open", "고가": "high", "저가": "low", "종가": "close",
        "거래량": "volume", "거래대금": "trading_value", "등락률": "change_pct",
    }
    out_cols = {v: k for k, v in cols.items()}

    def _remote() -> pd.DataFrame:
        stock = _pykrx_stock()
        try:
            with bounded_socket_timeout(20):
                df = stock.get_market_ohlcv(date_ymd, market=mkt)
        except DataSourceError as e:
            note_failure(e)
            raise
        except Exception as e:  # noqa: BLE001 - pykrx 는 임의 예외를 던진다
            wrapped = SourceUnavailableError(
                "krx", f"전종목 OHLCV 스냅샷 조회 실패({mkt} {date_ymd}): {e}"
            )
            note_failure(wrapped)
            raise wrapped from e
        return df if df is not None else pd.DataFrame()

    out = cached_frame(
        "market_ohlcv",
        make_cache_key(date_ymd, mkt),
        read_local=lambda: _store_read_daily(day, list(out_cols), out_cols),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_daily(day, df, cols),
        is_final=is_final_date(day),
        ledger=_store_ledger(),
    )
    return out if not out.empty else None
```

- [ ] **Step 4: 통과 확인**

```bash
docker compose exec -T web pytest tests/test_fetch_store_wiring.py tests/test_local_store_frame.py -v
```
Expected: PASS (16 passed)

- [ ] **Step 5: 기존 스위트 회귀 확인**

`_fetch_per_market` 의 반환 타입이 바뀌었고 실패가 예외가 되었다. 이 함수를 쓰는
`stocks.py`·`screener.py`·`recommend.py`·`factors.py` 경로 테스트가 깨질 수 있다.

```bash
docker compose exec -T web pytest -q 2>&1 | tail -30
```
Expected: 실패 0. 실패가 있으면 그 테스트가 "외부 전량 실패 시 빈 결과"를 기대하는지
확인하고, 기대를 `pytest.raises(DataSourceError)` 로 고친다 — **구현을 되돌리지 않는다.**
그것이 이 작업의 목적이다.

- [ ] **Step 6: 커밋**

```bash
git add backend/app/services/metrics/fetch.py backend/app/services/data/store/frame.py backend/tests/test_fetch_store_wiring.py
git commit -m "fix: 펀더멘털·시총·전종목 OHLCV 를 로컬 우선으로 바꾸고 전량 실패를 전파한다

_fetch_per_market 의 except Exception → 빈 프레임이 §44-1·§47 사고의 직접 원인이다.
전 시장 실패는 대표 예외로 raise 하고, 부분 실패는 성공분을 돌려주되 확정으로 굳히지
않아 다음 호출이 빠진 시장을 보완하게 한다. 확정 과거분은 stock_daily_snapshots 에서
읽어 pykrx 왕복 자체를 없앤다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: 기간 통계 리포지토리와 배선 (등락률·순매수)

**Files:**
- Create: `backend/app/services/data/store/periods.py`
- Modify: `backend/app/services/data/store/__init__.py`
- Modify: `backend/app/services/metrics/fetch.py` (`_fetch_price_change`·`_fetch_net_purchases`)
- Test: `backend/tests/test_local_store_repo.py` (이어 씀), `backend/tests/test_fetch_store_wiring.py` (이어 씀)

**Interfaces:**
- Consumes: `app.models.store.StockPeriodStat`, `LocalStoreSession`, `run_sync`, `cached_frame`, `make_cache_key`, `is_final_date`
- Produces:
  - `write_periods(start: date, end: date, investors: str, df: pd.DataFrame, *, columns: dict[str, str]) -> None`
  - `read_periods(start: date, end: date, investors: str, table_columns: list[str], *, out_columns: dict[str, str]) -> pd.DataFrame`
  - `delete_periods(start: date, end: date, investors: str) -> None`

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_local_store_repo.py` 끝에 이어 붙인다:

```python
from app.services.data.store import periods  # noqa: E402

_START = date(1990, 1, 2)
_END = date(1990, 1, 31)


@pytest.fixture(autouse=True)
def _cleanup_periods():
    yield
    periods.delete_periods(_START, _END, "")
    periods.delete_periods(_START, _END, "기관합계,외국인")


def test_기간통계를_쓰고_읽는다():
    df = pd.DataFrame({"등락률": [5.5], "거래대금": [1_000_000]}, index=["005930"])
    periods.write_periods(
        _START, _END, "", df, columns={"등락률": "change_pct", "거래대금": "trading_value"}
    )

    out = periods.read_periods(
        _START, _END, "", ["change_pct", "trading_value"],
        out_columns={"change_pct": "등락률", "trading_value": "거래대금"},
    )
    assert float(out.loc["005930", "등락률"]) == pytest.approx(5.5)


def test_투자자군이_다르면_다른_행이다():
    """등락률 행과 순매수 행이 서로를 덮어쓰면 안 된다."""
    periods.write_periods(
        _START, _END, "", pd.DataFrame({"등락률": [5.5]}, index=["005930"]),
        columns={"등락률": "change_pct"},
    )
    periods.write_periods(
        _START, _END, "기관합계,외국인",
        pd.DataFrame({"net_buy_value": [1e9]}, index=["005930"]),
        columns={"net_buy_value": "net_buy_value"},
    )

    pc = periods.read_periods(_START, _END, "", ["change_pct"], out_columns={"change_pct": "등락률"})
    npv = periods.read_periods(
        _START, _END, "기관합계,외국인", ["net_buy_value"],
        out_columns={"net_buy_value": "net_buy_value"},
    )
    assert float(pc.loc["005930", "등락률"]) == pytest.approx(5.5)
    assert float(npv.loc["005930", "net_buy_value"]) == pytest.approx(1e9)
```

`backend/tests/test_fetch_store_wiring.py` 끝에 이어 붙인다:

```python
def test_순매수_전량실패는_예외로_전파된다(_store, monkeypatch):
    """이전 판은 빈 프레임을 돌려줘 수급 팩터가 조용히 중립이 됐다."""

    class _Boom:
        def get_market_net_purchases_of_equities(self, *a, **kw):
            raise RuntimeError("차단")

    monkeypatch.setattr(F, "_pykrx_stock", lambda: _Boom())
    monkeypatch.setattr(F, "_store_write_periods", lambda *a, **kw: None)
    monkeypatch.setattr(F, "_store_read_periods", lambda *a, **kw: pd.DataFrame())

    with pytest.raises(DataSourceError):
        F._fetch_net_purchases("20190101", "20190131", ["KOSPI"])
```

- [ ] **Step 2: 실패 확인**

```bash
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v
docker compose exec -T web pytest tests/test_fetch_store_wiring.py -v
```
Expected: 둘 다 FAIL (`cannot import name 'periods'` / `no attribute '_store_write_periods'`)

- [ ] **Step 3: 리포지토리 구현**

`backend/app/services/data/store/periods.py`:

```python
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
```

`backend/app/services/data/store/__init__.py` 에 `from app.services.data.store import periods` 추가, `__all__` 에 `"periods"` 추가.

- [ ] **Step 4: `fetch.py` 배선**

`fetch.py` 의 래퍼 블록(Task 6 에서 추가한 `_store_read_daily` 아래)에 추가:

```python
from app.services.data.store import periods as _periods  # 상단 import 블록에 추가


def _store_write_periods(start, end, investors, df, columns):
    _periods.write_periods(start, end, investors, df, columns=columns)


def _store_read_periods(start, end, investors, cols, out_columns):
    return _periods.read_periods(start, end, investors, cols, out_columns=out_columns)
```

`_fetch_price_change`(134-145행)를 아래로 교체한다:

```python
def _fetch_price_change(start_ymd: str, end_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """기간 등락률·거래대금을 전 종목 일괄 조회한다 — 로컬 우선.

    컬럼: 시가, 종가, 등락률, 거래량, 거래대금. 티커 인덱스.
    등락률은 pykrx 원값 그대로(%) — 호출자가 /100 으로 변환한다.

    :raises DataSourceError: 전 시장 조회가 실패했을 때.
    """
    start_d, end_d = _ymd_to_date(start_ymd), _ymd_to_date(end_ymd)
    cols = {
        "시가": "open", "종가": "close", "등락률": "change_pct",
        "거래량": "volume", "거래대금": "trading_value",
    }
    out_cols = {v: k for k, v in cols.items()}
    complete = True

    def _one(stock, mkt: str) -> pd.DataFrame | None:
        return stock.get_market_price_change(start_ymd, end_ymd, market=mkt)

    def _remote() -> pd.DataFrame:
        nonlocal complete
        df, complete = _fetch_per_market(
            _one, mkts, what="가격변동", when=f"{start_ymd}~{end_ymd}", source="krx",
        )
        return df

    return cached_frame(
        "price_change",
        make_cache_key(start_ymd, end_ymd, mkts),
        read_local=lambda: _store_read_periods(start_d, end_d, "", list(out_cols), out_cols),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_periods(start_d, end_d, "", df, cols),
        is_final=lambda: is_final_date(end_d) and complete,
        ledger=_store_ledger(),
    )
```

`_fetch_net_purchases`(152-200행)의 본문을 아래로 교체한다(docstring 은 유지하되 마지막에
`:raises DataSourceError:` 한 줄 추가):

```python
    investors_key = ",".join(sorted(investors))
    start_d, end_d = _ymd_to_date(start_ymd), _ymd_to_date(end_ymd)
    complete = True

    def _remote() -> pd.DataFrame:
        nonlocal complete
        stock = _pykrx_stock()
        accum: dict[str, float] = {}
        errors: list[DataSourceError] = []
        ok = 0
        for mkt in mkts:
            for investor in investors:
                if stop_aggregate("krx", errors, ok):
                    logger.warning("순매수 집계 단락 (%s %s) — 남은 조회 건너뜀", mkt, investor)
                    break
                try:
                    df = stock.get_market_net_purchases_of_equities(
                        start_ymd, end_ymd, mkt, investor
                    )
                    ok += 1
                    if df is None or df.empty or "순매수거래대금" not in df.columns:
                        continue
                    vals = pd.to_numeric(df["순매수거래대금"], errors="coerce")
                    for ticker, v in vals.items():
                        if pd.isna(v):
                            continue
                        k = str(ticker).zfill(6)
                        accum[k] = accum.get(k, 0.0) + float(v)
                except DataSourceError as e:
                    logger.warning("투자자별 순매수 조회 실패 (%s %s): %s", mkt, investor, e)
                    note_failure(e)
                    errors.append(e)
                except Exception as e:  # noqa: BLE001 - pykrx 는 임의 예외를 던진다
                    wrapped = SourceUnavailableError(
                        "krx", f"순매수 조회 실패({mkt} {investor} {start_ymd}~{end_ymd}): {e}"
                    )
                    logger.warning(
                        "투자자별 순매수 조회 실패 (%s %s %s~%s)",
                        mkt, investor, start_ymd, end_ymd, exc_info=True,
                    )
                    note_failure(wrapped)
                    errors.append(wrapped)

        if errors and ok == 0:
            raise representative(errors)
        complete = not errors and ok == len(mkts) * len(investors)

        if not accum:
            out = pd.DataFrame(columns=["net_buy_value"])
            out.index.name = "티커"
            return out
        out = pd.DataFrame.from_dict(accum, orient="index", columns=["net_buy_value"])
        out.index.name = "티커"
        return out

    return cached_frame(
        "net_purchases",
        make_cache_key(start_ymd, end_ymd, mkts, investors),
        read_local=lambda: _store_read_periods(
            start_d, end_d, investors_key, ["net_buy_value"],
            out_columns={"net_buy_value": "net_buy_value"},
        ),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_periods(
            start_d, end_d, investors_key, df, {"net_buy_value": "net_buy_value"}
        ),
        is_final=lambda: is_final_date(end_d) and complete,
        ledger=_store_ledger(),
    )
```

- [ ] **Step 5: 통과 확인**

```bash
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v
docker compose exec -T web pytest tests/test_fetch_store_wiring.py -v
docker compose exec -T web pytest -q 2>&1 | tail -20
```
Expected: 전부 PASS, 전체 스위트 실패 0

- [ ] **Step 6: 커밋**

```bash
git add backend/app/services/data/store/periods.py backend/app/services/data/store/__init__.py backend/app/services/metrics/fetch.py backend/tests/test_local_store_repo.py backend/tests/test_fetch_store_wiring.py
git commit -m "feat: 기간 등락률·순매수를 로컬 우선으로 바꾼다

기간 등락률은 수정주가 기준이라 price_ticks 종가로 재유도하면 액면분할 구간에서
값이 갈린다. 원본 그대로 stock_period_stats 에 보관한다. 순매수 전량 실패도 이제
빈 프레임이 아니라 예외다 — 수급 팩터가 조용히 중립이 되던 경로를 막는다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: 지수 리포지토리와 배선 (지수 OHLCV·PIT 지수구성)

**Files:**
- Create: `backend/app/services/data/store/indexes.py`
- Modify: `backend/app/services/data/store/__init__.py`
- Modify: `backend/app/services/metrics/fetch.py` (`_fetch_index_ohlcv`)
- Modify: `backend/app/services/data/krx_index.py` (`index_members`)
- Test: `backend/tests/test_local_store_repo.py` (이어 씀)

**Interfaces:**
- Consumes: `app.models.store.IndexOhlcv`, `app.models.store.IndexConstituent`, `cached_frame`, `make_cache_key`, `is_final_date`
- Produces:
  - `write_index_ohlcv(index_code: str, df: pd.DataFrame, *, index_name: str | None = None) -> None` — df 는 날짜 인덱스, 컬럼 `open/high/low/close/volume/trading_value`
  - `read_index_ohlcv(index_code: str, start: date, end: date) -> pd.DataFrame` — 날짜 인덱스, 같은 컬럼
  - `write_constituents(index_code: str, base_date: date, symbols: list[str]) -> None`
  - `read_constituents(index_code: str, base_date: date) -> list[str]`
  - `delete_index_ohlcv(index_code: str) -> None`, `delete_constituents(index_code: str, base_date: date) -> None`

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_local_store_repo.py` 끝에 이어 붙인다:

```python
from app.services.data.store import indexes  # noqa: E402

_IDX = "9999"  # 실제 지수코드와 겹치지 않는 시험용 코드


@pytest.fixture(autouse=True)
def _cleanup_indexes():
    yield
    indexes.delete_index_ohlcv(_IDX)
    indexes.delete_constituents(_IDX, _DAY)


def test_지수_OHLCV_를_쓰고_기간으로_읽는다():
    df = pd.DataFrame(
        {"open": [100.0, 110.0], "high": [120.0, 130.0], "low": [90.0, 100.0],
         "close": [115.0, 125.0], "volume": [1000, 2000], "trading_value": [10, 20]},
        index=pd.to_datetime(["1990-01-02", "1990-01-03"]),
    )
    indexes.write_index_ohlcv(_IDX, df, index_name="시험지수")

    out = indexes.read_index_ohlcv(_IDX, date(1990, 1, 2), date(1990, 1, 3))
    assert len(out) == 2
    assert float(out.iloc[0]["close"]) == pytest.approx(115.0)
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "trading_value"]


def test_지수_OHLCV_기간_밖은_안_읽힌다():
    df = pd.DataFrame(
        {"open": [100.0], "high": [120.0], "low": [90.0],
         "close": [115.0], "volume": [1000], "trading_value": [10]},
        index=pd.to_datetime(["1990-01-02"]),
    )
    indexes.write_index_ohlcv(_IDX, df)
    assert indexes.read_index_ohlcv(_IDX, date(1990, 2, 1), date(1990, 2, 28)).empty


def test_PIT_지수구성을_쓰고_읽는다():
    indexes.write_constituents(_IDX, _DAY, ["005930", "000660"])
    assert sorted(indexes.read_constituents(_IDX, _DAY)) == ["000660", "005930"]


def test_지수구성_미적재는_빈_목록이다():
    assert indexes.read_constituents(_IDX, date(1991, 5, 5)) == []
```

- [ ] **Step 2: 실패 확인**

Run: `docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v`
Expected: FAIL — `cannot import name 'indexes'`

- [ ] **Step 3: 리포지토리 구현**

`backend/app/services/data/store/indexes.py`:

```python
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
```

`backend/app/services/data/store/__init__.py` 에 `from app.services.data.store import indexes` 추가, `__all__` 에 `"indexes"` 추가.

- [ ] **Step 4: `_fetch_index_ohlcv` 배선**

`fetch.py` 의 래퍼 블록에 추가:

```python
from app.services.data.store import indexes as _indexes  # 상단 import 블록에 추가


def _store_write_index_ohlcv(code, df, name):
    _indexes.write_index_ohlcv(code, df, index_name=name)


def _store_read_index_ohlcv(code, start, end):
    return _indexes.read_index_ohlcv(code, start, end)
```

`_fetch_index_ohlcv`(223-245행)를 아래로 교체한다:

```python
def _fetch_index_ohlcv(start_ymd: str, end_ymd: str, ticker: str) -> pd.DataFrame | None:
    """지수 OHLCV 를 조회한다 — 로컬 우선. 데이터가 없으면 None.

    pykrx 한글 컬럼 → 영문 변환:
      시가→open, 고가→high, 저가→low, 종가→close,
      거래량→volume, 거래대금→trading_value

    :raises DataSourceError: 조회가 실패했을 때(이전 판은 None 을 돌려줬다).
    """
    start_d, end_d = _ymd_to_date(start_ymd), _ymd_to_date(end_ymd)

    def _remote() -> pd.DataFrame:
        stock = _pykrx_stock()
        try:
            with bounded_socket_timeout(20):
                df = stock.get_index_ohlcv(start_ymd, end_ymd, ticker)
        except DataSourceError as e:
            note_failure(e)
            raise
        except Exception as e:  # noqa: BLE001 - pykrx 는 임의 예외를 던진다
            wrapped = SourceUnavailableError(
                "krx", f"지수 OHLCV 조회 실패({ticker} {start_ymd}~{end_ymd}): {e}"
            )
            # 패닉·섹터 지표의 핵심 입력이라 원인 스택을 운영 로그에 남긴다.
            logger.warning("지수 OHLCV 조회 실패 (%s)", ticker, exc_info=True)
            note_failure(wrapped)
            raise wrapped from e
        if df is None or df.empty:
            return pd.DataFrame(columns=_indexes.OHLCV_COLUMNS)
        return df.rename(columns={
            "시가": "open", "고가": "high", "저가": "low", "종가": "close",
            "거래량": "volume", "거래대금": "trading_value",
        })

    out = cached_frame(
        "index_ohlcv",
        make_cache_key(ticker, start_ymd, end_ymd),
        read_local=lambda: _store_read_index_ohlcv(ticker, start_d, end_d),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_index_ohlcv(ticker, df, None),
        is_final=is_final_date(end_d),
        ledger=_store_ledger(),
    )
    return out if not out.empty else None
```

- [ ] **Step 5: `krx_index.index_members` 배선**

`backend/app/services/data/krx_index.py` 의 `index_members`(239행~)를 수정한다.
기존 `_MEMBERS_CACHE` 조회(256-257행)와 저장(300행) 사이에 스토어를 끼운다. 기존
`if key in _MEMBERS_CACHE: return _MEMBERS_CACHE[key]` **바로 다음**에 삽입:

```python
    # 2차 캐시: 로컬 영구 저장소. PIT 지수구성은 확정 후 불변이므로 한 번 적재되면
    # KRX 로그인 없이도 조회된다(§44-1 차단 시 백테스트가 살아남는다).
    from app.services.data.store import indexes as _indexes
    from app.services.data.store.frame import is_final_date, make_cache_key
    from app.services.data.store.ledger import default_ledger

    _store_key = make_cache_key(index, as_of)
    _entry = default_ledger().get("index_members", _store_key)
    if _entry is not None and _entry.final:
        codes = _indexes.read_constituents(index, as_of)
        _MEMBERS_CACHE[key] = codes
        return codes
```

그리고 기존 `_MEMBERS_CACHE[key] = codes`(300행) **바로 앞**에 삽입:

```python
    _indexes.write_constituents(index, as_of, codes)
    default_ledger().put(
        "index_members", _store_key, row_count=len(codes), final=is_final_date(as_of)
    )
```

- [ ] **Step 6: 통과 확인**

```bash
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v
docker compose exec -T web pytest -q 2>&1 | tail -20
```
Expected: 전부 PASS, 전체 스위트 실패 0

- [ ] **Step 7: 커밋**

```bash
git add backend/app/services/data/store/indexes.py backend/app/services/data/store/__init__.py backend/app/services/metrics/fetch.py backend/app/services/data/krx_index.py backend/tests/test_local_store_repo.py
git commit -m "feat: 지수 OHLCV 와 PIT 지수구성을 로컬 우선으로 바꾼다

PIT 지수구성이 로컬에 있으면 KRX 로그인이 차단돼도 그 시점 유니버스는 살아남는다.
§44-1 에서 전 종목이 0개가 되어 백테스트가 빈 패널 위에서 '성공'하던 경로가 닫힌다.
지수 OHLCV 실패도 None 이 아니라 예외로 올린다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: DART 재무 리포지토리와 배선 (90일 확정 유예)

**Files:**
- Create: `backend/app/services/data/store/dart_store.py`
- Modify: `backend/app/services/data/store/__init__.py`
- Modify: `backend/app/services/data/opendart.py` (`single_company_accounts` 호출 경로)
- Test: `backend/tests/test_local_store_repo.py` (이어 씀), `backend/tests/test_opendart.py` (이어 씀)

**Interfaces:**
- Consumes: `app.models.store.DartFinancial`, `LocalStoreSession`, `run_sync`
- Produces:
  - `confirmed_date(rcept_dt: date | None, bsns_year: int) -> date` — 확정일 = 접수일+90일, 접수일 미상이면 사업연도 말일+1년
  - `read_accounts(corp_code, bsns_year, reprt_code, fs_div) -> tuple[list[dict], bool] | None` — `(원계정, 확정여부)`. 미적재면 `None`
  - `write_accounts(corp_code, bsns_year, reprt_code, fs_div, accounts: list[dict]) -> None`
  - `delete_accounts(corp_code, bsns_year, reprt_code, fs_div) -> None`

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_local_store_repo.py` 끝에 이어 붙인다:

```python
from app.services.data.store import dart_store  # noqa: E402

_CORP = "00000000"  # 실제 corp_code 와 겹치지 않는 시험값


@pytest.fixture(autouse=True)
def _cleanup_dart():
    yield
    dart_store.delete_accounts(_CORP, 1990, "11011", "CFS")


def test_원계정을_쓰고_읽는다():
    accounts = [{"account_nm": "자산총계", "thstrm_amount": "1000", "rcept_no": "19910301000001"}]
    dart_store.write_accounts(_CORP, 1990, "11011", "CFS", accounts)

    got = dart_store.read_accounts(_CORP, 1990, "11011", "CFS")
    assert got is not None
    rows, final = got
    assert rows[0]["account_nm"] == "자산총계"
    assert final is True  # 1991년 접수 + 90일은 이미 한참 지났다


def test_미적재는_None_이다():
    assert dart_store.read_accounts(_CORP, 1989, "11011", "CFS") is None
```

`backend/tests/test_opendart.py` 끝에 이어 붙인다(확정일 계산은 DB 없이 검증 가능):

```python
from datetime import date

from app.services.data.store.dart_store import confirmed_date


def test_확정일은_접수일_90일_후다():
    assert confirmed_date(date(2026, 3, 20), 2025) == date(2026, 6, 18)


def test_접수일_미상이면_사업연도_말_1년_후다():
    """보수적으로 잡는다 — 확정을 앞당기는 것보다 늦추는 쪽이 안전하다."""
    assert confirmed_date(None, 2025) == date(2026, 12, 31)
```

- [ ] **Step 2: 실패 확인**

```bash
docker compose exec -T web pytest tests/test_opendart.py -k 확정일 -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.data.store.dart_store'`

- [ ] **Step 3: 구현**

`backend/app/services/data/store/dart_store.py`:

```python
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
    """원계정을 저장한다. 빈 목록은 저장하지 않는다(원장이 '무자료'를 기록한다)."""
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
```

`backend/app/services/data/store/__init__.py` 에 `from app.services.data.store import dart_store` 추가, `__all__` 에 `"dart_store"` 추가.

- [ ] **Step 4: `opendart.py` 배선**

`single_company_accounts` 는 그대로 두고, 이미 캐시 계층인 `annual_metrics`·
`_period_metrics` 가 공유하는 `_ACCOUNTS_CACHE` 경로에 스토어를 끼운다. `opendart.py`
에 아래 함수를 `_ACCOUNTS_CACHE` 선언(578행) 다음에 추가한다:

```python
def cached_accounts(
    corp_code: str, bsns_year: int, reprt_code: str, fs_div: str
) -> list[dict] | None:
    """원계정을 2단 캐시(프로세스 → 로컬 DB) 뒤에서 가져온다.

    확정된 보고서(접수일 + 90일 경과)는 로컬에서만 읽는다. 미확정이면 정정공시가
    아직 들어올 수 있으므로 재조회한다.

    실패는 single_company_accounts 의 DataSourceError 를 그대로 전파한다(§48).
    """
    key = (corp_code, bsns_year, reprt_code, fs_div)
    cached = _ACCOUNTS_CACHE.get(key)
    if cached is not None:
        return cached

    from app.services.data.store import dart_store

    stored = dart_store.read_accounts(corp_code, bsns_year, reprt_code, fs_div)
    if stored is not None and stored[1]:  # 확정분
        _ACCOUNTS_CACHE[key] = stored[0]
        return stored[0]

    acc = single_company_accounts(corp_code, bsns_year, reprt_code, fs_div)
    if acc:
        dart_store.write_accounts(corp_code, bsns_year, reprt_code, fs_div, acc)
        _ACCOUNTS_CACHE[key] = acc
    return acc
```

그리고 `annual_metrics`(596행~)와 `_period_metrics`(636행~) 안에서
`_ACCOUNTS_CACHE.get(acc_key)` → `single_company_accounts(...)` → `_ACCOUNTS_CACHE[acc_key] = acc`
로 이어지는 세 줄짜리 블록을 각각 아래 한 줄로 교체한다:

```python
        acc = cached_accounts(corp_code, bsns_year, REPORT_ANNUAL, fs_div)
```
(`_period_metrics` 쪽은 `REPORT_ANNUAL` 대신 그 함수의 `reprt_code` 인자를 넘긴다.)

- [ ] **Step 5: 통과 확인**

```bash
docker compose exec -T web pytest tests/test_opendart.py -v
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v
docker compose exec -T web pytest -q 2>&1 | tail -20
```
Expected: 전부 PASS, 전체 스위트 실패 0

- [ ] **Step 6: 커밋**

```bash
git add backend/app/services/data/store/dart_store.py backend/app/services/data/store/__init__.py backend/app/services/data/opendart.py backend/tests/test_local_store_repo.py backend/tests/test_opendart.py
git commit -m "feat: DART 원계정을 로컬에 영구 저장한다(접수일+90일 확정 유예)

파생지표가 아니라 원계정을 저장한다 — derive_metrics 가 바뀌어도 원계정은 안 낡는다.
시장데이터와 달리 정정공시가 있으므로 접수일+90일이 지나야 굳히고 그 전엔 재조회를
허용한다. 일일 20,000건 한도를 백테스트 한 번에 태우던 구조가 사라진다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: 야간 배치 선적재

**Files:**
- Modify: `backend/worker/tasks.py`
- Modify: `backend/worker/celery_app.py`
- Test: `backend/tests/test_worker_snapshots.py` (신규)

**Interfaces:**
- Consumes: `_fetch_fundamentals`·`_fetch_market_cap`·`_fetch_market_ohlcv_snapshot`·`_fetch_index_ohlcv` (Task 6·8), `app.services.data.errors.DataSourceError`
- Produces: Celery 태스크 `worker.ingest_daily_snapshots` — 인자 없음, 전날 확정분을 선적재하고 `{"date": str, "ok": int, "failed": int}` 반환

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_worker_snapshots.py`:

```python
"""야간 선적재 태스크 검증 — 외부는 전부 대역, 호출 여부와 실패 집계만 본다."""
from datetime import date

import pytest

from app.services.data.errors import SourceUnavailableError
from worker import tasks


def test_전날_확정분을_적재한다(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [("펀더멘털", lambda: called.append("fund")),
                     ("시가총액", lambda: called.append("cap"))],
    )

    out = tasks.ingest_daily_snapshots()

    assert called == ["fund", "cap"]
    assert out == {"date": "20260805", "ok": 2, "failed": 0}


def test_일부_실패해도_나머지를_계속한다(monkeypatch):
    """한 종류가 막혔다고 나머지 선적재를 포기하면 다음 백테스트가 그만큼 더 조회한다."""

    def _boom():
        raise SourceUnavailableError("krx", "차단")

    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [("펀더멘털", _boom), ("시가총액", lambda: None)],
    )

    out = tasks.ingest_daily_snapshots()

    assert out == {"date": "20260805", "ok": 1, "failed": 1}
```

- [ ] **Step 2: 실패 확인**

Run: `docker compose exec -T web pytest tests/test_worker_snapshots.py -v`
Expected: FAIL — `AttributeError: module 'worker.tasks' has no attribute '_snapshot_target_date'`

- [ ] **Step 3: 구현**

`backend/worker/tasks.py` 끝에 추가한다:

```python
def _snapshot_target_date() -> date:
    """선적재 대상 일자 — 전날(확정된 마지막 날).

    당일분은 장중 값이 계속 바뀌어 확정으로 굳힐 수 없으므로(스토어 설계 §6)
    배치는 전날까지만 다룬다.
    """
    return date.today() - timedelta(days=1)


def _snapshot_steps(ymd: str) -> list[tuple[str, "Callable[[], object]"]]:
    """선적재 단계 목록 — (이름, 호출) 쌍.

    별도 함수로 뽑은 이유는 테스트가 실제 pykrx 를 부르지 않고 갈아끼우기 위함이다.
    """
    from app.services.metrics.fetch import (
        _fetch_fundamentals,
        _fetch_index_ohlcv,
        _fetch_market_cap,
        _fetch_market_ohlcv_snapshot,
    )

    mkts = ["KOSPI", "KOSDAQ"]
    steps: list[tuple[str, object]] = [
        ("펀더멘털", lambda: _fetch_fundamentals(ymd, mkts)),
        ("시가총액", lambda: _fetch_market_cap(ymd, mkts)),
    ]
    for mkt in mkts:
        steps.append((f"전종목OHLCV({mkt})", lambda m=mkt: _fetch_market_ohlcv_snapshot(ymd, m)))
    # 1001=KOSPI, 2001=KOSDAQ 대표지수 — 레짐·패닉 지표의 기준선.
    for code in ("1001", "2001"):
        steps.append((f"지수OHLCV({code})", lambda c=code: _fetch_index_ohlcv(ymd, ymd, c)))
    return steps


@celery_app.task(name="worker.ingest_daily_snapshots")
def ingest_daily_snapshots() -> dict:
    """전날 확정분을 로컬 저장소에 선적재한다.

    온디맨드 write-through 만으로도 저장소는 채워지지만, 그러면 그 날짜를 처음 밟는
    백테스트가 대기 비용을 전부 문다. 배치가 미리 채워두면 장중 조회가 사라진다.

    한 종류가 실패해도 나머지를 계속한다 — 부분 선적재라도 다음 백테스트의 외부
    조회를 그만큼 줄인다. 실패는 집계해 로그로 남긴다.
    """
    from app.services.data.errors import DataSourceError

    target = _snapshot_target_date()
    ymd = target.strftime("%Y%m%d")
    ok = failed = 0

    for name, call in _snapshot_steps(ymd):
        try:
            call()
            ok += 1
        except DataSourceError as e:
            failed += 1
            logger.warning("선적재 실패 [%s] %s: %s", ymd, name, e)
        except Exception:  # noqa: BLE001 - 한 단계 실패가 배치 전체를 멈추면 안 된다
            failed += 1
            logger.warning("선적재 실패 [%s] %s", ymd, name, exc_info=True)

    logger.info("일별 스냅샷 선적재 완료 [%s] ok=%d failed=%d", ymd, ok, failed)
    return {"date": ymd, "ok": ok, "failed": failed}
```

`worker/tasks.py` 상단 import 에 `Callable` 이 필요하다 — 12행의
`from datetime import date, datetime, time, timedelta, timezone` 은 그대로 두고,
파일 상단에 `from collections.abc import Callable` 을 추가한다.

`backend/worker/celery_app.py` 의 `beat_schedule` 에 추가한다 — 기존
`"ingest-daily-ohlcv"` 항목(18:30) **다음**에:

```python
    # 로컬 영구 저장소 선적재 — 일봉 적재(18:30) 직후. 온디맨드 write-through 만으로도
    # 저장소는 채워지지만, 그러면 그 날짜를 처음 밟는 백테스트가 대기 비용을 다 문다.
    "ingest-daily-snapshots": {
        "task": "worker.ingest_daily_snapshots",
        "schedule": crontab(hour=18, minute=50),
    },
```

- [ ] **Step 4: 통과 확인**

```bash
docker compose exec -T web pytest tests/test_worker_snapshots.py -v
docker compose exec -T web pytest -q 2>&1 | tail -20
```
Expected: PASS (2 passed), 전체 스위트 실패 0

- [ ] **Step 5: 워커 재기동·태스크 등록 확인**

```bash
docker compose restart worker
docker compose exec -T worker celery -A worker.celery_app.celery_app inspect registered 2>&1 | grep ingest_daily_snapshots
```
Expected: `worker.ingest_daily_snapshots` 가 출력된다

- [ ] **Step 6: 커밋**

```bash
git add backend/worker/tasks.py backend/worker/celery_app.py backend/tests/test_worker_snapshots.py
git commit -m "feat: 전날 확정분을 야간에 선적재한다

온디맨드 write-through 만으로도 저장소는 채워지지만 그 날짜를 처음 밟는 백테스트가
대기 비용을 전부 문다. 일봉 적재 직후(18:50) 배치로 미리 채워 장중 조회를 없앤다.
한 종류가 실패해도 나머지를 계속한다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: 호출자 영향 정리와 문서 갱신

`fetch.py` 계열이 이제 예외를 던진다. 이를 소비하는 라우트·러너가 그 예외를 어떻게
다루는지 확정하고 문서에 반영한다.

**Files:**
- Modify: `backend/app/services/metrics/panic.py` (`_fetch_market_ohlcv_snapshot`·`_fetch_index_ohlcv` 반환 계약 변경 반영)
- Modify: `backend/app/services/metrics/sectors.py` (`_fetch_index_ohlcv` 반환 계약 변경 반영)
- Modify: `backend/app/services/metrics/factors.py` (낡은 docstring 정리 — Task 6·7·8 리뷰 이월)
- Modify: `backend/engine/rebalance_runner.py` (`_is_risk_off` docstring — Task 8 리뷰 이월)
- Modify: `backend/app/api/routes/backtests.py` (`_provider_with_flow` 의 `except Exception` — Task 7 리뷰 이월)
- Modify: `docs/improvements.md`
- Modify: `CLAUDE.md`
- Test: `backend/tests/test_caller_degradation.py` (이어 씀)

**Task 6·7·8 리뷰에서 이월된 필수 판단 항목** (Step 1 에서 반드시 결론 낼 것):
- **(중요)** `api/routes/backtests.py:220-223` `_provider_with_flow` 가 `compute_flow_norm` 을
  `try/except Exception: flow = None` 으로 감싼다 — §47 사고 패턴이 이 진입 경로에 그대로 남아 있다.
- `factors.compute_flow_norm` 이 `npf.empty` 체크 **전에** 무조건 `_fetch_market_cap` 을
  호출한다(`factors.py:262-269`). 순매수와 무관하게 flow 팩터 전체가 죽을 수 있다.
- `fetch._fetch_net_purchases` docstring 의 "전량 실패면 빈 프레임을 반환한다"가 같은 블록의
  `:raises:` 와 정면 모순. `factors.compute_flow_norm` docstring 도 동일.
- `factors.py:189-190` `compute_residual_momentum_provider` docstring("조회 실패는 빈/부분
  Series 반환")이 낡음 — 206행은 이제 예외를 전파한다.
- `engine/rebalance_runner.py:686-703` `_is_risk_off` docstring("기준지수 조회 실패 시
  False=투자 유지")이 낡음. 실거래 엔진에서 KRX 일시 장애 시 이전엔 "레짐만 건너뛰고 틱 계속"
  이었으나 이제 "틱 전체 실패 기록 후 조기 종료"(`base_runner.py:131-147` 이 흡수해 크래시는
  없음). **이 동작 변경이 의도된 정책인지 먼저 판단하고, 그 결론대로 코드 또는 문서를 맞출 것.**
- `factors.compute_flow_norm` 의 `if npf is None or npf.empty` 분기는 이제 "로컬 스토어가 0행을
  확정 기록한 경우"에만 도달하는 사실상 죽은 경로.

**Interfaces:**
- Consumes: Task 6·7·8 이 바꾼 `fetch.py` 공개 함수들
- Produces: 문서 외 신규 인터페이스 없음

- [ ] **Step 1: 예외 전파 경로 점검**

이 함수들을 부르는 곳을 전부 훑고, 각 지점이 `DataSourceError` 를 (a) 그대로 올릴지
(b) 잡아서 부분 저하로 처리할지 정한다.

```bash
grep -rn "_fetch_fundamentals\|_fetch_market_cap\|_fetch_price_change\|_fetch_net_purchases\|_fetch_market_ohlcv_snapshot\|_fetch_index_ohlcv" backend/app backend/engine backend/scripts --include=*.py | grep -v "metrics/fetch.py"
```

판단 기준:
- **백테스트·리밸런싱 경로**(`factors.py`, `rebalance_runner.py`, `stocks.py`) → **그대로 올린다.** 빈 입력 위에서 '성공'하는 것이 §44-1·§47 사고의 본체다.
- **조회 화면 라우트**(`screener.py`, `recommend.py`, `backtests.py` 의 벤치마크) → 그대로 올린다. `app/main.py` 의 핸들러가 Request→500, Schema→502, 그 외→503 으로 변환한다.
- **보조 지표**(`panic.py` 의 S9 브레드스, `sectors.py` 의 개별 업종) → 개별 항목 실패는
  잡아서 그 항목만 빼되, **전량 실패는 올린다.**

- [ ] **Step 2: `panic.py` 수정**

245행 `snap = _fetch_market_ohlcv_snapshot(as_of_ymd, mkt)` 를 감싼다. S9 는 날짜를
훑는 보조 지표라 하루가 빠져도 나머지로 계산할 수 있다:

```python
        try:
            snap = _fetch_market_ohlcv_snapshot(as_of_ymd, mkt)
        except DataSourceError as e:
            # S9 는 여러 날짜의 누적으로 계산하므로 하루가 빠져도 나머지로 굴러간다.
            # 다만 전량 실패는 아래 "유효 표본 부족" 가드가 잡아 신호를 내지 않는다.
            logger.warning("S9 스냅샷 건너뜀 (%s %s): %s", mkt, as_of_ymd, e)
            snap = None
```

`panic.py` 상단 import 에 `from app.services.data.errors import DataSourceError` 를 추가한다.

355·492행의 `_fetch_index_ohlcv` 는 **감싸지 않는다** — 지수는 패닉 신호의 기준선이라
없으면 어떤 신호도 낼 수 없다. 366·523행의 `_fetch_price_change` 도 감싸지 않는다.

- [ ] **Step 3: `sectors.py` 수정**

`_compute_one_sector`(88행) 안의 96행 `df = _fetch_index_ohlcv(hist_start_ymd, date_ymd, ticker)`
는 업종 하나당 한 번 불린다. 개별 업종 실패로 전체 로테이션이 죽으면 안 되므로 감싼다:

```python
    try:
        df = _fetch_index_ohlcv(hist_start_ymd, date_ymd, ticker)
    except DataSourceError as e:
        logger.warning("업종지수 건너뜀 (%s): %s", ticker, e)
        return None
```

54행의 기준지수(`ref_ticker`)는 **감싸지 않는다** — 상대강도의 분모라 없으면 계산 자체가
불가능하다.

`sectors.py` 상단 import 에 `from app.services.data.errors import DataSourceError` 를 추가한다.

- [ ] **Step 4: 저하 동작 테스트 추가**

`backend/tests/test_caller_degradation.py` 끝에 이어 붙인다:

```python
from app.services.data.errors import SourceUnavailableError


def test_업종_하나가_막혀도_로테이션은_계속한다(monkeypatch):
    """개별 업종 실패로 전체 섹터 로테이션이 죽으면 안 된다."""
    from app.services.metrics import sectors

    monkeypatch.setattr(
        sectors, "_fetch_index_ohlcv",
        lambda *a, **kw: (_ for _ in ()).throw(SourceUnavailableError("krx", "일시장애")),
    )
    got = sectors._compute_one_sector(
        "1001", "KOSPI", "20260806", "20260101", ref_return=0.01
    )
    assert got is None
```

- [ ] **Step 5: `docs/improvements.md` 갱신**

§47 항목에 아래 문단을 덧붙인다:

```markdown
**해소 경로(2026-08-06)**: 원인이던 `metrics/fetch.py` 의 조용한 실패를 걷어내고
확정 과거 데이터를 로컬에 영구 저장하는 작업으로 닫는다. 설계는
`docs/superpowers/specs/2026-08-06-local-persistent-store-design.md`, 계획은
`docs/superpowers/plans/2026-08-06-local-persistent-store.md`. §47 재검증은 이
작업이 끝나고 pykrx 차단이 풀린 뒤에 다시 돌린다.
```

새 항목 §49 를 문서 끝에 추가한다:

```markdown
## §49 확정 과거 데이터의 로컬 영구 저장 (완료: 2026-08-06)

백테스트 입력 6종(펀더멘털·시가총액·기간등락률/순매수·지수 및 전종목 OHLCV·PIT
지수구성·DART 재무)을 5개 정규화 테이블 + 페치 원장(`external_fetches`)에 영구
저장하고, 조회를 `cached_frame` 한 진입점으로 통일했다.

핵심은 원장이다. 정규화 테이블만으로는 "휴장일이라 0행"과 "아직 적재 안 됨"이 같은
값이라, 저장소를 붙여도 §48 이 닫으려던 조용한 실패가 그대로 재현된다.

부수 효과로 `_fetch_per_market` 의 `except Exception → 빈 프레임` 이 사라졌다.
전 시장 실패는 이제 `representative()` 대표 예외로 raise 되고, 부분 실패는 성공분을
돌려주되 확정으로 굳히지 않는다.

**남은 한계**: 최초 적재는 여전히 외부 가용성에 달려 있다. pykrx 차단 중에는 미적재
구간의 백테스트가 `DataSourceError` 로 멈춘다 — 의도한 동작이며, 조용히 빈 값으로
완주하던 이전보다 낫다.
```

- [ ] **Step 6: `CLAUDE.md` 갱신**

"필수 함정" 절의 마지막 항목 다음에 한 줄 추가:

```markdown
- 확정 과거 데이터(펀더멘털·시총·OHLCV·PIT구성·DART재무)는 Postgres 에 영구 저장돼
  로컬 우선으로 읽힌다. 조회 계약은 `app/services/data/store/frame.py`, 강제 재적재는
  각 리포지토리의 `delete_*` 후 `external_fetches` 행 삭제.
```

- [ ] **Step 7: 전체 검증**

```bash
docker compose restart web worker engine
docker compose exec -T web pytest -q 2>&1 | tail -20
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -q 2>&1 | tail -10
```
Expected: 둘 다 실패 0

- [ ] **Step 8: 커밋**

```bash
git add backend/app/services/metrics/panic.py backend/app/services/metrics/sectors.py backend/tests/test_caller_degradation.py docs/improvements.md CLAUDE.md
git commit -m "docs: 로컬 저장소 도입에 따른 호출자 저하 동작과 문서를 정리한다

보조 지표(S9 브레드스·개별 업종)는 항목 단위 실패를 흡수하되 기준선(지수·기준업종)
실패는 그대로 올린다 — 기준선이 없으면 신호 자체가 성립하지 않는다. §47 에 해소
경로를, §49 에 이번 작업을 기록한다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. 스펙 커버리지**

| 스펙 절 | 담당 태스크 |
|---|---|
| §4.1 `stock_daily_snapshots` | Task 2(스키마), Task 5(리포), Task 6(배선) |
| §4.2 `stock_period_stats` | Task 2, Task 7 |
| §4.3 `index_ohlcv` | Task 2, Task 8 |
| §4.4 `index_constituents` | Task 2, Task 8 |
| §4.5 `dart_financials` | Task 2, Task 9 |
| §4.6 `external_fetches` 원장 + cache_key 규약 | Task 2, Task 3, Task 4(`make_cache_key`) |
| §5 4상태 조회 계약 | Task 4(코어), Task 6(`_fetch_per_market` 전환) |
| §6 확정·정정 규칙 | Task 4(`is_final_date`), Task 9(`confirmed_date`) |
| §7 온디맨드 write-through | Task 6·7·8·9 |
| §7 야간 배치 | Task 10 |
| §8 NullPool 전용 엔진 | Task 1 |
| §9 프로세스 내 1차 캐시 유지 | Task 6(`_FUND_CACHE`), Task 8(`_MEMBERS_CACHE`), Task 9(`_ACCOUNTS_CACHE`) |
| §10 테스트 6시나리오 | Task 4(①②③④⑤), Task 6(⑥ 부분 실패) |
| §11 검증 조건(§47 재검증) | Task 11 Step 5 에 문서화. 실행은 pykrx 차단 해제 후 별건 |

빠진 것 없음. §2 비목표(`corp_code_map`·`panic.py` 파일 캐시·`price_ticks`)는 의도적으로 어떤 태스크에도 없다.

**2. 플레이스홀더 점검**

모든 스텝에 실제 코드가 들어 있다. Task 11 이 손대는 `sectors.py` 함수명은
`_compute_one_sector`(88행, 시그니처 `(ticker, mkt, date_ymd, hist_start_ymd, ref_return)`)
로 확인·확정했다.

**3. 타입 정합**

- `cached_frame` 의 `is_final` 은 Task 4 에서부터 `bool | Callable[[], bool]` 이다.
  단일 조회(Task 6 의 `_fetch_market_ohlcv_snapshot`, Task 8 의 `_fetch_index_ohlcv`)는
  실패하면 raise 하므로 부분 실패 개념이 없어 `bool` 을 그대로 넘기고, 시장별 집계
  (Task 6 의 펀더멘털·시총, Task 7 의 등락률·순매수)는 `complete` 가 조회 뒤에 정해지므로
  `lambda:` 로 넘긴다. 두 형태가 공존하는 것이 의도다.
- `_fetch_per_market` 은 이제 `tuple[pd.DataFrame, bool]` 을 반환한다. 이 함수를 쓰는
  곳은 `fetch.py` 내부뿐(Task 6·7 이 전부 갱신)이며 외부 호출자는 없다 —
  Step 1 의 grep 결과로 확인됨.
- `daily.read_daily`/`periods.read_periods` 는 `out_columns` 매핑 이름이 같고,
  `indexes.read_index_ohlcv` 만 고정 컬럼(`OHLCV_COLUMNS`)을 쓴다. 지수는 컬럼 집합이
  불변이라 매핑이 불필요하다 — 의도된 차이.
- `LedgerEntry(row_count, final)` 의 필드명은 Task 3 정의 이후 Task 4·6·8 에서 동일하게 쓰인다.

---

**Plan complete and saved to `docs/superpowers/plans/2026-08-06-local-persistent-store.md`.**
