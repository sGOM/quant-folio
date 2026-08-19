# KIS 종목마스터 로컬 캐싱 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KIS가 공개 CDN으로 배포하는 시장 전체 종목마스터 zip(코스피/코스닥 —
거래정지·관리종목·정리매매·시장경고·불성실공시·우회상장·단기과열·SPAC·액면가·
업종분류 등)을 매일 1회 받아 파싱해 새 테이블에 시점별 스냅샷으로 저장한다.

**Architecture:** `app/services/data/kis_master.py`(신규)가 다운로드→zip 추출→
고정폭 파싱→DB 적재를 담당하고, `worker/tasks.py`의 신규 Celery beat 태스크가
매일 18:40 KST에 호출한다. 기존 `krx_index.snapshot_sector_map`/
`app/services/data/kofia.py`와 동일한 코드 관례(원인별 `DataSourceError`,
`db.add_all`+`flush`, 실패 시 `publish_alert` sentinel)를 그대로 따른다.

**Tech Stack:** FastAPI/SQLAlchemy(AsyncSession) + Celery + httpx + pandas
(`read_fwf`) — 전부 기존 의존성, 신규 패키지 없음.

**Spec:** `docs/superpowers/specs/2026-08-18-kis-stock-master-cache-design.md`

## Global Constraints

- 다운로드 URL은 인증·유량제한이 없다(`https://new.real.download.dws.co.kr/common/master/{kospi,kosdaq}_code.mst.zip`) — KIS 앱키/토큰을 쓰지 않는다.
- 원본 필드는 `raw JSONB`로 무손실 보존한다. 승격 typed 컬럼은 `name`(한글명) 하나뿐이다.
- 에러는 새 예외 클래스를 만들지 않고 `app/services/data/errors.py`의 기존 계층
  (`SourceUnavailableError`/`SourceSchemaError`)을 소스명 `"kis_master"`로 재사용한다.
- 시장(코스피/코스닥) 단위 부분 실패를 허용한다 — 한 시장 실패는 다른 시장 저장을
  막지 않는다. 두 시장 다 실패했을 때만 예외를 올린다.
- 같은 날 재실행은 삭제 후 재삽입으로 덮어쓴다(중복 행 없음) — 분기 배치인
  `sector_map_snapshots`의 "이미 있으면 skip"과 달리, 이건 매일 배치이므로 skip이
  아니라 항상 최신 상태로 덮어쓴다.
- 모든 테스트는 실 네트워크·실 DB 없이 돈다(httpx는 monkeypatch, DB는 fake 세션
  객체) — `tests/test_krx_index.py`의 `_FakeSnapshotDB` 패턴을 따른다.

---

### Task 1: `KisStockMasterSnapshot` DB 모델 + 마이그레이션

**Files:**
- Modify: `backend/app/models/models.py` (line ~294 부근, `SectorMapSnapshot` 클래스 뒤)
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/0017_kis_stock_master_snapshots.py`
- Test: `backend/tests/test_kis_master.py` (신규 파일 — 이후 Task에서 계속 추가됨)

**Interfaces:**
- Produces: `app.models.KisStockMasterSnapshot`(SQLAlchemy 모델) — 컬럼
  `id, trade_date: date, symbol: str, market: str, name: str, raw: dict, created_at: datetime`.
  이후 모든 Task가 이 모델을 `db.add_all([...])`로 채운다.

- [ ] **Step 1: 스키마 검증 실패 테스트 작성**

`backend/tests/test_kis_master.py` 신규 생성:

```python
"""app/services/data/kis_master.py 단위 테스트.

외부 호출은 httpx 를 대역으로 바꿔 검증한다(네트워크 의존 없음). DB 상호작용은
tests/test_krx_index.py 의 _FakeSnapshotDB 패턴처럼 최소 대역 세션으로 검증한다
(실 DB 연결 없음).
"""
from __future__ import annotations

from datetime import date

import pytest


def test_kis_stock_master_snapshot_model_schema():
    from app.models import KisStockMasterSnapshot

    table = KisStockMasterSnapshot.__table__
    assert table.name == "kis_stock_master_snapshots"
    assert set(table.columns.keys()) == {
        "id", "trade_date", "symbol", "market", "name", "raw", "created_at",
    }
    unique_cols = {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if type(constraint).__name__ == "UniqueConstraint"
    }
    assert ("symbol", "trade_date") in unique_cols
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `docker compose exec web pytest tests/test_kis_master.py -v`
Expected: FAIL — `ImportError: cannot import name 'KisStockMasterSnapshot'`

- [ ] **Step 3: 모델 추가**

`backend/app/models/models.py`에서 `SectorMapSnapshot` 클래스(약 294~318행) 바로
뒤, `# ─────────────────────────── news_articles ───────────────────────────`
주석 앞에 삽입:

```python
# ─────────────────────── kis_stock_master_snapshots ───────────────────────
class KisStockMasterSnapshot(Base):
    """KIS 종목마스터(거래정지·관리종목·액면가·업종분류 등) 일별 스냅샷.

    KRX MDC/FDR/DART 어디에도 없던 매매 상태 플래그를 KIS 공개 CDN 의 시장 전체
    zip(worker.snapshot_kis_stock_master, 매일 18:40 KST)에서 받아 적재한다. 원본
    필드는 시장별로 다르고(60~70개) 소비처가 아직 불확실해 raw JSONB 로 무손실
    보존하고, 조회 빈도가 확실한 name 만 승격 컬럼으로 둔다
    (docs/superpowers/specs/2026-08-18-kis-stock-master-cache-design.md).
    이 파일이 제공하는 정보는 '현재' 상태뿐이라 스냅샷 도입 이전 구간은 소급
    적용이 불가능하다(sector_map_snapshots 와 동일한 구조적 한계).
    """
    __tablename__ = "kis_stock_master_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_kis_stock_master_symbol_date"),
        Index("ix_kis_stock_master_date", "trade_date"),
        Index("ix_kis_stock_master_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

`backend/app/models/__init__.py`에서 `from app.models.models import (...)` 블록에
`Execution`과 `NewsArticle` 사이(알파벳 순)에 `KisStockMasterSnapshot,` 추가하고,
`__all__` 리스트의 `"SectorMapSnapshot",` 바로 뒤에 `"KisStockMasterSnapshot",`
추가.

- [ ] **Step 4: 테스트 실행해 통과 확인**

Run: `docker compose exec web pytest tests/test_kis_master.py -v`
Expected: PASS

- [ ] **Step 5: 마이그레이션 작성**

`backend/alembic/versions/0017_kis_stock_master_snapshots.py` 신규 생성:

```python
"""KIS 종목마스터 스냅샷 테이블 추가 — kis_stock_master_snapshots.

거래정지·관리종목·정리매매·시장경고·불성실공시·우회상장·단기과열·SPAC·액면가·
업종 대/중/소분류 등, KRX MDC/FDR/DART 어디에도 없던 매매 상태 플래그를 KIS
공개 CDN 의 시장 전체 zip(kospi_code.mst/kosdaq_code.mst)에서 매일 받아
시점별로 적재한다. 원본 필드는 시장별로 다르고 향후 소비처의 요구가 아직
불확실해 raw JSONB 로 무손실 보존하고, 조회 빈도가 확실한 name 만 승격
컬럼으로 둔다(docs/superpowers/specs/2026-08-18-kis-stock-master-cache-design.md).

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kis_stock_master_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "symbol", "trade_date", name="uq_kis_stock_master_symbol_date"
        ),
    )
    op.create_index(
        "ix_kis_stock_master_date", "kis_stock_master_snapshots", ["trade_date"]
    )
    op.create_index(
        "ix_kis_stock_master_symbol", "kis_stock_master_snapshots", ["symbol"]
    )


def downgrade() -> None:
    op.drop_index("ix_kis_stock_master_symbol", table_name="kis_stock_master_snapshots")
    op.drop_index("ix_kis_stock_master_date", table_name="kis_stock_master_snapshots")
    op.drop_table("kis_stock_master_snapshots")
```

- [ ] **Step 6: 마이그레이션 적용 확인(수동, 이 저장소는 alembic round-trip 자동
      테스트가 없다 — 기존 관례대로 docker로 직접 검증)**

Run:
```bash
docker compose exec web alembic upgrade head
docker compose exec web alembic downgrade -1
docker compose exec web alembic upgrade head
```
Expected: 세 명령 모두 에러 없이 종료, 마지막에 `kis_stock_master_snapshots`
테이블이 존재.

- [ ] **Step 7: 커밋**

```bash
git add backend/app/models/models.py backend/app/models/__init__.py \
        backend/alembic/versions/0017_kis_stock_master_snapshots.py \
        backend/tests/test_kis_master.py
git commit -m "feat: KIS 종목마스터 스냅샷 테이블 추가"
```

---

### Task 2: 종목마스터 고정폭 파서 (`_parse_master`)

**Files:**
- Create: `backend/app/services/data/kis_master.py`
- Modify: `backend/tests/test_kis_master.py`

**Interfaces:**
- Consumes: 없음(순수 파싱, 네트워크·DB 무관).
- Produces: `kis_master._KOSPI_FIELD_SPECS: list[int]`,
  `kis_master._KOSPI_COLUMNS: list[str]`, `kis_master._KOSDAQ_FIELD_SPECS: list[int]`,
  `kis_master._KOSDAQ_COLUMNS: list[str]`,
  `kis_master._parse_master(text: str, market: str) -> list[dict]` — 각 항목
  `{"symbol": str(6자리), "name": str, "raw": dict[str, str]}`. Task 3이 이 함수를
  다운로드 파이프라인에 연결한다.

이 필드 스펙·컬럼명은 KIS 공식 GitHub(`koreainvestment/open-trading-api`)의
`stocks_info/kis_kospi_code_mst.py`·`kis_kosdaq_code_mst.py`에서 그대로 이식한
것이다 — 임의로 바꾸지 말 것(순서·폭이 실제 파일 레이아웃과 정확히 일치해야
파싱이 맞는다).

- [ ] **Step 1: 실패하는 파서 테스트 작성**

`backend/tests/test_kis_master.py`에 추가(파일 상단 `from __future__ import
annotations` 아래, 기존 `test_kis_stock_master_snapshot_model_schema` 뒤에):

```python
def _build_line(market: str, symbol: str, std_code: str, name: str, values: dict) -> str:
    """테스트용 종목마스터 1행을 실제 파일과 동일한 고정폭 레이아웃으로 만든다.

    프로덕션 파서가 쓰는 것과 동일한 field_specs/columns 를 그대로 가져다 써서
    폭이 어긋날 여지를 없앤다 — 필드 순서가 바뀌면 프로덕션 코드와 이 헬퍼가
    동시에 깨지므로 회귀를 잡을 수 있다.
    """
    from app.services.data import kis_master as km

    field_specs, columns = (
        (km._KOSPI_FIELD_SPECS, km._KOSPI_COLUMNS) if market == "KOSPI"
        else (km._KOSDAQ_FIELD_SPECS, km._KOSDAQ_COLUMNS)
    )
    part2 = "".join(
        str(values.get(col, "")).ljust(width)[:width]
        for col, width in zip(columns, field_specs)
    )
    head = symbol.ljust(9) + std_code.ljust(12) + name
    return head + part2


def test_parse_master_kospi_extracts_flags_and_par_value():
    from app.services.data import kis_master as km

    line = _build_line(
        "KOSPI", "005930", "KR7005930003", "삼성전자",
        {"거래정지": "N", "관리종목": "N", "액면가": "100", "지수업종대분류": "20"},
    )
    rows = km._parse_master(line, "KOSPI")

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "005930"
    assert row["name"] == "삼성전자"
    assert row["raw"]["거래정지"].strip() == "N"
    assert row["raw"]["액면가"].strip() == "100"
    assert row["raw"]["표준코드"] == "KR7005930003"


def test_parse_master_kosdaq_multiple_rows():
    from app.services.data import kis_master as km

    lines = "\n".join([
        _build_line("KOSDAQ", "247540", "KR7247540008", "에코프로비엠",
                    {"거래정지 여부": "N", "관리 종목 여부": "N"}),
        _build_line("KOSDAQ", "086520", "KR7086520004", "에코프로",
                    {"거래정지 여부": "Y", "관리 종목 여부": "N"}),
    ])
    rows = km._parse_master(lines, "KOSDAQ")

    assert [r["symbol"] for r in rows] == ["247540", "086520"]
    assert rows[0]["name"] == "에코프로비엠"
    assert rows[1]["raw"]["거래정지 여부"].strip() == "Y"


def test_parse_master_line_too_short_raises_schema_error():
    from app.services.data.errors import SourceSchemaError
    from app.services.data import kis_master as km

    with pytest.raises(SourceSchemaError):
        km._parse_master("005930짧은행", "KOSPI")


def test_parse_master_empty_text_raises_schema_error():
    from app.services.data.errors import SourceSchemaError
    from app.services.data import kis_master as km

    with pytest.raises(SourceSchemaError):
        km._parse_master("", "KOSPI")
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `docker compose exec web pytest tests/test_kis_master.py -v -k parse_master`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.data.kis_master'`

- [ ] **Step 3: 파서 구현**

`backend/app/services/data/kis_master.py` 신규 생성:

```python
"""KIS 종목마스터(거래정지·관리종목·액면가·업종분류 등) 클라이언트.

목적: KRX MDC/FDR/DART 어디에도 없는 매매 상태 플래그(거래정지·관리종목·
정리매매·시장경고·불성실공시·우회상장·단기과열·SPAC)와 액면가·업종 세분류를
로컬에 확보한다.

데이터 소스: KIS 가 공개 CDN 으로 배포하는 시장 전체 zip 파일
(`https://new.real.download.dws.co.kr/common/master/{kospi,kosdaq}_code.mst.zip`).
**인증·유량제한이 없다** — KIS 앱키/토큰과 무관한 별도 경로다. 필드 스펙은
KIS 공식 GitHub(`koreainvestment/open-trading-api`)의
`stocks_info/kis_kospi_code_mst.py`·`kis_kosdaq_code_mst.py`에서 이식했다.

설계 원칙(kofia.py 와 동일):
- 블로킹(sync) 함수 — 호출부가 스레드풀/asyncio.to_thread 로 실행.
- 전송·스키마 실패는 `app.services.data.errors` 의 원인별 `DataSourceError` 로
  raise 한다. 소스명은 `"kis_master"`.

상세: docs/superpowers/specs/2026-08-18-kis-stock-master-cache-design.md
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date

import httpx
import pandas as pd

from app.services.data.errors import (
    DataSourceError,
    SourceSchemaError,
    classify_httpx,
    note_failure,
    representative,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://new.real.download.dws.co.kr/common/master/{name}.mst.zip"
_FILE_NAMES = {"KOSPI": "kospi_code", "KOSDAQ": "kosdaq_code"}
_TIMEOUT = 30.0

# ─────────────────────────── 코스피 필드 스펙 ───────────────────────────
# KIS 공식 레포 stocks_info/kis_kospi_code_mst.py 의 field_specs/part2_columns 그대로.
_KOSPI_FIELD_SPECS: list[int] = [
    2, 1, 4, 4, 4,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 9, 5, 5, 1,
    1, 1, 2, 1, 1,
    1, 2, 2, 2, 3,
    1, 3, 12, 12, 8,
    15, 21, 2, 7, 1,
    1, 1, 1, 1, 9,
    9, 9, 5, 9, 8,
    9, 3, 1, 1, 1,
]
_KOSPI_COLUMNS: list[str] = [
    "그룹코드", "시가총액규모", "지수업종대분류", "지수업종중분류", "지수업종소분류",
    "제조업", "저유동성", "지배구조지수종목", "KOSPI200섹터업종", "KOSPI100",
    "KOSPI50", "KRX", "ETP", "ELW발행", "KRX100",
    "KRX자동차", "KRX반도체", "KRX바이오", "KRX은행", "SPAC",
    "KRX에너지화학", "KRX철강", "단기과열", "KRX미디어통신", "KRX건설",
    "Non1", "KRX증권", "KRX선박", "KRX섹터_보험", "KRX섹터_운송",
    "SRI", "기준가", "매매수량단위", "시간외수량단위", "거래정지",
    "정리매매", "관리종목", "시장경고", "경고예고", "불성실공시",
    "우회상장", "락구분", "액면변경", "증자구분", "증거금비율",
    "신용가능", "신용기간", "전일거래량", "액면가", "상장일자",
    "상장주수", "자본금", "결산월", "공모가", "우선주",
    "공매도과열", "이상급등", "KRX300", "KOSPI", "매출액",
    "영업이익", "경상이익", "당기순이익", "ROE", "기준년월",
    "시가총액", "그룹사코드", "회사신용한도초과", "담보대출가능", "대주가능",
]

# ─────────────────────────── 코스닥 필드 스펙 ───────────────────────────
# KIS 공식 레포 stocks_info/kis_kosdaq_code_mst.py 의 field_specs/part2_columns 그대로.
_KOSDAQ_FIELD_SPECS: list[int] = [
    2, 1,
    4, 4, 4, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 9,
    5, 5, 1, 1, 1,
    2, 1, 1, 1, 2,
    2, 2, 3, 1, 3,
    12, 12, 8, 15, 21,
    2, 7, 1, 1, 1,
    1, 9, 9, 9, 5,
    9, 8, 9, 3, 1,
    1, 1,
]
_KOSDAQ_COLUMNS: list[str] = [
    "증권그룹구분코드", "시가총액 규모 구분 코드 유가",
    "지수업종 대분류 코드", "지수 업종 중분류 코드", "지수업종 소분류 코드", "벤처기업 여부 (Y/N)",
    "저유동성종목 여부", "KRX 종목 여부", "ETP 상품구분코드", "KRX100 종목 여부 (Y/N)",
    "KRX 자동차 여부", "KRX 반도체 여부", "KRX 바이오 여부", "KRX 은행 여부", "기업인수목적회사여부",
    "KRX 에너지 화학 여부", "KRX 철강 여부", "단기과열종목구분코드", "KRX 미디어 통신 여부",
    "KRX 건설 여부", "(코스닥)투자주의환기종목여부", "KRX 증권 구분", "KRX 선박 구분",
    "KRX섹터지수 보험여부", "KRX섹터지수 운송여부", "KOSDAQ150지수여부 (Y,N)", "주식 기준가",
    "정규 시장 매매 수량 단위", "시간외 시장 매매 수량 단위", "거래정지 여부", "정리매매 여부",
    "관리 종목 여부", "시장 경고 구분 코드", "시장 경고위험 예고 여부", "불성실 공시 여부",
    "우회 상장 여부", "락구분 코드", "액면가 변경 구분 코드", "증자 구분 코드", "증거금 비율",
    "신용주문 가능 여부", "신용기간", "전일 거래량", "주식 액면가", "주식 상장 일자", "상장 주수(천)",
    "자본금", "결산 월", "공모 가격", "우선주 구분 코드", "공매도과열종목여부", "이상급등종목여부",
    "KRX300 종목 여부 (Y/N)", "매출액", "영업이익", "경상이익", "단기순이익", "ROE(자기자본이익률)",
    "기준년월", "전일기준 시가총액 (억)", "그룹사 코드", "회사신용한도초과여부", "담보대출가능여부", "대주가능여부",
]


def _widths_and_columns(market: str) -> tuple[list[int], list[str]]:
    if market == "KOSPI":
        return _KOSPI_FIELD_SPECS, _KOSPI_COLUMNS
    if market == "KOSDAQ":
        return _KOSDAQ_FIELD_SPECS, _KOSDAQ_COLUMNS
    raise ValueError(f"지원하지 않는 시장: {market}")


def _parse_master(text: str, market: str) -> list[dict]:
    """종목마스터 원문(cp949 디코딩 완료 텍스트)을 파싱해 종목별 딕셔너리로 반환한다.

    각 행은 가변길이 head(단축코드 9자·표준코드 12자·한글명 나머지) + 고정폭
    tail(시장별 field_specs 합만큼)로 구성된다. tail 폭은 field_specs 총합으로
    직접 계산한다 — 하드코딩하면 원본 스크립트의 개행문자 포함 여부(228/222)와
    실제 콘텐츠 폭(227/221)이 어긋나는 off-by-one 함정이 있다.

    :raises SourceSchemaError: 행이 tail 폭보다 짧음, 데이터 행이 없음,
        part1/part2 파싱 후 컬럼·행 수가 기대와 다름(포맷이 바뀐 신호)
    """
    field_specs, columns = _widths_and_columns(market)
    tail = sum(field_specs)

    part1_rows: list[tuple[str, str, str]] = []
    part2_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(line) <= tail:
            raise SourceSchemaError(
                "kis_master",
                f"{market} 마스터 행 길이가 예상보다 짧다(len={len(line)}, "
                f"기대 tail={tail}): {line[:60]!r}",
            )
        head = line[: len(line) - tail]
        part1_rows.append((head[0:9].strip(), head[9:21].strip(), head[21:].strip()))
        part2_lines.append(line[-tail:])

    if not part1_rows:
        raise SourceSchemaError("kis_master", f"{market} 마스터 파일에 데이터 행이 없다")

    part2_df = pd.read_fwf(
        io.StringIO("\n".join(part2_lines)), widths=field_specs, names=columns, dtype=str,
    ).fillna("")

    if len(part2_df.columns) != len(columns):
        raise SourceSchemaError(
            "kis_master",
            f"{market} part2 컬럼 수 불일치: {len(part2_df.columns)} != {len(columns)}",
        )
    if len(part2_df) != len(part1_rows):
        raise SourceSchemaError(
            "kis_master",
            f"{market} part1/part2 행 수 불일치: {len(part1_rows)} != {len(part2_df)}",
        )

    rows: list[dict] = []
    for (symbol, std_code, name), (_, part2_row) in zip(part1_rows, part2_df.iterrows()):
        if not symbol or not name:
            continue
        raw = part2_row.to_dict()
        raw["표준코드"] = std_code
        rows.append({"symbol": symbol.zfill(6), "name": name, "raw": raw})
    return rows
```

- [ ] **Step 4: 테스트 실행해 통과 확인**

Run: `docker compose exec web pytest tests/test_kis_master.py -v -k parse_master`
Expected: PASS (4건)

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/kis_master.py backend/tests/test_kis_master.py
git commit -m "feat: KIS 종목마스터 고정폭 파서 추가"
```

---

### Task 3: 다운로드 + zip 추출 + `fetch_market_master` 통합

**Files:**
- Modify: `backend/app/services/data/kis_master.py`
- Modify: `backend/tests/test_kis_master.py`

**Interfaces:**
- Consumes: Task 2의 `_parse_master(text, market) -> list[dict]`.
- Produces: `kis_master.fetch_market_master(market: str) -> list[dict]` — Task 4가
  이 함수를 시장별로 호출해 DB에 적재한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_kis_master.py`에 추가:

```python
class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            import httpx
            request = httpx.Request("GET", "https://example.test")
            response = httpx.Response(self._status, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)


def _zip_bytes(inner_name: str, content: bytes) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_name, content)
    return buf.getvalue()


def test_fetch_market_master_downloads_extracts_and_parses(monkeypatch):
    from app.services.data import kis_master as km

    line = _build_line(
        "KOSPI", "005930", "KR7005930003", "삼성전자", {"거래정지": "N"},
    )
    zip_content = _zip_bytes("kospi_code.mst", line.encode("cp949"))

    captured = {}

    def fake_get(url, timeout=None, follow_redirects=None):
        captured["url"] = url
        return _Resp(zip_content)

    monkeypatch.setattr(km.httpx, "get", fake_get)

    rows = km.fetch_market_master("KOSPI")

    assert "kospi_code.mst.zip" in captured["url"]
    assert rows[0]["symbol"] == "005930"


def test_fetch_market_master_download_failure_raises_unavailable(monkeypatch):
    from app.services.data import kis_master as km
    from app.services.data.errors import SourceUnavailableError

    def boom(*a, **kw):
        raise RuntimeError("연결 실패")

    monkeypatch.setattr(km.httpx, "get", boom)

    with pytest.raises(SourceUnavailableError):
        km.fetch_market_master("KOSPI")


def test_fetch_market_master_zip_without_mst_raises_schema_error(monkeypatch):
    from app.services.data import kis_master as km
    from app.services.data.errors import SourceSchemaError

    zip_content = _zip_bytes("readme.txt", b"not the master file")
    monkeypatch.setattr(km.httpx, "get", lambda *a, **kw: _Resp(zip_content))

    with pytest.raises(SourceSchemaError):
        km.fetch_market_master("KOSPI")


def test_fetch_market_master_bad_zip_raises_schema_error(monkeypatch):
    from app.services.data import kis_master as km
    from app.services.data.errors import SourceSchemaError

    monkeypatch.setattr(km.httpx, "get", lambda *a, **kw: _Resp(b"this is not a zip"))

    with pytest.raises(SourceSchemaError):
        km.fetch_market_master("KOSPI")
```

파일 상단 import에 `import io`가 없으면 추가(Task 2에서 이미 프로덕션 코드에
있지만 테스트 파일에도 `io.BytesIO` 사용을 위해 필요 — 이미 `_zip_bytes`가
`import zipfile`을 지역 임포트하듯 `import io`도 파일 상단에 추가).

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `docker compose exec web pytest tests/test_kis_master.py -v -k fetch_market_master`
Expected: FAIL — `AttributeError: module 'app.services.data.kis_master' has no attribute 'fetch_market_master'`

- [ ] **Step 3: 다운로드·추출·통합 함수 구현**

`backend/app/services/data/kis_master.py`의 `_parse_master` 함수 뒤에 추가:

```python
def _download_zip(market: str) -> bytes:
    url = _BASE_URL.format(name=_FILE_NAMES[market])
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception as e:  # noqa: BLE001
        exc = classify_httpx("kis_master", e)
        note_failure(exc)
        logger.warning("KIS 종목마스터(%s) 다운로드 실패: %s", market, exc)
        raise exc from e


def _extract_mst_text(zip_bytes: bytes, market: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if n.endswith(".mst")]
            if not names:
                raise SourceSchemaError(
                    "kis_master", f"{market} zip 안에 .mst 파일이 없다: {zf.namelist()}",
                )
            with zf.open(names[0]) as f:
                return f.read().decode("cp949")
    except zipfile.BadZipFile as e:
        raise SourceSchemaError("kis_master", f"{market} zip 파싱 실패: {e}") from e


def fetch_market_master(market: str) -> list[dict]:
    """market("KOSPI"|"KOSDAQ")의 종목마스터를 다운로드·파싱해 반환한다.

    각 항목: {"symbol": 6자리 코드, "name": 한글명, "raw": {필드명: 값, ...}}.

    :raises DataSourceError: 다운로드 실패(SourceUnavailableError 등) 또는
        파싱 실패(SourceSchemaError)
    """
    zip_bytes = _download_zip(market)
    text = _extract_mst_text(zip_bytes, market)
    return _parse_master(text, market)
```

- [ ] **Step 4: 테스트 실행해 통과 확인**

Run: `docker compose exec web pytest tests/test_kis_master.py -v`
Expected: PASS (전체)

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/kis_master.py backend/tests/test_kis_master.py
git commit -m "feat: KIS 종목마스터 다운로드·zip 추출 연결"
```

---

### Task 4: DB 적재 오케스트레이션 + 조회 헬퍼

**Files:**
- Modify: `backend/app/services/data/kis_master.py`
- Modify: `backend/tests/test_kis_master.py`

**Interfaces:**
- Consumes: Task 3의 `fetch_market_master(market) -> list[dict]`, Task 1의
  `app.models.KisStockMasterSnapshot`.
- Produces: `kis_master.snapshot_stock_master(db, trade_date=None) -> int`,
  `kis_master.latest_stock_master(db, symbol) -> dict | None`. Task 5가
  `snapshot_stock_master`를 Celery 태스크에서 호출한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_kis_master.py`에 추가:

```python
class _FakeMasterDB:
    """KisStockMasterSnapshot 대상 execute/add_all/flush 만 지원하는 최소 대역."""

    def __init__(self):
        self.deleted_markets: list[str] = []
        self.added: list = []
        self.flushed = False

    async def execute(self, stmt):
        self.deleted_markets.append(str(stmt))
        return None

    def add_all(self, objs):
        self.added.extend(objs)

    async def flush(self):
        self.flushed = True


def test_snapshot_stock_master_saves_both_markets(monkeypatch):
    import asyncio

    from app.services.data import kis_master as km

    def _fake_fetch(market):
        if market == "KOSPI":
            return [{"symbol": "005930", "name": "삼성전자", "raw": {"거래정지": "N"}}]
        return [{"symbol": "247540", "name": "에코프로비엠", "raw": {"거래정지 여부": "N"}}]

    monkeypatch.setattr(km, "fetch_market_master", _fake_fetch)
    db = _FakeMasterDB()

    n = asyncio.run(km.snapshot_stock_master(db, trade_date=date(2026, 8, 19)))

    assert n == 2
    assert {o.symbol for o in db.added} == {"005930", "247540"}
    assert {o.market for o in db.added} == {"KOSPI", "KOSDAQ"}
    assert all(o.trade_date == date(2026, 8, 19) for o in db.added)
    assert db.flushed


def test_snapshot_stock_master_partial_failure_still_saves_other_market(monkeypatch):
    import asyncio

    from app.services.data import kis_master as km
    from app.services.data.errors import SourceUnavailableError

    def _fake_fetch(market):
        if market == "KOSPI":
            raise SourceUnavailableError("kis_master", "다운로드 실패")
        return [{"symbol": "247540", "name": "에코프로비엠", "raw": {}}]

    monkeypatch.setattr(km, "fetch_market_master", _fake_fetch)
    db = _FakeMasterDB()

    n = asyncio.run(km.snapshot_stock_master(db, trade_date=date(2026, 8, 19)))

    assert n == 1
    assert {o.symbol for o in db.added} == {"247540"}


def test_snapshot_stock_master_both_markets_fail_raises(monkeypatch):
    import asyncio

    from app.services.data import kis_master as km
    from app.services.data.errors import DataSourceError, SourceUnavailableError

    def _fake_fetch(market):
        raise SourceUnavailableError("kis_master", f"{market} 다운로드 실패")

    monkeypatch.setattr(km, "fetch_market_master", _fake_fetch)
    db = _FakeMasterDB()

    with pytest.raises(DataSourceError):
        asyncio.run(km.snapshot_stock_master(db, trade_date=date(2026, 8, 19)))

    assert db.added == []


class _FakeScalarDB:
    def __init__(self, row):
        self._row = row

    async def scalar(self, stmt):
        return self._row


def test_latest_stock_master_returns_none_when_missing():
    import asyncio

    from app.services.data import kis_master as km

    db = _FakeScalarDB(None)
    result = asyncio.run(km.latest_stock_master(db, "999999"))
    assert result is None


def test_latest_stock_master_flattens_raw():
    import asyncio

    from app.services.data import kis_master as km

    class _Row:
        trade_date = date(2026, 8, 19)
        market = "KOSPI"
        name = "삼성전자"
        raw = {"거래정지": "N"}

    db = _FakeScalarDB(_Row())
    result = asyncio.run(km.latest_stock_master(db, "005930"))

    assert result == {
        "trade_date": date(2026, 8, 19), "market": "KOSPI",
        "name": "삼성전자", "거래정지": "N",
    }
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `docker compose exec web pytest tests/test_kis_master.py -v -k "snapshot_stock_master or latest_stock_master"`
Expected: FAIL — `AttributeError`(함수 없음)

- [ ] **Step 3: 오케스트레이션·조회 함수 구현**

`backend/app/services/data/kis_master.py`의 `fetch_market_master` 함수 뒤에 추가.
파일 상단 import 블록도 갱신(`from datetime import date` 는 이미 있음,
`representative`는 이미 import 됨 — 아래 두 함수가 실제로 쓰는 것만 정리):

```python
async def snapshot_stock_master(db, trade_date: date | None = None) -> int:
    """KOSPI+KOSDAQ 종목마스터를 지정일(기본 오늘) 스냅샷으로 적재한다.

    한 시장이 실패해도 다른 시장은 저장한다(§48 부분 실패 관례) — 두 시장이 모두
    실패했을 때만 대표 예외를 올린다. 같은 날 재실행은 해당 시장 행을 지우고
    다시 넣어 덮어쓴다(멱등). flush 만 하고 commit 은 호출부 책임이다
    (krx_index.snapshot_sector_map 과 동일한 트랜잭션 경계).

    :raises DataSourceError: 두 시장 모두 실패했을 때, 원인 우선순위상 대표 예외
    """
    from sqlalchemy import delete

    from app.models import KisStockMasterSnapshot

    snap_date = trade_date or date.today()
    errors: list[DataSourceError] = []
    saved = 0

    for market in ("KOSPI", "KOSDAQ"):
        try:
            rows = fetch_market_master(market)
        except DataSourceError as e:
            errors.append(e)
            logger.warning("KIS 종목마스터(%s) 적재 실패 — 스킵: %s", market, e)
            continue

        await db.execute(
            delete(KisStockMasterSnapshot).where(
                KisStockMasterSnapshot.market == market,
                KisStockMasterSnapshot.trade_date == snap_date,
            )
        )
        db.add_all(
            [
                KisStockMasterSnapshot(
                    trade_date=snap_date, symbol=r["symbol"], market=market,
                    name=r["name"], raw=r["raw"],
                )
                for r in rows
            ]
        )
        saved += len(rows)

    if saved == 0 and errors:
        raise representative(errors)

    await db.flush()
    logger.info(
        "KIS 종목마스터 스냅샷 적재: trade_date=%s %d종목(성공 시장 %d/2)",
        snap_date, saved, 2 - len(errors),
    )
    return saved


async def latest_stock_master(db, symbol: str) -> dict | None:
    """symbol 의 가장 최근 종목마스터 스냅샷을 반환한다. 없으면 None.

    반환 딕셔너리: {"trade_date", "market", "name", **raw}.
    """
    from sqlalchemy import select

    from app.models import KisStockMasterSnapshot

    row = await db.scalar(
        select(KisStockMasterSnapshot)
        .where(KisStockMasterSnapshot.symbol == symbol)
        .order_by(KisStockMasterSnapshot.trade_date.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {"trade_date": row.trade_date, "market": row.market, "name": row.name, **row.raw}
```

- [ ] **Step 4: 테스트 실행해 통과 확인**

Run: `docker compose exec web pytest tests/test_kis_master.py -v`
Expected: PASS (전체)

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/kis_master.py backend/tests/test_kis_master.py
git commit -m "feat: KIS 종목마스터 DB 적재·조회 함수 추가"
```

---

### Task 5: Celery 배치 배선

**Files:**
- Modify: `backend/worker/tasks.py`
- Modify: `backend/worker/celery_app.py`
- Create: `backend/tests/test_kis_master_snapshot_task.py`

**Interfaces:**
- Consumes: Task 4의 `kis_master.snapshot_stock_master(db, trade_date=None) -> int`.
- Produces: Celery task `worker.snapshot_kis_stock_master`(beat 스케줄에 등록됨,
  매일 18:40 KST). 이후 소비처는 이 태스크가 채운 `kis_stock_master_snapshots`를
  `kis_master.latest_stock_master`로 읽으면 된다(이번 계획 범위 밖).

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_kis_master_snapshot_task.py` 신규 생성
(`tests/test_sector_map_snapshot.py`와 동일한 대역 패턴):

```python
"""worker/tasks.py::snapshot_kis_stock_master 실패 시 알림 발행 검증.

매일 배치라 조용히 실패하면 다음날까지 kis_stock_master_snapshots 가 스테일
상태로 남을 수 있다. user_id=None + severity="warning" 조합은 publish_alert 가
WS·텔레그램을 건너뛰고 DB 영속화만 시도하므로(test_alerts_cleanup.py 와 동일
사유), 로그로 발행 여부를 확인한다.
"""
import logging

import pytest

from tests.conftest import FakeRedis


class _FakeSessionBoom:
    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def rollback(self) -> None:
        return None


class _FakeAsyncRedisConn(FakeRedis):
    async def aclose(self) -> None:
        return None


class _FakeRedisCls:
    last: "_FakeAsyncRedisConn | None" = None

    @classmethod
    def from_url(cls, _url):
        cls.last = _FakeAsyncRedisConn()
        return cls.last


async def test_kis_master_snapshot_failure_publishes_alert_and_reraises(monkeypatch, caplog):
    from app.core import database
    from app.services.data import kis_master
    from worker import tasks

    monkeypatch.setattr(database, "AsyncSessionLocal", _FakeSessionBoom())

    async def _boom(_db):
        raise RuntimeError("KIS 종목마스터 조회 실패")

    monkeypatch.setattr(kis_master, "snapshot_stock_master", _boom)
    monkeypatch.setattr("redis.asyncio.Redis", _FakeRedisCls)

    with caplog.at_level(logging.WARNING, logger="engine.alerts"):
        with pytest.raises(RuntimeError, match="KIS 종목마스터 조회 실패"):
            await tasks._snapshot_kis_stock_master_async()

    assert "kis_master_outage" in caplog.text
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `docker compose exec web pytest tests/test_kis_master_snapshot_task.py -v`
Expected: FAIL — `AttributeError: module 'worker.tasks' has no attribute
'_snapshot_kis_stock_master_async'`

- [ ] **Step 3: Celery 태스크 추가**

`backend/worker/tasks.py`에서 `snapshot_sector_map` 태스크 정의(약 263~273행)
바로 뒤, `# DB 백업(E-2)` 주석 앞에 삽입:

```python
async def _snapshot_kis_stock_master_async() -> dict:
    from app.core.config import settings
    from app.core.database import AsyncSessionLocal
    from app.services.data.kis_master import snapshot_stock_master

    async with AsyncSessionLocal() as db:
        try:
            n = await snapshot_stock_master(db)
            await db.commit()
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            # 매일 배치라 조용히 실패하면 다음날 재시도까지 스테일 상태로 남는다.
            # 다른 배치 태스크와 동일한 sentinel 관례로 알린다.
            from redis.asyncio import Redis

            from engine.alerts import publish_alert

            redis = Redis.from_url(settings.REDIS_URL)
            try:
                await publish_alert(
                    redis, user_id=None, strategy_id=0, severity="warning",
                    message=f"KIS 종목마스터 적재 실패: {e}",
                    code="kis_master_outage",
                    dedup_window_hours=24.0,
                )
            finally:
                await redis.aclose()
            raise

    result = {"snapshot_symbols": n}
    logger.info("KIS 종목마스터 스냅샷 적재 완료: %s", result)
    return result


@celery_app.task(name="worker.snapshot_kis_stock_master")
def snapshot_kis_stock_master() -> dict:
    """KIS 종목마스터(거래정지·관리종목·액면가·업종분류 등)를 매일 1회 적재한다.

    코스피·코스닥 zip 을 받아 파싱해 kis_stock_master_snapshots 에 스냅샷으로
    쌓는다(docs/superpowers/specs/2026-08-18-kis-stock-master-cache-design.md).
    한 시장만 실패해도 다른 시장은 저장되고, 둘 다 실패했을 때만 알림 발행 후
    예외를 전파한다.
    """
    return _run_async(_snapshot_kis_stock_master_async())
```

- [ ] **Step 4: 테스트 실행해 통과 확인**

Run: `docker compose exec web pytest tests/test_kis_master_snapshot_task.py -v`
Expected: PASS

- [ ] **Step 5: beat_schedule 등록**

`backend/worker/celery_app.py`의 `beat_schedule` 딕셔너리에서
`"ingest-daily-snapshots"` 항목(18:50) 뒤에 추가:

```python
    # KIS 종목마스터(거래정지·관리종목·액면가·업종분류) 일별 스냅샷 — 일봉 적재
    # (18:30)와 로컬 저장소 선적재(18:50) 사이. 인증·유량제한 없는 시장 전체 zip
    # 다운로드라 다른 배치와 자원 경합이 없다.
    "snapshot-kis-stock-master-nightly": {
        "task": "worker.snapshot_kis_stock_master",
        "schedule": crontab(hour=18, minute=40),
    },
```

- [ ] **Step 6: 워커 임포트 확인**

Run: `docker compose exec web python -c "from worker.celery_app import celery_app; from worker import tasks; assert 'snapshot-kis-stock-master-nightly' in celery_app.conf.beat_schedule; print('OK')"`
Expected: `OK` 출력(임포트 에러 없음, beat_schedule에 등록됨).

- [ ] **Step 7: 전체 스위트 회귀 확인**

Run: `docker compose exec web pytest -q`
Expected: 기존 통과 건수 + 이번에 추가한 테스트(모델 스키마 1 + 파서 4 + 다운로드
4 + 오케스트레이션 5 + 태스크 1 = 15건) 전부 PASS, 기존 실패 없음.

- [ ] **Step 8: 커밋**

```bash
git add backend/worker/tasks.py backend/worker/celery_app.py \
        backend/tests/test_kis_master_snapshot_task.py
git commit -m "feat: KIS 종목마스터 일별 배치 태스크 배선"
```

---

## Post-Plan Verification (수동)

계획 완료 후, 실제 재빌드된 컨테이너에서 1회 수동 실행으로 실 네트워크 경로를
확인한다(테스트는 전부 대역이라 실제 KIS CDN 응답 형식은 검증하지 못한다):

```bash
docker compose restart worker
docker compose exec worker celery -A worker.celery_app.celery_app call worker.snapshot_kis_stock_master
docker compose logs -f worker | grep -i "KIS 종목마스터"
```

기대: 로그에 "KIS 종목마스터 스냅샷 적재 완료: {'snapshot_symbols': N}"(N은
2000~2700 부근)이 찍히고, 다음 쿼리로 실제 행이 보인다:

```bash
docker compose exec db psql -U quant -d quant -c \
  "SELECT market, count(*) FROM kis_stock_master_snapshots GROUP BY market;"
```

이 단계는 자동화된 테스트로 대체할 수 없다(실 KIS CDN 응답 형식이 스펙과
일치하는지는 네트워크 접근 없이는 확인 불가) — 계획 실행자가 직접 확인하고
결과를 보고할 것.

**검증 완료(2026-08-19)**: `alembic upgrade head`(0016→0017) 적용 후 워커
재기동, `celery call worker.snapshot_kis_stock_master` 수동 실행. 로그
"KIS 종목마스터 스냅샷 적재: trade_date=2026-08-19 4385종목(성공 시장 2/2)".
DB 확인: KOSPI 2563 / KOSDAQ 1822 행, 삼성전자(005930) raw 필드 정상 파싱
(거래정지="N", 관리종목="N"). 실 KIS CDN 응답 형식이 파서 스펙과 일치함을
확인. 관련 테스트 45건(`test_kis_master.py`·`test_kis_master_snapshot_task.py`·
`test_risk_evaluate.py`) 통과.
