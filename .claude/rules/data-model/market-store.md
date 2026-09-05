---
paths:
  - "backend/app/models/store.py"
  - "backend/app/services/data/store/**"
---

# 시장데이터 저장소 테이블 (`app/models/store.py`)

**확정된 과거 데이터를 Postgres 에 영구 저장**해 로컬 우선으로 읽는다. 외부(pykrx·DART)는
느리고 불안정해서, 한 번 확정된 값은 다시 묻지 않는 것이 원칙이다.

조회 계약은 `app/services/data/store/frame.py` 하나로 모인다 → [../market-data.md](../market-data.md)

## 테이블과 복합 PK

| 테이블 | PK | 내용 |
|---|---|---|
| `stock_daily_snapshots` | (trade_date, symbol) | 종목 일별 확정값 — 시총·종가·거래대금·PER/PBR/DIV 등. **hypertable**(파티션 키 `trade_date`) |
| `stock_period_stats` | (start_date, end_date, investors, symbol) | 기간 통계 — 등락률·투자자별 순매수 |
| `index_ohlcv` | (index_code, trade_date) | 지수 일별 OHLCV |
| `index_ohlcv_coverage` | (index_code, covered_from) | **구간 커버리지 원장** — 어디까지 채웠는지 |
| `index_constituents` | (index_code, base_date, symbol) | 시점별 지수 구성종목(PIT, 생존편향 제거용) |
| `dart_financials` | (corp_code, bsns_year, reprt_code, fs_div) | OpenDART 재무제표 |
| `external_fetches` | (source, cache_key) | **적재 원장** — 위 전부의 "확정 여부(final)·행수" 기록 |
| `sector_map_snapshots` | — | 업종분류 분기 스냅샷(PIT 부분 해소) |
| `kis_stock_master_snapshots` | (symbol, trade_date) unique | KIS 종목마스터 — 관리종목·정리매매·액면가·업종 |

## `external_fetches` 가 핵심이다

각 데이터 테이블은 "값"만 담고, **그 값이 확정인지 아닌지는 `external_fetches` 가 안다.**

| 원장 상태 | 동작 |
|---|---|
| `final=True` 기록 있음 | 로컬에서 읽어 반환 |
| 기록 없음 / `final=False` | 원격 1회 조회 → 로컬 기록 + 원장 갱신 |
| 원격 조회 실패 | `DataSourceError` 를 그대로 raise (값으로 삼키지 않는다) |

**빈 결과는 소스가 "없다"고 명시적으로 선언한 경우에만 확정으로 굳힌다.** 그 외에는
`row_count==0` 이면 `final=False` 로 내려 다음에 재조회한다 — 스키마 변경으로 빈 응답이
온 것을 확정으로 굳히면 프로세스 재시작으로도 회복되지 않고 영구히 0행이 된다(실제 사고
이력: §44-1·§47). 유일한 예외는 OpenDART status 013(무자료 명시)이고, 그 경로는
`cached_frame` 이 아니라 `dart_store` 가 따로 확정한다.

## 강제 재적재

값만 지우면 원장이 "확정"으로 남아 다시 안 받아온다. **둘 다 지운다.**

```
각 리포지토리의 delete_* 호출  →  해당 external_fetches 행 삭제
```

## 주의

- **저장소는 빈 상태에서 시작할 수 있어야 한다.** 시드 데이터를 전제하면 안 된다.
- **종목명의 신뢰 소스는 `krx_index.all_listed_stocks`**(KRX MDC finder, 날짜 비의존).
  FDR/pykrx 는 이 환경에서 불안정하다. 외부 조회 실패 시 seed-only 캐시로 굳히지 말 것(자가복구 불가).
