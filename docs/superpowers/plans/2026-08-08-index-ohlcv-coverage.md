# 지수 OHLCV 구간 커버리지 조회 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 지수 OHLCV 조회가 "요청 범위 정확일치"가 아니라 "확보 구간에 포함되면 히트"로 동작하게 해, pykrx 가 막혀도 이미 받아 둔 구간의 백테스트·레짐·패닉이 굴러가게 한다.

**Architecture:** 새 테이블 `index_ohlcv_coverage` 에 **요청 범위**를 확정분으로 잘라 기록하고(받아온 행의 범위가 아니다 — 그래야 거래일 달력이 필요 없다), `frame.py` 에 `cached_frame` 의 형제인 `cached_range()` 를 두어 커버 판정·병합·실패 메시지를 한 곳에 모은다. `_fetch_index_ohlcv` 만 그 진입점으로 갈아타고 호출자는 하나도 바뀌지 않는다. 야간 배치가 400 거래일 구간을 미리 확보해 가용성을 운에 맡기지 않는다.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 async (asyncpg) · Alembic · PostgreSQL/TimescaleDB · pandas · Celery · pytest

설계 문서: `docs/superpowers/specs/2026-08-08-index-ohlcv-coverage-design.md`

## Global Constraints

- 주석·docstring·커밋 메시지·문서는 **한국어**. 식별자는 영어. (`docs/CONVENTIONS.md` §0)
- 커밋 트레일러: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **커밋만 하고 push 하지 않는다.** main 직접 커밋 금지 — 작업 브랜치에서 진행한다.
- `from __future__ import annotations` + 내장 제네릭 타입힌트. 공개 함수는 시그니처에 타입 필수.
- **테스트는 실 KRX/DART/KOFIA 를 타지 않는다.** `backend/tests/conftest.py` 가 자격증명을 비운다.
- **테스트는 실 개발 DB 에 행을 남기지 않는다.** 실 DB 테스트(`QF_DB_TESTS=1`)는 autouse 픽스처로 정리한다.
- 테스트 실행은 컨테이너 안에서: `docker compose exec -T web pytest`. 실 DB 테스트는 `-e QF_DB_TESTS=1`.
- **DB 이름은 `quant`** (`quantfolio` 아님): `docker compose exec -T db psql -U quant -d quant`
- `web`/`worker`/`engine` 은 핫리로드가 없다. 코드 변경 후 `docker compose restart <svc>`.
- **안전 근거는 측정하고 적는다.** "X 가 막아준다"를 검증 없이 주석에 쓰지 않는다(`docs/CONVENTIONS.md` §0).
- **회귀 테스트에는 이빨이 있어야 한다.** 고친 코드를 되돌렸을 때 실제로 실패하는지 확인하고 완료를 보고한다.
- 현재 기준선: **831 passed, 16 skipped** / 실 DB 포함 **847 passed** / 마이그레이션 head `0015`.

---

### Task 1: 커버리지 테이블 모델과 마이그레이션

**Files:**
- Modify: `backend/app/models/store.py` (`IndexOhlcv` 클래스 바로 뒤에 추가)
- Create: `backend/alembic/versions/0016_index_ohlcv_coverage.py`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces: `app.models.store.IndexOhlcvCoverage` — 컬럼 `index_code: str`, `covered_from: date`, `covered_to: date`, `updated_at: datetime`. 복합 PK `(index_code, covered_from)`. Task 2 가 이 모델로 읽고 쓴다.

- [ ] **Step 1: 모델 추가**

`backend/app/models/store.py` 의 `IndexOhlcv` 클래스 정의가 끝난 직후(다음 `class IndexConstituent` 앞)에 삽입한다:

```python
class IndexOhlcvCoverage(Base):
    """지수 OHLCV 로 확보한 구간 — 범위 조회의 커버리지 판정 근거.

    커버 구간은 "받아온 행의 범위"가 아니라 **요청한 범위**다. `[A, B]` 를 달라고 해서
    소스가 정상 응답했다면 그 창 안의 데이터는 전부 받은 것이므로, 저장된 행의
    최소/최대 날짜를 뒤져 갭을 판정할 필요가 없다 — 거래일 달력 의존을 없애는 것이
    이 설계의 핵심이다(설계 §3.1).

    저장된 구간은 정의상 전부 확정분이다. 기록 시 `covered_to` 를 마지막 확정일로
    잘라 넣기 때문이다(설계 §3.2) — 그래서 `final` 컬럼이 따로 없다.

    한 지수에 행이 여럿일 수 있다. 겹치거나 맞닿은 구간만 병합하고 주말만큼 벌어진
    구간은 병합하지 않기 때문이다(설계 §3.4).
    """

    __tablename__ = "index_ohlcv_coverage"

    index_code: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)
    covered_from: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    covered_to: Mapped[date] = mapped_column(Date, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

`store.py` 상단(13~26행)이 이미 `Date`·`DateTime`·`String`·`func`·`Mapped`·`mapped_column` 을 전부 import 하고 있다(확인함). **추가 import 는 필요 없다.**

- [ ] **Step 2: 마이그레이션 작성**

`backend/alembic/versions/0016_index_ohlcv_coverage.py` 를 만든다:

```python
"""지수 OHLCV 확보 구간 테이블(index_ohlcv_coverage)을 추가한다.

범위 키 소스는 요청 범위가 정확히 일치할 때만 로컬 히트했다(§49 의 남은 한계).
확보 구간을 따로 기록해 "요청이 그 안에 들어오면 히트"로 바꾼다.

index_ohlcv 는 이제 페치 원장(external_fetches)을 쓰지 않는다 — 범위형에서 원장이
하던 일("미적재 vs 데이터 없음")을 커버리지 테이블이 더 정확히 하므로, 둘을
병행하면 진실이 두 곳이 된다. 기존 원장 행을 함께 지운다.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "index_ohlcv_coverage",
        sa.Column("index_code", sa.String(length=20), nullable=False),
        sa.Column("covered_from", sa.Date(), nullable=False),
        sa.Column("covered_to", sa.Date(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("index_code", "covered_from"),
    )
    op.execute("DELETE FROM external_fetches WHERE source = 'index_ohlcv'")


def downgrade() -> None:
    op.drop_table("index_ohlcv_coverage")
```

- [ ] **Step 3: 마이그레이션 적용과 왕복 확인**

```bash
docker compose exec -T web alembic upgrade head
docker compose exec -T db psql -U quant -d quant -c "\d index_ohlcv_coverage"
docker compose exec -T web alembic downgrade -1
docker compose exec -T web alembic upgrade head
docker compose exec -T web alembic current
```

Expected: `\d` 가 4컬럼과 PK `(index_code, covered_from)` 를 보여준다. 마지막 `current` 가 `0016 (head)`.

- [ ] **Step 4: 전체 스위트가 깨지지 않았는지 확인**

```bash
docker compose exec -T web pytest -q
```

Expected: `831 passed, 16 skipped` (모델 추가만으로는 테스트 수가 변하지 않는다)

- [ ] **Step 5: 커밋**

```bash
git add backend/app/models/store.py backend/alembic/versions/0016_index_ohlcv_coverage.py
git commit -m "feat: 지수 OHLCV 확보 구간 테이블을 추가한다" \
  -m "범위 키 소스는 요청 범위가 정확히 일치할 때만 로컬 히트했다. 확보 구간을 따로
기록해 '요청이 그 안에 들어오면 히트'로 바꾸기 위한 첫 단계다.

커버 구간은 받아온 행의 범위가 아니라 요청한 범위를 기록한다 — 그래야 저장된 행을
뒤져 갭을 판정할 필요가 없고, 거래일 달력 의존이 사라진다. covered_to 를 마지막
확정일로 잘라 넣으므로 저장된 구간은 정의상 전부 확정분이라 final 컬럼이 없다.

index_ohlcv 는 이제 external_fetches 를 쓰지 않는다(진실이 두 곳이 되지 않게).
마이그레이션이 기존 원장 행을 함께 지운다." \
  -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: 커버리지 리포지토리 (read/merge + 삭제 연동)

**Files:**
- Modify: `backend/app/services/data/store/indexes.py`
- Test: `backend/tests/test_local_store_repo.py` (파일 끝에 이어 씀)

**Interfaces:**
- Consumes: `app.models.store.IndexOhlcvCoverage` (Task 1)
- Produces:
  - `read_coverage(index_code: str) -> list[tuple[date, date]]` — `(covered_from, covered_to)` 를 `covered_from` 오름차순으로 반환
  - `merge_coverage(index_code: str, start: date, end: date) -> None` — `end < start` 면 no-op
  - `delete_index_ohlcv(index_code: str) -> None` — 기존 시그니처 그대로, 커버리지까지 삭제하도록 동작만 확장
  - Task 4 가 이 셋을 쓴다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`backend/tests/test_local_store_repo.py` 끝에 이어 붙인다:

```python
# ───── 지수 OHLCV 확보 구간(커버리지) ─────

_COV_CODE = "9999"  # 실제 지수코드와 겹치지 않는 시험값


@pytest.fixture(autouse=True)
def _cleanup_coverage():
    yield
    indexes.delete_index_ohlcv(_COV_CODE)


def test_확보구간을_쓰고_읽는다():
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30))

    assert indexes.read_coverage(_COV_CODE) == [(date(2020, 1, 1), date(2020, 6, 30))]


def test_겹치는_구간은_하나로_병합된다():
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30))
    indexes.merge_coverage(_COV_CODE, date(2020, 4, 1), date(2020, 9, 30))

    assert indexes.read_coverage(_COV_CODE) == [(date(2020, 1, 1), date(2020, 9, 30))]


def test_새_구간이_기존보다_앞서도_병합된다():
    """병합 조건을 한쪽만 보면(새.from <= 기존.to+1) 이 케이스가 누락된다."""
    indexes.merge_coverage(_COV_CODE, date(2020, 6, 1), date(2020, 9, 30))
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 7, 31))

    assert indexes.read_coverage(_COV_CODE) == [(date(2020, 1, 1), date(2020, 9, 30))]


def test_하루_맞닿은_구간은_병합된다():
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30))
    indexes.merge_coverage(_COV_CODE, date(2020, 7, 1), date(2020, 9, 30))

    assert indexes.read_coverage(_COV_CODE) == [(date(2020, 1, 1), date(2020, 9, 30))]


def test_주말만큼_벌어진_구간은_병합하지_않는다():
    """그 사이에 거래일이 있었는지 달력 없이 단정할 수 없다 — 보수적으로 둘로 남긴다."""
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 26))  # 금
    indexes.merge_coverage(_COV_CODE, date(2020, 6, 29), date(2020, 9, 30))  # 월

    assert indexes.read_coverage(_COV_CODE) == [
        (date(2020, 1, 1), date(2020, 6, 26)),
        (date(2020, 6, 29), date(2020, 9, 30)),
    ]


def test_역전된_구간은_기록하지_않는다():
    indexes.merge_coverage(_COV_CODE, date(2020, 6, 30), date(2020, 1, 1))

    assert indexes.read_coverage(_COV_CODE) == []


def test_일봉_삭제는_커버리지도_함께_지운다():
    """행만 지우고 커버리지가 남으면 '커버됐다는데 행이 없는' 영구 빈 결과가 된다."""
    df = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
         "volume": [10], "trading_value": [100]},
        index=pd.to_datetime(["2020-01-02"]),
    )
    indexes.write_index_ohlcv(_COV_CODE, df, None)
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30))

    indexes.delete_index_ohlcv(_COV_CODE)

    assert indexes.read_coverage(_COV_CODE) == []
    assert indexes.read_index_ohlcv(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30)).empty
```

`from app.services.data.store import indexes` 는 176행에 이미 있고(지수 테스트 절이 쓴다), `date`·`timedelta`·`pandas as pd`·`pytest` 도 이미 import 돼 있다(확인함). **추가 import 는 필요 없다.**

- [ ] **Step 2: 실패를 확인한다**

```bash
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -q -k 구간 or 커버리지
```

Expected: FAIL — `AttributeError: module 'app.services.data.store.indexes' has no attribute 'merge_coverage'`

- [ ] **Step 3: 리포지토리를 구현한다**

`backend/app/services/data/store/indexes.py` 를 고친다.

상단 import 를 보강한다:

```python
from datetime import date, timedelta
```

`from app.models.store import IndexConstituent, IndexOhlcv` 를 다음으로 바꾼다:

```python
from app.models.store import IndexConstituent, IndexOhlcv, IndexOhlcvCoverage
```

`delete_index_ohlcv` 의 비동기 본체를 확장한다:

```python
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
```

파일 끝(지수구성 함수들 뒤)에 커버리지 함수를 추가한다:

```python
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
```

- [ ] **Step 4: 테스트 통과를 확인한다**

```bash
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -q
docker compose exec -T web pytest -q
```

Expected: 실 DB 스위트 전부 통과(기존 16 + 신규 7 = 23 passed). 전체 스위트는 `831 passed, 23 skipped`(신규 7건이 `QF_DB_TESTS` 미설정 시 skip).

- [ ] **Step 5: 이빨 검증 — 양방향 병합 조건을 한쪽으로 되돌린다**

`_merge_coverage` 의 `IndexOhlcvCoverage.covered_from <= end + one` 줄을 잠시 지우고 실행한다:

```bash
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -q -k 앞서도
```

Expected: FAIL. 확인 후 **Edit 로 원복**하고 `git diff` 로 무출력을 확인한다.

- [ ] **Step 6: 잔여물 확인과 커밋**

```bash
docker compose exec -T db psql -U quant -d quant -c "select count(*) from index_ohlcv_coverage;"
```

Expected: `0` (autouse 픽스처가 정리한다)

```bash
git add backend/app/services/data/store/indexes.py backend/tests/test_local_store_repo.py
git commit -m "feat: 지수 OHLCV 확보 구간 읽기·병합을 추가한다" \
  -m "겹치거나 하루 맞닿은 구간을 흡수해 한 행으로 대체한다. 병합 조건은 양방향이다 —
한쪽만 보면 새 구간이 기존보다 앞설 때 병합이 누락된다(회귀 테스트로 고정).
주말만큼 벌어진 구간은 병합하지 않는다: 그 사이 거래일 유무를 달력 없이 단정할 수
없어서다.

delete_index_ohlcv 가 커버리지도 함께 지운다. 행만 지우고 커버 구간이 남으면
'커버됐다는데 행이 없는' 영구 빈 결과가 된다 — 마이그레이션 0015 가 고친 것과 같은
형태의 함정이라 같은 함수 안에서 묶었다." \
  -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `last_final_date()` 와 `cached_range()`

**Files:**
- Modify: `backend/app/services/data/store/frame.py`
- Test: `backend/tests/test_local_store_frame.py` (파일 끝에 이어 씀)

**Interfaces:**
- Consumes: 없음(이 태스크는 소스 비의존 — 커버리지 접근을 콜러블로 주입받는다)
- Produces:
  - `last_final_date(*, today: date | None = None) -> date` — 확정으로 봐도 되는 마지막 날짜(KST 전날)
  - `cached_range(key: str, start: date, end: date, *, read_local: Callable[[], pd.DataFrame], fetch_remote: Callable[[], pd.DataFrame], write_local: Callable[[pd.DataFrame], None], read_coverage: Callable[[], list[tuple[date, date]]], merge_coverage: Callable[[date, date], None]) -> pd.DataFrame`
  - Task 4 가 `cached_range` 를 쓴다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`backend/tests/test_local_store_frame.py` 끝에 이어 붙인다:

```python
# ───── cached_range: 범위 조회의 구간 커버리지 계약 ─────

from app.services.data.errors import SourceUnavailableError  # noqa: E402
from app.services.data.store.frame import cached_range, last_final_date  # noqa: E402


class _RangeStore:
    """cached_range 주입 대역 — 호출 횟수와 커버 구간만 본다."""

    def __init__(self, intervals=None, rows=1):
        self.intervals: list[tuple[date, date]] = list(intervals or [])
        self.remote_calls = 0
        self.written: list[int] = []
        self._rows = rows
        self.fail: Exception | None = None

    def read_local(self):
        return pd.DataFrame({"close": [1.0] * self._rows})

    def fetch_remote(self):
        self.remote_calls += 1
        if self.fail is not None:
            raise self.fail
        return pd.DataFrame({"close": [1.0] * self._rows})

    def write_local(self, df):
        self.written.append(len(df))

    def read_coverage(self):
        return list(self.intervals)

    def merge_coverage(self, start, end):
        if end < start:
            return
        self.intervals.append((start, end))

    def call(self, start, end):
        return cached_range(
            "1001", start, end,
            read_local=self.read_local,
            fetch_remote=self.fetch_remote,
            write_local=self.write_local,
            read_coverage=self.read_coverage,
            merge_coverage=self.merge_coverage,
        )


_PAST_A = date(2020, 1, 1)
_PAST_B = date(2020, 6, 30)


def test_last_final_date_는_KST_전날이다():
    assert last_final_date(today=date(2026, 8, 8)) == date(2026, 8, 7)
    assert is_final_date(date(2026, 8, 7), today=date(2026, 8, 8)) is True
    assert is_final_date(date(2026, 8, 8), today=date(2026, 8, 8)) is False


def test_커버된_구간은_외부를_타지_않는다():
    store = _RangeStore(intervals=[(date(2019, 1, 1), date(2021, 1, 1))])

    store.call(_PAST_A, _PAST_B)

    assert store.remote_calls == 0


def test_부분_커버는_원격을_타고_병합된_뒤_히트한다():
    """워크포워드처럼 창이 밀리며 쌓이는 경우가 이 형태다."""
    store = _RangeStore(intervals=[(_PAST_A, date(2020, 3, 31))])

    store.call(_PAST_A, _PAST_B)
    assert store.remote_calls == 1

    store.call(_PAST_A, _PAST_B)
    assert store.remote_calls == 1  # 병합된 구간이 요청을 덮는다


def test_끝이_오늘이면_커버리지가_있어도_원격을_탄다():
    """당일 봉은 장중 계속 변한다 — 로컬로 줄 수 없다."""
    today = last_final_date() + timedelta(days=1)
    store = _RangeStore(intervals=[(date(2000, 1, 1), date(2100, 1, 1))])

    store.call(_PAST_A, today)

    assert store.remote_calls == 1


def test_빈_결과는_커버리지로_기록하지_않는다():
    """§49 I3 와 같은 판단 — 빈 응답은 '없다는 명시적 선언'이 아니다."""
    store = _RangeStore(rows=0)

    store.call(_PAST_A, _PAST_B)
    store.call(_PAST_A, _PAST_B)

    assert store.intervals == []
    assert store.remote_calls == 2


def test_원격_실패는_클래스를_보존하고_보유_구간을_알려준다():
    store = _RangeStore(intervals=[(_PAST_A, date(2020, 3, 31))])
    store.fail = SourceUnavailableError("krx", "타임아웃")

    with pytest.raises(SourceUnavailableError) as caught:
        store.call(_PAST_A, _PAST_B)

    assert "2020-01-01~2020-03-31" in str(caught.value)
    assert "타임아웃" in str(caught.value)


def test_보유_구간이_없으면_없음이라고_알려준다():
    store = _RangeStore()
    store.fail = SourceUnavailableError("krx", "타임아웃")

    with pytest.raises(SourceUnavailableError) as caught:
        store.call(_PAST_A, _PAST_B)

    assert "없음" in str(caught.value)
```

현재 이 파일(39~45행)은 `from datetime import date` / `import pandas as pd` / `import pytest` / `from app.services.data.store.frame import cached_frame, is_final_date, make_cache_key` 를 갖고 있다(확인함). **`timedelta` 만 더한다** — 39행을 `from datetime import date, timedelta` 로 바꾼다. `cached_range`·`last_final_date` 는 위 테스트 블록이 자체 import 하므로 45행은 그대로 둔다.

- [ ] **Step 2: 실패를 확인한다**

```bash
docker compose exec -T web pytest tests/test_local_store_frame.py -q
```

Expected: FAIL — `ImportError: cannot import name 'cached_range'`

- [ ] **Step 3: `frame.py` 를 구현한다**

상단 import 를 보강한다:

```python
from datetime import date, timedelta
```

그리고 예외 타입을 쓰기 위해:

```python
from app.services.data.errors import DataSourceError
```

`is_final_date` 를 `last_final_date` 위에 얹는다(계산 출처를 하나로 묶는다). 기존 docstring 은 그대로 두고 마지막 줄만 바꾼다:

```python
    ref = today or _now_kst_date()
    return last_day <= last_final_date(today=ref)
```

`_now_kst_date` 정의 바로 뒤에 추가한다:

```python
def last_final_date(*, today: date | None = None) -> date:
    """확정으로 봐도 되는 마지막 날짜 — KST 기준 전날.

    `is_final_date` 와 같은 기준을 두 곳에서 따로 계산하면 언젠가 어긋난다. 범위형
    조회(`cached_range`)는 "어디까지 확정인가"를 **값으로** 필요로 하므로(커버 구간을
    거기서 자른다) 여기 한 곳에 두고 `is_final_date` 도 이것을 쓴다.
    """
    ref = today or _now_kst_date()
    return ref - timedelta(days=1)
```

파일 끝에 `cached_range` 와 두 헬퍼를 추가한다:

```python
def _covers(intervals: list[tuple[date, date]], start: date, end: date) -> bool:
    """확보 구간 중 [start, end] 를 통째로 포함하는 것이 있는가.

    구간들의 합집합이 아니라 **개별 구간**을 본다. 합집합으로 판정하려면 사이의
    빈틈이 휴장일인지 미적재인지 알아야 하는데, 그 판단에는 거래일 달력이 필요하다.
    """
    return any(f <= start and t >= end for f, t in intervals)


def _with_coverage_hint(
    exc: DataSourceError, intervals: list[tuple[date, date]]
) -> DataSourceError:
    """실패 예외에 로컬 확보 구간을 덧붙여 **같은 클래스로** 다시 만든다.

    사용자가 기간을 좁혀 재시도할 근거를 응답 본문(`app/main.py` 가 502/503 으로
    변환한다)에서 바로 보게 하려는 것이다. 기간을 대신 축소하지는 않는다 — 구간이
    바뀐 성과를 같은 것으로 착각하는 함정은 이 저장소가 이미 데인 적이 있다.

    `errors.py` 의 예외 계층이 전부 `(source, message, retry_after=)` 시그니처를
    공유하는 것에 기댄다. 쿨다운·실패 집계는 `fetch_remote()` 안에서 이미 기록됐으므로
    인스턴스를 바꿔도 영향이 없다.
    """
    if intervals:
        newest = sorted(intervals, key=lambda iv: iv[1], reverse=True)
        shown = ", ".join(f"{f:%Y-%m-%d}~{t:%Y-%m-%d}" for f, t in newest[:3])
        if len(newest) > 3:
            shown += f" …외 {len(newest) - 3}건"
    else:
        shown = "없음"
    return type(exc)(
        exc.source,
        f"{exc.detail} — 로컬 확보 구간: {shown}",
        retry_after=exc.retry_after,
    )


def cached_range(
    key: str,
    start: date,
    end: date,
    *,
    read_local: Callable[[], pd.DataFrame],
    fetch_remote: Callable[[], pd.DataFrame],
    write_local: Callable[[pd.DataFrame], None],
    read_coverage: Callable[[], list[tuple[date, date]]],
    merge_coverage: Callable[[date, date], None],
) -> pd.DataFrame:
    """범위 조회의 로컬 우선 진입점 — 확보 구간이 요청을 덮으면 외부를 타지 않는다.

    `cached_frame` 의 형제다. 차이는 게이트가 캐시키 정확일치가 아니라 **구간 포함**
    이라는 점 하나다. 범위 키 소스(지수 OHLCV)는 요청 범위가 조금만 달라도 원장
    키가 어긋나 이미 가진 행을 못 쓰는 문제가 있었다.

    커버 구간으로 기록하는 것은 **요청 범위**이지 받아온 행의 범위가 아니다 —
    `[start, end]` 에 대해 소스가 정상 응답했으면 그 창 안은 전부 받은 것이므로,
    거래일 달력 없이도 갭 없음을 말할 수 있다.

    :param key: 로그·디버깅용 식별자(지수코드). 게이트 판정에는 쓰지 않는다 —
        커버 구간 조회 자체가 `read_coverage` 콜러블에 이미 묶여 있다.
    :raises DataSourceError: 외부 조회가 실패했을 때. `detail` 에 로컬 확보 구간을
        덧붙여 올린다. **빈 프레임으로 삼키지 않는다.**
    """
    final_through = last_final_date()

    if end <= final_through and _covers(read_coverage(), start, end):
        logger.debug("로컬 커버 히트: %s %s~%s", key, start, end)
        return read_local()

    try:
        df = fetch_remote()
    except DataSourceError as e:
        raise _with_coverage_hint(e, read_coverage()) from e

    if df is None:
        df = pd.DataFrame()
    write_local(df)

    if not df.empty:
        # 빈 결과는 커버리지로 기록하지 않는다 — "없다는 명시적 선언"이 아니라
        # 스키마 변동으로 값을 잃은 것일 수도 있다(cached_frame 의 0행 가드와 같은 판단).
        merge_coverage(start, min(end, final_through))

    logger.debug("원격 조회: %s %s~%s rows=%d", key, start, end, len(df))
    return df
```

- [ ] **Step 4: 테스트 통과를 확인한다**

```bash
docker compose exec -T web pytest tests/test_local_store_frame.py -q
docker compose exec -T web pytest -q
```

Expected: 신규 7건 통과. 전체 `838 passed, 23 skipped`.

- [ ] **Step 5: 이빨 검증 — 커버 게이트를 무력화한다**

`cached_range` 의 `if end <= final_through and _covers(...)` 를 `if False:` 로 잠시 바꾸고 실행한다:

```bash
docker compose exec -T web pytest tests/test_local_store_frame.py -q -k 커버된_구간 or 부분_커버
```

Expected: 두 건 FAIL. 확인 후 **Edit 로 원복**하고 `git diff` 무출력을 확인한다.

- [ ] **Step 6: 커밋**

```bash
git add backend/app/services/data/store/frame.py backend/tests/test_local_store_frame.py
git commit -m "feat: 범위 조회 진입점 cached_range 를 추가한다" \
  -m "cached_frame 의 형제다. 게이트가 캐시키 정확일치가 아니라 구간 포함이라는 점만
다르다. 커버 구간으로 기록하는 것은 요청 범위이지 받아온 행의 범위가 아니다 —
소스가 정상 응답한 창 안은 전부 받은 것이므로 거래일 달력 없이 갭 없음을 말할 수 있다.

빈 결과는 커버리지로 기록하지 않는다(cached_frame 의 0행 가드와 같은 판단).
끝이 오늘이면 커버리지가 있어도 원격을 탄다 — 당일 봉은 장중 계속 변한다.
원격 실패는 같은 예외 클래스로 다시 만들어 detail 에 로컬 확보 구간을 싣는다.
기간을 대신 축소하지는 않는다.

last_final_date 를 두어 is_final_date 와 계산 출처를 하나로 묶었다." \
  -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `_fetch_index_ohlcv` 를 `cached_range` 로 전환

**Files:**
- Modify: `backend/app/services/metrics/fetch.py`
- Test: `backend/tests/test_fetch_store_wiring.py` (파일 끝에 이어 씀)

**Interfaces:**
- Consumes: `cached_range` (Task 3), `indexes.read_coverage`/`merge_coverage` (Task 2)
- Produces: `_fetch_index_ohlcv(start_ymd: str, end_ymd: str, ticker: str) -> pd.DataFrame | None` — **시그니처와 반환 불변**. 호출자(`panic.py`·`sectors.py`·`rebalance_runner.py`·`backtests.py`·`scripts/`)는 손대지 않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`backend/tests/test_fetch_store_wiring.py` 끝에 이어 붙인다:

```python
class _IndexOhlcvFakeStock:
    """get_index_ohlcv 대역 — 호출 횟수를 센다."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def get_index_ohlcv(self, start_ymd, end_ymd, ticker):
        self.calls.append((start_ymd, end_ymd, ticker))
        idx = pd.to_datetime([start_ymd, end_ymd])
        return pd.DataFrame(
            {"시가": [1.0, 1.0], "고가": [2.0, 2.0], "저가": [0.5, 0.5],
             "종가": [1.5, 1.5], "거래량": [10, 10], "거래대금": [100, 100]},
            index=idx,
        )


def test_확보구간에_포함되면_원격을_안_탄다(_store, monkeypatch, _coverage):
    """워크포워드 시나리오 — [A,D] 를 확보해 두면 그 안의 [B,C] 는 이미 가진 데이터다.

    구간 커버리지 이전에는 캐시키가 정확히 일치해야 해서 이 요청이 매번 원격을 탔다.
    이 테스트의 책임은 **fetch.py 가 커버리지 조회를 제대로 배선했는가**다. 병합 규칙
    자체는 Task 2 의 실 DB 테스트가 본다 — 여기서 병합을 재구현하면 두 곳이 갈린다.
    """
    fake = _IndexOhlcvFakeStock()
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)
    _coverage["1001"] = [(date(2018, 1, 1), date(2023, 4, 1))]  # [A, D]

    F._fetch_index_ohlcv("20180401", "20230101", "1001")  # [B, C] ⊂ [A, D]

    assert fake.calls == []


def test_확보구간_밖이면_원격을_타고_구간이_기록된다(_store, monkeypatch, _coverage):
    fake = _IndexOhlcvFakeStock()
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    F._fetch_index_ohlcv("20180401", "20230101", "1001")

    assert len(fake.calls) == 1
    assert _coverage["1001"] == [(date(2018, 4, 1), date(2023, 1, 1))]
```

이 두 테스트가 쓰는 `_coverage` 픽스처를 같은 파일에 추가한다. **병합 규칙을 흉내 내지
않는다** — 기록만 하고, 히트 케이스는 테스트가 구간을 직접 심는다:

```python
@pytest.fixture
def _coverage(monkeypatch) -> dict[str, list[tuple[date, date]]]:
    """지수 커버리지 인메모리 대역 — 기록과 조회만 한다.

    실제 병합 규칙(겹침·±1일 인접)은 여기서 재구현하지 않는다. 재구현하면 프로덕션과
    갈릴 수 있고, 그 규칙은 Task 2 의 실 DB 테스트가 이미 지킨다. 이 파일의 관심사는
    fetch.py 가 커버리지 조회·기록을 제대로 배선했는가뿐이다.
    """
    store: dict[str, list[tuple[date, date]]] = {}

    monkeypatch.setattr(F, "_store_read_coverage", lambda code: list(store.get(code, [])))
    monkeypatch.setattr(
        F,
        "_store_merge_coverage",
        lambda code, start, end: store.setdefault(code, []).append((start, end)),
    )
    return store
```

`test_fetch_store_wiring.py` 6행은 현재 `from datetime import date` 다(확인함). **`timedelta` 를 더해** `from datetime import date, timedelta` 로 바꾼다.

- [ ] **Step 2: 실패를 확인한다**

```bash
docker compose exec -T web pytest tests/test_fetch_store_wiring.py -q -k 확보구간
```

Expected: FAIL — `AttributeError: ... has no attribute '_store_read_coverage'`

- [ ] **Step 3: `fetch.py` 를 전환한다**

기존 `_store_read_index_ohlcv` 래퍼(74~80행 부근) 옆에 두 개를 추가한다:

```python
def _store_read_coverage(code):
    from app.services.data.store import indexes

    return indexes.read_coverage(code)


def _store_merge_coverage(code, start, end):
    from app.services.data.store import indexes

    indexes.merge_coverage(code, start, end)
```

`_fetch_index_ohlcv` 의 `cached_frame(...)` 호출을 `cached_range(...)` 로 바꾼다:

```python
    out = cached_range(
        ticker,
        start_d,
        end_d,
        read_local=lambda: _store_read_index_ohlcv(ticker, start_d, end_d),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_index_ohlcv(ticker, df, None),
        read_coverage=lambda: _store_read_coverage(ticker),
        merge_coverage=lambda f, t: _store_merge_coverage(ticker, f, t),
    )
    return out if not out.empty else None
```

`fetch.py` 상단의 `from app.services.data.store.frame import cached_frame, is_final_date, make_cache_key` 계열 import 에 `cached_range` 를 더한다. `_fetch_index_ohlcv` 안에서 더 이상 쓰이지 않는 `make_cache_key`/`is_final_date` 는 다른 fetcher 들이 계속 쓰므로 **지우지 않는다**.

- [ ] **Step 4: 테스트 통과를 확인한다**

```bash
docker compose exec -T web pytest -q
```

Expected: `840 passed, 23 skipped` (Task 4 가 테스트 2건을 더한다)

- [ ] **Step 5: 이빨 검증 — 커버리지 주입을 끊는다**

`_fetch_index_ohlcv` 의 `read_coverage=` 를 `lambda: []` 로 잠시 바꾸고 실행한다:

```bash
docker compose exec -T web pytest tests/test_fetch_store_wiring.py -q -k 확보구간
```

Expected: FAIL(원격 1회 추가). 확인 후 **Edit 로 원복**하고 `git diff` 무출력을 확인한다.

- [ ] **Step 6: 실 DB 로 종단 확인**

```bash
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/ -q
docker compose exec -T db psql -U quant -d quant -c "select count(*) from index_ohlcv_coverage;" -c "select count(*) from external_fetches where source='index_ohlcv';"
```

Expected: 실 DB 스위트 전부 통과. 두 카운트 모두 `0`(테스트가 실 DB 를 오염시키지 않는다).

- [ ] **Step 7: 커밋**

```bash
git add backend/app/services/metrics/fetch.py backend/tests/test_fetch_store_wiring.py
git commit -m "feat: 지수 OHLCV 조회를 구간 커버리지 방식으로 바꾼다" \
  -m "_fetch_index_ohlcv 가 cached_frame 대신 cached_range 를 쓴다. 시그니처와 반환은
불변이라 호출자(panic·sectors·rebalance_runner·backtests·scripts)는 하나도 바뀌지 않는다.

워크포워드처럼 창이 밀리며 쌓이는 경우가 이 변경의 표적이다 — [A,C] 와 [B,D] 를
받아 두면 그 안의 [B,C] 는 이미 가진 데이터인데, 캐시키 정확일치 시절에는 매번
원격을 탔다. 회귀 테스트가 그 시나리오를 고정한다." \
  -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: 야간 배치에 넓은 구간 지수 선적재를 되살린다

**Files:**
- Modify: `backend/worker/tasks.py`
- Test: `backend/tests/test_worker_snapshots.py`

**Interfaces:**
- Consumes: `_fetch_index_ohlcv` 의 커버리지 동작 (Task 4)
- Produces: `_snapshot_steps(ymd: str) -> list[tuple[str, Callable[[], object]]]` — 단계 수가 4 → **7**

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`backend/tests/test_worker_snapshots.py` 의 `test_snapshot_steps_는_올바른_함수를_올바른_인자로_배선한다` 를 다음으로 교체한다(docstring 포함):

```python
def test_snapshot_steps_는_올바른_함수를_올바른_인자로_배선한다(monkeypatch):
    """_snapshot_steps 본문이 실제로 무엇을 어떤 인자로 부르는지 검증한다.

    기존 두 테스트는 _snapshot_steps 자체를 통째로 대역해 이 함수 본문이
    `return []` 여도 통과한다 — 이 테스트가 그 공백을 메운다. 특히 late binding
    (루프 변수 캡처) 이 깨져 KOSPI/KOSDAQ 이나 지수 3종이 같은 값으로 뭉개지는
    회귀를 잡는다.

    지수 OHLCV 는 **넓은 구간**으로 선적재해야 한다. 하루치(`(ymd, ymd)`)로 넣으면
    넓은 범위를 쓰는 소비자(패닉 90·섹터 252·레짐 210 거래일)와 커버 구간이 겹치지
    않아 히트가 0이 된다 — 그래서 한 번 뺐던 단계다(§49 I4). 구간 커버리지가 그
    문제를 해소했으므로 되살리되, 하루치로 퇴행하면 여기서 잡힌다.
    """
    import app.services.metrics.fetch as fetch_mod

    calls: dict[str, list[tuple]] = {
        "fundamentals": [], "market_cap": [], "market_ohlcv": [], "index_ohlcv": [],
    }

    def fake_fundamentals(as_of_ymd, mkts):
        calls["fundamentals"].append((as_of_ymd, tuple(mkts)))

    def fake_market_cap(as_of_ymd, mkts):
        calls["market_cap"].append((as_of_ymd, tuple(mkts)))

    def fake_market_ohlcv(date_ymd, mkt):
        calls["market_ohlcv"].append((date_ymd, mkt))

    def fake_index_ohlcv(start_ymd, end_ymd, ticker):
        calls["index_ohlcv"].append((start_ymd, end_ymd, ticker))

    monkeypatch.setattr(fetch_mod, "_fetch_fundamentals", fake_fundamentals)
    monkeypatch.setattr(fetch_mod, "_fetch_market_cap", fake_market_cap)
    monkeypatch.setattr(fetch_mod, "_fetch_market_ohlcv_snapshot", fake_market_ohlcv)
    monkeypatch.setattr(fetch_mod, "_fetch_index_ohlcv", fake_index_ohlcv)

    steps = tasks._snapshot_steps("20260805")
    assert len(steps) == 7
    for _name, call in steps:
        call()

    assert calls["fundamentals"] == [("20260805", ("KOSPI", "KOSDAQ"))]
    assert calls["market_cap"] == [("20260805", ("KOSPI", "KOSDAQ"))]
    assert calls["market_ohlcv"] == [("20260805", "KOSPI"), ("20260805", "KOSDAQ")]

    assert [t for _s, _e, t in calls["index_ohlcv"]] == ["1001", "2001", "1028"]
    # 끝은 대상일, 시작은 400 거래일 근사만큼 과거 — 하루치가 아니다.
    # 경계 상수(_SNAPSHOT_INDEX_BDAYS·buffer)가 조금 바뀌어도 안 깨지도록, 고정 날짜
    # 대신 "최소 1년 이상 과거"라는 성질로 단언한다(하루치 퇴행은 확실히 잡힌다).
    from datetime import datetime as _dt

    for start_ymd, end_ymd, _t in calls["index_ohlcv"]:
        assert end_ymd == "20260805"
        span = (_dt.strptime(end_ymd, "%Y%m%d") - _dt.strptime(start_ymd, "%Y%m%d")).days
        assert span > 365
```

- [ ] **Step 2: 실패를 확인한다**

```bash
docker compose exec -T web pytest tests/test_worker_snapshots.py -q -k 배선
```

Expected: FAIL — `assert 4 == 7`

- [ ] **Step 3: `_snapshot_steps` 를 확장한다**

`backend/worker/tasks.py` 의 `_INGEST_FAILURE_ALERT_RATIO` 상수 근처에 추가한다:

```python
#: 야간 선적재 대상 기준지수 — 레짐·패닉(1001·2001)과 벤치마크·잔차모멘텀(1028).
#: 업종지수는 수십 개라 비용이 크고 섹터 로테이션 전용이므로 대상이 아니다.
_SNAPSHOT_INDEX_TICKERS = ("1001", "2001", "1028")
#: 선적재 구간(거래일). 소비자 중 가장 긴 창(섹터 252·레짐 ma_period+10)을 덮는다.
_SNAPSHOT_INDEX_BDAYS = 400
```

`_snapshot_steps` 의 docstring 에서 "지수 OHLCV 는 일부러 넣지 않는다" 문단을 다음으로 교체한다:

```
    지수 OHLCV 는 **넓은 구간**으로 넣는다. `cached_range` 의 커버 판정이 구간 포함
    이므로, 400 거래일을 미리 확보해 두면 그보다 짧은 창을 쓰는 소비자(패닉 90·섹터
    252·레짐 ma_period+10)가 전부 로컬로 굴러간다. 하루치(`(ymd, ymd)`)로 넣던
    시절에는 소비자 범위와 절대 겹치지 않아 히트가 0이라 이 단계를 뺐었다(§49 I4) —
    구간 커버리지가 그 문제를 해소했다.
```

`_snapshot_steps` 본문의 import 와 마지막에 단계를 더한다:

```python
    from app.services.metrics.fetch import (
        _fetch_fundamentals,
        _fetch_index_ohlcv,
        _fetch_market_cap,
        _fetch_market_ohlcv_snapshot,
    )
    from app.services.metrics.common import _approx_start, _ymd
```

그리고 전종목 OHLCV 루프 뒤에:

```python
    day = datetime.strptime(ymd, "%Y%m%d").date()
    hist_start_ymd = _ymd(_approx_start(day, _SNAPSHOT_INDEX_BDAYS, buffer=30))
    for code in _SNAPSHOT_INDEX_TICKERS:
        steps.append(
            (
                f"지수OHLCV({code})",
                lambda c=code: _fetch_index_ohlcv(hist_start_ymd, ymd, c),
            )
        )
    return steps
```

`tasks.py` 상단에 `datetime` 이 이미 import 돼 있다(`from datetime import date, datetime, time, timedelta, timezone`) — 확인만 한다.

`ingest_daily_snapshots` docstring 의 두 곳을 고친다:
- "범위 키 소스(지수 OHLCV·기간 통계)는 요청 범위가 정확히 일치할 때만 로컬 히트하므로 선적재로 채울 수 없다" → 지수 OHLCV 는 이제 구간 커버리지로 채울 수 있고, **기간 통계만** 해당한다고 정정
- "단계가 4개뿐이라 … 1/4=25%" → "단계가 7개뿐이라 … 1/7≈14%"

- [ ] **Step 4: 테스트 통과를 확인한다**

```bash
docker compose exec -T web pytest tests/test_worker_snapshots.py -q
docker compose exec -T web pytest -q
```

Expected: `test_worker_snapshots.py` 전부 통과. 전체 `840 passed, 23 skipped`.

- [ ] **Step 5: 이빨 검증 — 하루치로 퇴행시킨다**

`_fetch_index_ohlcv(hist_start_ymd, ymd, c)` 를 `_fetch_index_ohlcv(ymd, ymd, c)` 로 잠시 바꾸고 실행한다:

```bash
docker compose exec -T web pytest tests/test_worker_snapshots.py -q -k 배선
```

Expected: FAIL(`assert start_ymd < "20250101"`). 확인 후 **Edit 로 원복**하고 `git diff` 무출력을 확인한다.

- [ ] **Step 6: 워커 재시작과 태스크 등록 확인**

```bash
docker compose restart worker
docker compose exec -T worker python -c "from worker.celery_app import celery_app; print('worker.ingest_daily_snapshots' in celery_app.tasks)"
```

Expected: `True`

- [ ] **Step 7: 커밋**

```bash
git add backend/worker/tasks.py backend/tests/test_worker_snapshots.py
git commit -m "feat: 야간 배치가 기준지수 400거래일 구간을 선적재한다" \
  -m "커버리지만으로는 '겹치는 구간을 우연히 받아뒀을 때'만 듣는다 — 운에 기대는
가용성이다. 400 거래일을 미리 확보하면 그보다 짧은 창을 쓰는 소비자(패닉 90·섹터
252·레짐 ma_period+10)가 pykrx 가 막혀도 전부 로컬로 굴러간다.

하루치 키라 소비자와 절대 안 겹쳐서 뺐던 단계다(§49 I4). 구간 커버리지가 그 이유를
해소했으므로 되살린다. 대상은 레짐·패닉(1001·2001)과 벤치마크·잔차모멘텀(1028)
세 개다 — 업종지수는 수십 개라 비용이 크고 섹터 로테이션 전용이라 제외한다.

단계가 4 → 7 로 늘어 1건 실패가 1/7≈14% 다. 10% 임계는 그대로 둔다(한 단계가
하루치 데이터 종류 하나 전체라 1건 실패도 무시할 잡음이 아니다)." \
  -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: 문서 갱신

**Files:**
- Modify: `docs/CONVENTIONS.md` (§1 "외부 데이터 조회 계약")
- Modify: `docs/improvements.md` (§49 의 범위 키 한계 문단)

**Interfaces:**
- Consumes: Task 1~5 의 최종 동작
- Produces: 없음(문서)

- [ ] **Step 1: `CONVENTIONS.md` 의 조회 계약을 갱신한다**

"조회 진입점은 `app/services/data/store/frame.py` 의 `cached_frame` 하나다" 로 시작하는 항목을 다음으로 교체한다:

```markdown
- **조회 진입점은 `app/services/data/store/frame.py` 에 모여 있다.** 날짜 단위 키를
  쓰는 소스는 `cached_frame`(캐시키 정확일치 게이트), 범위 키를 쓰는 소스는
  `cached_range`(구간 포함 게이트)를 쓴다. 로컬 우선 읽기·원격 폴백·확정 기록이
  이 두 함수에 모여 있다. 새 소스를 붙일 때 `try/except` 로 자체 캐싱을 만들지 말고
  둘 중 하나를 쓴다.
- **범위 키 소스의 커버 구간은 "요청한 범위"이지 "받아온 행의 범위"가 아니다.**
  `[A, B]` 에 대해 소스가 정상 응답했으면 그 창 안은 전부 받은 것이므로, 저장된 행을
  뒤져 갭을 판정할 필요가 없다 — 그 판정에는 거래일 달력이 필요한데 이 저장소엔
  신뢰할 소스가 없다. 겹치거나 하루 맞닿은 구간만 병합하고 주말만큼 벌어진 구간은
  병합하지 않는다(사이에 거래일이 있었는지 단정할 수 없다).
```

- [ ] **Step 2: `improvements.md` §49 의 한계 문단을 갱신한다**

"**범위 키 소스의 한계(I4, 2026-08-08 통합 리뷰)**" 로 시작하는 문단 끝의
"구간 커버리지 기반 조회(요청 범위가 적재 구간에 포함되면 히트)는 별도 과제로 남는다."
를 다음으로 교체한다:

```markdown
**해소(2026-08-08)**: 지수 OHLCV 에 한해 구간 커버리지 조회를 도입했다
(`index_ohlcv_coverage` 테이블 + `frame.cached_range`, 설계는
`docs/superpowers/specs/2026-08-08-index-ohlcv-coverage-design.md`). 확보 구간이
요청을 포함하면 로컬로 답하므로, 야간 배치가 400 거래일을 미리 확보해 두면 pykrx 가
막혀도 레짐·패닉·벤치마크가 굴러간다. 선적재 단계도 그래서 되살렸다.

**기간 통계(`stock_period_stats`)는 여전히 정확일치다.** 등락률·누적 순매수는 구간
자체가 값인 집계라 긴 구간에서 짧은 구간을 뽑을 수 없다 — 커버리지가 원리적으로
성립하지 않는다. 업종지수도 선적재 대상이 아니라 차단 시 섹터 로테이션은 멈춘다.
```

- [ ] **Step 3: 문서가 코드 사실과 맞는지 대조한다**

```bash
grep -n "cached_range\|cached_frame" backend/app/services/data/store/frame.py | head
grep -n "_SNAPSHOT_INDEX_TICKERS\|_SNAPSHOT_INDEX_BDAYS" backend/worker/tasks.py
```

문서에 적은 함수명·상수명·지수코드가 실제와 일치하는지 눈으로 확인한다. **한 글자라도
다르면 고친다** — 이 저장소는 거짓 문서가 세 번 결함 원인이었다.

- [ ] **Step 4: 최종 검증**

```bash
docker compose restart web worker engine
docker compose exec -T web pytest -q
docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/ -q
docker compose exec -T db psql -U quant -d quant -c "select 'coverage' t, count(*) from index_ohlcv_coverage union all select 'store', (select count(*) from stock_daily_snapshots)+(select count(*) from stock_period_stats)+(select count(*) from index_ohlcv)+(select count(*) from index_constituents)+(select count(*) from dart_financials)+(select count(*) from external_fetches);"
docker compose exec -T web alembic current
```

Expected: `840 passed, 23 skipped` (Task 4 가 테스트 2건을 더한다) / 실 DB 포함 전부 통과 / 두 카운트 모두 `0` / `0016 (head)`

- [ ] **Step 5: 커밋**

```bash
git add docs/CONVENTIONS.md docs/improvements.md
git commit -m "docs: 범위 키 소스의 구간 커버리지 조회를 계약에 반영한다" \
  -m "조회 진입점이 둘이 됐다 — 날짜 키는 cached_frame(정확일치), 범위 키는
cached_range(구간 포함). 커버 구간이 '요청 범위'이지 '받아온 행의 범위'가 아니라는
점과 주말 갭을 병합하지 않는 이유를 컨벤션에 남긴다.

§49 의 '범위 키는 선적재로 못 채운다' 한계를 지수 OHLCV 에 한해 해소로 갱신한다.
기간 통계는 구간 자체가 값인 집계라 원리적으로 커버리지가 성립하지 않으므로 한계로
남긴다." \
  -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. 스펙 커버리지**

| 스펙 절 | 구현 태스크 |
|---|---|
| §3.1 요청 범위를 커버로 기록 | Task 3 Step 3 (`merge_coverage(start, min(end, final_through))`) |
| §3.2 확정분으로 잘라 기록 | Task 3 Step 3 (`min(end, final_through)`), Task 1 모델 docstring |
| §3.3 빈 결과 미기록 | Task 3 Step 3 (`if not df.empty:`) + 테스트 |
| §3.4 겹침·인접 병합, 주말 갭 미병합 | Task 2 Step 3 + 테스트 3건 |
| §3.5 부분 커버여도 전 구간 재조회 | Task 3 Step 3 (게이트가 `_covers` 완전 포함만) + 테스트 |
| §3.6 실패 시 보유 구간 첨부, 자동 축소 없음 | Task 3 `_with_coverage_hint` + 테스트 2건 |
| §4 데이터 모델·원장 분리 | Task 1 |
| §5 조회 흐름 | Task 3 |
| §6 `last_final_date` 단일 출처 | Task 3 Step 3 |
| §6 `delete_index_ohlcv` 가 커버리지 삭제 | Task 2 Step 3 + 테스트 |
| §7 야간 400 거래일 선적재 | Task 5 |
| §8 파일 목록 | Task 1~6 전부 |
| §9 테스트 | 각 태스크의 테스트 스텝 |

누락 없음.

**2. 플레이스홀더 스캔**: "TBD"·"적절히 처리"·"위 내용에 대한 테스트 작성" 없음. 모든 코드 스텝에 실제 코드 블록이 있다.

**3. 타입 정합성**

- `read_coverage(index_code) -> list[tuple[date, date]]` — Task 2 정의, Task 3 의 `read_coverage: Callable[[], list[tuple[date, date]]]` 와 Task 4 의 `lambda: _store_read_coverage(ticker)` 가 일치
- `merge_coverage(index_code, start, end) -> None` — Task 2 정의, Task 3 의 `merge_coverage: Callable[[date, date], None]` 는 지수코드가 이미 바인딩된 형태이고 Task 4 의 `lambda f, t: _store_merge_coverage(ticker, f, t)` 가 그 형태를 만든다. 일치
- `last_final_date(*, today=None) -> date` — Task 3 정의, 같은 태스크 안에서만 쓰인다
- `cached_range(key, start, end, *, ...)` — Task 3 정의, Task 4 호출과 인자 순서·이름 일치
- `_snapshot_steps(ymd: str)` — 시그니처 불변, 단계 수만 4 → 7

**4. 검증 수치 추적**: 831(현재) → Task 2 후 831 passed/23 skipped(실 DB 7건 추가) → Task 3 후 838 → Task 4 후 840 → Task 5·6 후 840 유지. 각 태스크의 Expected 와 일치한다.
