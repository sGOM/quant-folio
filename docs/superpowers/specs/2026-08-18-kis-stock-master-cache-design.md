# KIS 종목마스터(매매상태·액면가·업종분류) 로컬 캐싱 설계 (2026-08-18)

## 1. 배경

현재 종목 관련 정적 정보는 세 갈래로 흩어져 있다 — 카탈로그(`symbols.py`, 코드/한글명/
영문명), PIT 업종분류(`krx_index.sector_map` + `sector_map_snapshots`), 펀더멘털·
시가총액(§49 로컬 영구 저장소). 전부 KRX MDC/FDR/DART/pykrx 소스다.

**매매 상태 플래그**(거래정지·관리종목·정리매매·시장경고·불성실공시·우회상장·
단기과열·SPAC)와 **액면가·업종 세분류(대/중/소)**는 이 중 어디에도 없다. 이 설계는
이를 채운다.

**목적(사용자 요청 원문)**: "추후 실행될 기능 중, 로컬 DB에 저장해두면 API 호출
횟수를 확연히 줄일 수 있는 데이터들을 API 호출 허용치 내로 지속적으로 호출하여
저장". 조사 결과 이 전제는 절반만 맞는다 — 이 데이터의 실제 소스는 종목별 REST
호출이 아니라 **KIS 공개 CDN이 배포하는 시장 전체 zip 파일**
(`kospi_code.mst.zip`/`kosdaq_code.mst.zip`, `new.real.download.dws.co.kr`,
공식 `koreainvestment/open-trading-api` 레포의 `stocks_info/kis_kospi_code_mst.py`·
`kis_kosdaq_code_mst.py`가 이 경로로 다운로드한다)라 **인증·유량제한이 아예
없다.** "허용치 내에서 지속 호출"이라는 전제 자체가 이 소스엔 적용되지 않는다 —
시장 전체를 한 번에 받는 것이 유일하고 가장 저렴한 방법이다.

## 2. 목표와 비목표

**목표**: 매일 1회 코스피·코스닥 zip을 받아 파싱해 DB에 시점별로 적재한다. 향후
기능(주문 전 관리종목/거래정지 체크, 스크리너 필터 등)이 이 저장소를 즉시 로컬로
조회할 수 있게 조회 계약까지 마련한다.

**비목표**:
- 이 데이터를 실제로 쓰는 소비처(화면·주문 로직 배선)는 이번 배치에서 만들지
  않는다 — 적재 + 최소 조회 함수까지만.
- 과거 소급 적재는 불가능하다. 이 파일은 **'현재' 상태만** 제공하므로
  `sector_map_snapshots`와 동일한 구조적 한계를 그대로 물려받는다: 스냅샷 도입
  이전 구간은 영원히 확보할 수 없다.

## 3. 핵심 결정

### 3.1 저장 형태 — JSONB raw + 최소 승격 컬럼

시장별 원본 필드는 60~70개(거래정지·관리종목·정리매매·시장경고·불성실공시·
우회상장·단기과열·SPAC·액면가·업종대/중/소분류·상장주수·자본금·결산월·ROE 등).
"미래 기능"을 위한 선제 적재라 지금은 어떤 필드가 실제로 쓰일지 모른다. 70개를
전부 typed 컬럼으로 만들면 스키마가 무거워지고, KIS가 파일 포맷을 바꾸면(과거에도
필드가 추가된 이력이 있다) 마이그레이션이 매번 필요해진다.

`raw JSONB`에 파싱된 전체 딕셔너리를 원본 필드명(한글) 그대로 저장하고, 조회
빈도가 확실한 것만 typed 컬럼으로 승격한다. 지금은 `name`(한글명)만 승격 — 나머지
승격은 실제 소비처가 생길 때 그 소비처의 요구에 맞춰 판단한다(YAGNI).

### 3.2 시장별 파싱 스펙은 별도 함수로 유지한다

코스피와 코스닥은 파일 레이아웃이 다르다 — part1/part2 분리 지점(코스피는 뒤
228바이트, 코스닥은 222바이트가 part2), part2 컬럼 수·필드폭·컬럼명이 다르다
(예: 코스피엔 없는 "벤처기업 여부"가 코스닥엔 있음). 하나로 통합해 분기 처리하면
필드 어긋남을 코드 리뷰로 잡기 어려워지므로, `_parse_kospi`/`_parse_kosdaq` 두
함수로 나누고 각자 자기 시장의 `field_specs`/컬럼명 리스트를 갖는다.

### 3.3 일별 append, 같은 날 재실행은 덮어쓰기(idempotent upsert)

`sector_map_snapshots`는 분기 배치라 "이번 분기에 이미 있으면 skip"이었다. 이
작업은 매일 배치이므로 skip이 아니라 매일 새 `trade_date` 행이 쌓이는 것이 정상
동작이다. 같은 날 재실행(수동 재시도 등)은 `UniqueConstraint(symbol, trade_date)`
기반 `ON CONFLICT DO UPDATE`로 덮어쓴다 — 중복 삽입도, "먼저 실행된 것만 인정"도
아니다.

### 3.4 시장 단위 부분 실패를 허용한다

코스피 다운로드가 실패해도 코스닥은 저장한다(§48 관례와 동일 — 완전한 실패만
전체를 막는다). 두 시장이 모두 실패했을 때만 태스크가 예외를 올린다.

### 3.5 에러는 기존 원인별 계층을 재사용한다

새 예외 클래스를 만들지 않는다. `app/services/data/errors.py`의 기존 계층으로
충분하다:
- 다운로드 실패(네트워크·타임아웃·비-200 응답) → `SourceUnavailableError("kis_master", ...)`
- zip 안에 `.mst` 파일이 없음 / part1·part2 파싱 후 예상 컬럼 수와 불일치 →
  `SourceSchemaError("kis_master", ...)` — KIS가 파일 포맷을 바꿨거나 우리 파서
  버그, 둘 다 재시도로 안 풀리므로 쿨다운 없음
- `note_failure`/`cooldown_remaining`은 그대로 적용 가능(소스명만 `"kis_master"`로
  새로 등록되는 것뿐, 로직 변경 없음)

## 4. 데이터 모델

```
kis_stock_master_snapshots
  id            int          PK
  trade_date    date         not null
  symbol        varchar(20)  not null   -- 단축코드 6자리
  market        varchar(10)  not null   -- "KOSPI" | "KOSDAQ"
  name          varchar(100) not null   -- 한글종목명(조회 편의 승격 컬럼)
  raw           jsonb        not null   -- 파싱된 나머지 전체 필드(표준코드 포함,
                                         --   KIS 원본 필드명 그대로 key)
  created_at    timestamptz  not null default now()

  UniqueConstraint(symbol, trade_date)  name="uq_kis_stock_master_symbol_date"
  Index(trade_date)                     name="ix_kis_stock_master_date"
  Index(symbol)                         name="ix_kis_stock_master_symbol"
```

`sector_map_snapshots`(0010)와 동일한 인덱스 전략 — 종목별 시점 조회와
"as_of 이전 가장 최근 배치일" 탐색 양쪽을 커버한다.

## 5. 파싱 로직

- part1(가변길이 앞부분): 전체 행에서 뒤 228바이트(코스피)/222바이트(코스닥)를
  제외한 나머지를 `[0:9]` 단축코드, `[9:21]` 표준코드, `[21:]` 한글명으로 분리
  (`rstrip`/`strip`).
- part2(고정폭 뒷부분): market별 `field_specs`(폭 리스트)·컬럼명 리스트로
  `pandas.read_fwf` 파싱. 리스트는 공식 레포
  (`koreainvestment/open-trading-api/stocks_info/kis_kospi_code_mst.py`·
  `kis_kosdaq_code_mst.py`, 공개 레포)의 정의를 그대로 이식한다.
- 인코딩은 `cp949`.
- **임시 파일을 쓰지 않는다** — 참고한 원본 스크립트는 디스크에 파일을 풀고
  중간 tmp 파일까지 쓰지만, 이 프로젝트는 `io.BytesIO`(zip 응답) +
  `io.StringIO`(part1/part2 중간 텍스트)로 메모리에서 전부 처리한다.
- part1 행 수와 part2 행 수가 다르면(파일이 깨졌거나 포맷이 바뀐 신호)
  `SourceSchemaError`.

## 6. 적재 흐름 — `snapshot_stock_master(db) -> int`

```
today = KST 오늘 날짜
errors = []
saved_markets = []
for market in (KOSPI, KOSDAQ):
    try:
        content = _download_master(market)      # SourceUnavailableError 가능
        rows = _parse(market, content)           # SourceSchemaError 가능
        upsert(rows, trade_date=today)           # ON CONFLICT (symbol, trade_date) DO UPDATE
        saved_markets.append(market)
    except DataSourceError as e:
        errors.append(e)

if not saved_markets:
    raise representative(errors)   # 두 시장 다 실패했을 때만

return 저장된 총 종목 수
```

`krx_index.snapshot_sector_map`과 같은 트랜잭션 경계(호출자가 커밋)를 따른다 —
이 함수 자체는 세션에 add만 하고 commit은 태스크가 한다.

## 7. 배치 배선

- `worker/tasks.py::snapshot_kis_stock_master`(신규 Celery task) +
  `_snapshot_kis_stock_master_async`(commit/rollback + 실패 알림).
- 실패 시 `publish_alert(user_id=None, strategy_id=0, severity="warning",
  message=f"KIS 종목마스터 적재 실패: {e}", code="kis_master_outage",
  dedup_window_hours=24.0)` — `snapshot_sector_map`과 동일한 sentinel 관례.
- `celery_app.py` beat_schedule에 추가:
  ```python
  "snapshot-kis-stock-master-nightly": {
      "task": "worker.snapshot_kis_stock_master",
      "schedule": crontab(hour=18, minute=40),
  },
  ```
  일봉 적재(18:30)와 로컬 저장소 선적재(18:50) 사이 — 다른 야간 배치와 자원
  경합 없이 끼워 넣는다.

## 8. 조회 헬퍼(최소)

`app/services/data/kis_master.py::latest_stock_master(symbol: str) -> dict | None`
— 해당 종목의 가장 최근 `trade_date` 행을 `{trade_date, market, name, **raw}`
평탄화 딕셔너리로 반환. 이번 배치에서 이 함수의 소비처는 만들지 않지만, "적재만
하고 아무도 못 읽는" 상태를 피하기 위해 최소 계약 하나는 마련한다 — 이후 실제
소비처(예: 주문 전 위험상태 체크)가 생기면 이 함수를 그대로 쓰거나 요구에 맞게
확장한다.

## 9. 파일

- `backend/app/models/models.py` — `KisStockMasterSnapshot` 모델
- `backend/alembic/versions/0017_kis_stock_master_snapshots.py` — 테이블 생성
- `backend/app/services/data/kis_master.py`(신규) — 다운로드·파싱·업서트·
  `latest_stock_master`
- `backend/worker/tasks.py` — `snapshot_kis_stock_master` 태스크
- `backend/worker/celery_app.py` — beat_schedule 추가
- 테스트: `backend/tests/test_kis_master.py` — 파서·다운로드 에러 분류·업서트·
  태스크 알림을 한 파일에서 다룬다(`test_sector_map_snapshot.py` 규모 참고)

## 10. 테스트 계획

| 층 | 검증 |
|---|---|
| 파서 | 코스피 픽스처 정상 파싱(필드 수·대표 값 몇 개), 코스닥 픽스처 정상 파싱, part1/part2 행 수 불일치 → `SourceSchemaError`, zip 안에 `.mst` 없음 → `SourceSchemaError` |
| 다운로드 | httpx 실패(타임아웃/5xx) → `SourceUnavailableError`(httpx mock, 실 네트워크 없음) |
| 업서트 | 신규 적재 건수, 같은 날 재실행 시 덮어쓰기(행 수 불변 — 중복 없음), 한 시장만 실패해도 다른 시장은 저장됨, 두 시장 다 실패 시 대표 예외 raise |
| 태스크 | 실패 시 `publish_alert(code="kis_master_outage")` 발행 — `snapshot_sector_map` 실패 알림 테스트 패턴 재사용 |
| 조회 | `latest_stock_master`가 최신 `trade_date` 행을 반환, 데이터 없으면 `None` |
| 마이그레이션 | 0017 upgrade/downgrade 왕복 |

모든 테스트는 실제 KIS CDN을 타지 않고 httpx 응답을 대역화한다(`docs/CONVENTIONS.md`
"테스트 격리" 원칙 유지).

## 11. 롤백과 최악의 경우

테이블이 비어 있거나 태스크가 계속 실패해도 기존 기능(매매·백테스트·화면)에는
영향이 없다 — 이 데이터를 읽는 소비처가 아직 없기 때문이다. 실패는 warning
알림만 내고 다음 날 자동 재시도된다. 새로운 실패 모드를 만들지 않는다.

## 12. 남은 한계

- **과거 소급 불가**: 파일이 '현재' 상태만 제공하므로 스냅샷 도입 이전 구간은
  영원히 확보할 수 없다(`sector_map_snapshots`와 동일).
- **소비처 부재**: 이 배치만으로는 즉시 체감되는 기능 변화가 없다. 후속 과제
  (예: 주문 전 관리종목·거래정지 체크, 스크리너 필터)에서 `latest_stock_master`를
  실제로 소비할 것.
- **비공식 경로 의존**: `new.real.download.dws.co.kr`는 KIS의 공식 REST TR
  문서가 아니라 공식 GitHub 조직(`koreainvestment`)이 공개한 예제 스크립트에서
  관찰한 다운로드 URL이다. KIS가 이 경로나 파일 포맷을 바꾸면 `SourceSchemaError`로
  드러나긴 하지만(조용히 실패하지 않음), 복구는 코드 수정이 필요하다 — 계약이
  아니라 관찰에 기반한 통합이라는 점을 명시적 리스크로 남긴다.
