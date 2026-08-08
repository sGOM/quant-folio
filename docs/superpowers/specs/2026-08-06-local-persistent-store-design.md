# 확정 과거 데이터의 로컬 영구 저장 — 설계

작성일: 2026-08-06
관련: `docs/superpowers/specs/2026-08-04-external-api-silent-failure-design.md`(§48 경계 정의),
`docs/improvements.md` §44-1(KRX 로그인 차단)·§47(폐기된 검증), `docs/improvements.md` C-1(`price_ticks` 로컬 적재)
브랜치: `fix/pykrx-silent-failure`

## 1. 문제

이미 확정된 과거 데이터를 백테스트가 돌 때마다 외부(pykrx·KRX MDC·OpenDART)에서 다시
조회한다. 2019년 3월 12일의 전 종목 PER 은 다시는 바뀌지 않는데도, 그 날짜를 밟는
백테스트는 매번 KRX 에 묻는다.

이 구조가 두 가지 대가를 치른다.

**첫째, 외부 가용성이 곧 백테스트 가용성이다.** §44-1 에서 KRX 가 로그인을 차단하자
PIT 조회가 전부 0종목을 반환했고 백테스트는 빈 패널 위에서 '성공'했다. §48 이
`krx_index`·`opendart`·`kofia` 세 모듈에 예외 전파를 깔아 조용한 실패를 없앴지만,
`app/services/metrics/fetch.py` 는 그 범위 밖이었다. 그 결과 §47 검증(id=23 폭락장
4-arm)이 다시 같은 방식으로 무의미한 수치를 냈다 — 3개 arm 이 바이트 단위로 동일했고
레짐 오버레이가 한 번도 발화하지 않았으며 펀더멘털 조회가 전량 실패했는데도 스크립트는
exit 0 으로 완주해 리포트를 출력했다. 원인은 `_fetch_per_market` 의
`except Exception → 빈 프레임`이다.

**둘째, 반복 조회 자체가 차단을 부른다.** §47 검증 직전까지 pykrx 로그인은 성공하고
있었다. 그 사이 한 일은 검증 스크립트 실행뿐이었다. 즉 반복 조회가 차단을 자초했을
가능성이 크고, 차단 상태에서 재시도하면 상황은 더 나빠진다(§44-1).

두 대가는 같은 뿌리다. **불변인 데이터를 매번 네트워크에서 가져온다.**

## 2. 목표와 비목표

**목표**: 확정된 과거 데이터를 로컬(Postgres)에 한 번 저장하고 이후로는 로컬에서만
읽는다. 외부 호출은 "아직 로컬에 없는 것"에 한해 1회만 발생한다.

**비목표(이번 범위 밖)**:
- `opendart.corp_code_map()` — 현재 상장 매핑이지 확정 과거 데이터가 아니다.
- `panic.py` 의 파일 캐시(`.cache/panic_breadth_*.json`) 정리 — `stock_daily_snapshots`
  가 들어오면 잉여가 되지만 후속 작업으로 남긴다.
- `price_ticks`(종목 일봉) — C-1 에서 이미 로컬 우선이다.
- 자동 백오프 재시도 — §48 과 동일하게 이번에도 다루지 않는다.

## 3. 대상 데이터 6종

| 데이터 | 현재 캐시 | 조회 함수 |
|---|---|---|
| 펀더멘털(PER/PBR/DIV) | 프로세스 내 LRU 64 | `metrics/fetch._fetch_fundamentals` |
| 시가총액·상장주식수 | 없음 | `metrics/fetch._fetch_market_cap` |
| 기간 등락률·순매수 | 없음 | `_fetch_price_change`·`_fetch_net_purchases` |
| 전종목/지수 OHLCV | 없음 | `_fetch_market_ohlcv_snapshot`·`_fetch_index_ohlcv` |
| PIT 지수구성 | 프로세스 내 dict | `data/krx_index.index_members` |
| DART 재무 | 프로세스 내 dict | `data/opendart.single_company_accounts` |

## 4. 저장 스키마 — 5테이블 + 원장

선례는 `SectorMapSnapshot`(`app/models/models.py:294`, 마이그레이션
`0010_sector_map_snapshots.py`)과 `PriceTick`(같은 파일 244행, TimescaleDB hypertable).

### 4.1 `stock_daily_snapshots` — PK `(trade_date, symbol)`

펀더멘털·시총·전종목 OHLCV 세 조회가 모두 (날짜 × 종목) 격자라 한 장으로 접는다.
`trade_date` 기준 hypertable.

| 컬럼 | 출처 |
|---|---|
| `market` | 조회 시장(KOSPI/KOSDAQ) |
| `per`, `pbr`, `div` | `get_market_fundamental`. PER≤0 → NULL(적자 규칙 유지) |
| `market_cap`, `shares` | `get_market_cap`(시가총액·상장주식수) |
| `open`/`high`/`low`/`close`/`volume`/`trading_value`/`change_pct` | `get_market_ohlcv` |

세 소스가 서로 다른 시점에 채우므로 전 컬럼 nullable. upsert 는 **들어온 값이 NULL 이면
기존값을 보존**한다(`COALESCE(EXCLUDED.x, t.x)`) — 시총만 적재된 행을 펀더멘털 적재가
지우면 안 된다.

### 4.2 `stock_period_stats` — PK `(start_date, end_date, investors, symbol)`

기간키 데이터는 일봉에서 재유도할 수 없다. pykrx 의 기간 등락률은 수정주가 기준이라
`price_ticks` 종가로 다시 계산하면 액면분할·유상증자 구간에서 값이 갈린다. 원본 그대로
보관한다.

컬럼: `market`, `change_pct`, `open`, `close`, `volume`, `trading_value`(←
`_fetch_price_change`), `net_buy_value`(← `_fetch_net_purchases`).
`investors` 는 투자자군 조합(정렬 후 `,` 조인, 기본 `"기관합계,외국인"`)이며, 조합이
달라지면 다른 행이다. 등락률만 조회한 행은 `investors=''`.

### 4.3 `index_ohlcv` — PK `(index_code, trade_date)`

`open`/`high`/`low`/`close`/`volume`/`trading_value` + `index_name`.
컬럼명은 `_fetch_index_ohlcv` 의 한글→영문 변환 결과를 그대로 쓴다.

### 4.4 `index_constituents` — PK `(index_code, base_date, symbol)`

`krx_index.index_members(as_of, index)` 의 PIT 결과.

### 4.5 `dart_financials` — PK `(corp_code, bsns_year, reprt_code, fs_div)`

`accounts` JSONB 에 **원계정 리스트를 그대로** 넣는다. 파생지표(`derive_metrics`·
`piotroski_f_score`)를 저장하지 않는 이유는 파생 코드가 바뀌면 저장값이 낡기 때문이다.
원계정은 안 바뀐다.

부가 컬럼: `rcept_no`, `rcept_dt`, `fetched_at`, `confirmed_at`.

### 4.6 `external_fetches`(원장) — PK `(source, cache_key)`

컬럼: `fetched_at`, `row_count`, `final`(bool).

`source` 는 조회 종류(`fundamentals`·`market_cap`·`price_change`·`net_purchases`·
`market_ohlcv`·`index_ohlcv`·`index_members`·`dart_accounts`), `cache_key` 는 그 조회의
인자를 정렬·결정적으로 직렬화한 문자열이다(예: `20190312|KOSDAQ,KOSPI`,
`20190101~20190331|KOSPI|기관합계,외국인`). 같은 인자가 항상 같은 키를 만들어야 하므로
시장 목록·투자자군은 반드시 정렬한다.

**원장이 없으면 이 설계는 §48 이 닫으려던 실패 모드를 그대로 재현한다.** 정규화 테이블
단독으로는 "휴장일이라 0행"과 "아직 적재 안 됨"이 똑같이 0행이다. 조회 사실 자체를
따로 기록해야 둘이 갈린다.

## 5. 조회 계약 — 4상태 분리

`app/services/data/local_store.py`:

```
cached_frame(source, cache_key, *, read_local, fetch_remote, write_local, is_final)
```

| 원장 상태 | 동작 |
|---|---|
| `final=True` 기록 있음 | `read_local()` 반환. **0행이면 0행 그대로** = 진짜 데이터 없음 |
| 기록 없음 또는 `final=False` | `fetch_remote()` 1회 → 성공 시 `write_local()` + 원장 기록 |
| `fetch_remote()` 실패 | `DataSourceError` **그대로 raise**. 빈 프레임으로 삼키지 않는다 |
| 소스 미설정 | 스토어 진입 전에 통과(§48 "미설정" 경로 불변) |

이에 맞춰 `_fetch_per_market` 의 `except Exception → 빈 프레임`을 걷어낸다. 시장별 부분
실패는 `data/errors.stop_aggregate(source, errors, ok)` 에 넘겨 체계적 실패 여부를
판정하게 한다(§48 의 집계 단락 규칙 재사용). 일부 시장만 실패하고 나머지가 성공하면
성공분만 저장하되 원장에는 기록하지 않는다 — 부분 결과를 확정으로 굳히지 않기 위함이다.

**개정(I3, 2026-08-08 통합 리뷰)**: 위 표의 "0행이면 0행 그대로 = 진짜 데이터 없음"은
최초 설계 의도였지만 실제로는 위험했다 — 호출자가 넘긴 `is_final`을 그대로 믿으면,
"정상 응답인데 스키마가 바뀌어 값을 잃음"과 "진짜 휴장일/무자료"를 구분할 수 없는
채로 0행이 영구 확정된다(`krx_index.index_members`가 이미 이 이유로 `if codes:`
가드를 두고 있었는데, 코어 `cached_frame`에는 같은 가드가 없었다 — Task 8 리뷰가
blocking 으로 지적). 그래서 계약을 좁혔다: **`row_count==0`이면 호출자가 넘긴
`is_final`과 무관하게 항상 `final=False`로 내린다.** 이 표의 첫 행은 "0행이면서
동시에 final=True 로 남는" 경우가 이제 DART(OpenDART status 013, 명시적 무자료
선언 — `cached_frame`을 거치지 않고 `dart_store`가 자체 경로로 기록)에만 해당한다는
뜻으로 읽어야 한다.

## 6. 확정·정정 규칙

- **시장데이터**: 캐시키의 마지막 날짜가 `< 오늘(KST)` 이면 `final=True` 로 영구 확정.
  당일분은 저장하되 `final=False` 로 남겨 다음 호출 때 재조회한다 — 장중 미확정값이
  영구히 굳는 것을 막는다.
- **DART**: `confirmed_at = rcept_dt + 90일`. `today >= confirmed_at` 이면 `final=True`,
  그 전에는 `final=False` 로 재조회를 허용한다(정정공시 반영). `rcept_dt` 를 못 구하면
  보수적으로 `bsns_year 말일 + 1년`.

## 7. 적재 경로

- **온디맨드 write-through**: §5 계약 그대로. 백테스트가 처음 밟는 날짜가 자동으로
  영구화된다. 두 번째 실행부터 그 날짜는 네트워크를 타지 않는다.
- **야간 배치**: `worker.ingest_daily_ohlcv`(Celery beat) 옆에 `ingest_daily_snapshots`
  를 추가해 전날 확정분(펀더멘털·시총·전종목 OHLCV·지수 OHLCV)을 선적재한다. 실패
  임계 알림은 `_INGEST_FAILURE_ALERT_RATIO`(10%) 방식을 재사용한다.

## 8. DB 접근 — 전용 NullPool 엔진

`metrics/fetch.py` 는 동기 코드이고 호출자가 `asyncio.to_thread` 로 실행한다. 즉
**메인 이벤트루프가 살아있는 채로** 워커 스레드에서 돈다.

`worker/tasks.py:21-39` 의 기존 해법(`asyncio.run` 후 `engine.dispose()`)은 여기서 쓸 수
없다. 전역 엔진을 dispose 하면 메인 루프가 쓰던 커넥션까지 끊긴다. 그렇다고 dispose
없이 전역 엔진을 쓰면 asyncpg 커넥션이 루프에 묶여 있어
`Future attached to a different loop` 로 죽는다 — 워커에서 실제로 겪은 잠복 버그다.

→ `app/core/local_store_db.py` 에 `create_async_engine(DATABASE_URL, poolclass=NullPool)`
전용 엔진과 세션팩토리를 둔다. 매 호출이 제 루프의 새 커넥션을 열고 닫으므로 교차 루프
재사용이 원천적으로 불가능하고, 전역 풀을 건드리지 않는다. psycopg2 등 신규 의존성도
필요 없다(requirements 에는 asyncpg 만 있다). 호출 빈도는 리밸런싱 날짜 단위라 커넥션
수립 비용은 무시할 수준이다.

동기 진입점은 `asyncio.get_running_loop()` 이 잡히면 즉시 `RuntimeError` 를 던진다.
async 컨텍스트에서 오용하면 조용히 막히는 대신 터지게 한다.

**개정(2026-08-08, 통합 리뷰 B2)**: raise 하지 않고 **전용 워커 스레드에서 새 루프로
실행**하도록 완화했다. 위 서술은 `backend/scripts/` 의 검증 스크립트 22개를 하드
크래시시켰다 — 그것들은 `_build_pit_pool`(→ `krx_index.index_members` → 원장 조회)을
`async def main()` 안에서 직접 부르는데, main 브랜치에서는 순수 블로킹 HTTP 라 돌던
코드다. 특히 §11 이 완료 판정 기준으로 지목한 `validate_id23_crash_2026.py` 가 여기
포함돼, 문서가 약속한 검증 절차를 코드가 막는 상태가 됐다. 가드는 원래 "어차피 터질
`asyncio.run` 의 예외를 진입점에서 명확히 하는" 역할뿐이었으므로(가드가 없어도 크래시는
났다), 실제로 실행 가능하게 만드는 편이 호출자 22곳을 고치는 것보다 낫고 잠복 재발도
막는다. `local_store_engine` 이 NullPool 이라 워커 스레드가 연 커넥션이 그 스레드의
루프에 묶인 채 풀에 남지 않는다는 점이 이 완화의 안전 근거다. 서버 코드(FastAPI
라우트·engine 데몬)가 이 폴백을 타는 것은 여전히 잘못이므로 폴백 진입 시 경고를
남긴다(프로세스당 1회).

## 9. 기존 프로세스 내 캐시

`_FUND_CACHE`(LRU 64)·`krx_index._MEMBERS_CACHE`/`_MKTCAP_CACHE`·
`opendart._ACCOUNTS_CACHE` 는 **1차 핫캐시로 유지**한다. DB 왕복도 비용이고, 이들은
이미 "실패는 캐시하지 않는다" 규칙을 지키고 있다. 뒤에 DB 가 2차로 붙는 구조다.

## 10. 테스트

컨테이너 안에서 `docker compose exec -T web pytest`. **실제 KRX/DART 호출 금지** —
`tests/conftest.py` 가 `KRX_ID`/`KRX_PW` 를 비우는 이유가 이것이다(과거 계정 ID 가
테스트 출력에 유출된 사고).

`fetch_remote` 를 스텁해 4상태를 각각 검증한다.

1. 미적재 → 외부 1회 호출 후 로컬에 저장되고 원장에 기록된다
2. 적재됨(`final=True`) → 외부 호출 0회, 로컬값 반환
3. 빈 결과(0행) → 호출자가 `is_final=True`를 넘겨도 `final=False`로 내려 다음 호출에서
   재조회된다(I3, 2026-08-08 통합 리뷰 — §5 개정 참고). "0행이 매 호출 재조회됨"이
   의도한 동작이다. 예외는 DART(OpenDART status 013, `cached_frame`을 거치지 않고
   `dart_store`가 자체 확정)뿐이다
4. 외부 실패 → `DataSourceError` 전파. 빈 프레임이 아니다
5. `final=False`(당일 / 미확정 DART) → 다음 호출에서 재조회된다
6. 부분 실패(일부 시장만 성공) → 원장에 기록되지 않아 다음 호출에서 보완된다

## 11. 검증 조건

이 작업이 끝났다는 판단 기준은 **§47 검증(id=23 폭락장 4-arm)을 다시 돌렸을 때 arm
간 수치가 갈리고 레짐 오버레이가 발화하는 것**이다. 현재 pykrx 로그인이 차단된
상태이므로, 최초 적재는 차단 해제 이후에 이뤄져야 한다. 차단 중에는 §5 계약에 따라
`DataSourceError` 가 올라와 백테스트가 **실패로 멈춘다** — 이것이 의도한 동작이며,
조용히 빈 값으로 완주하던 이전 동작보다 낫다.
