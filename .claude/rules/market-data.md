---
paths:
  - "backend/app/services/data/**"
  - "backend/app/services/metrics/**"
  - "backend/app/services/screener.py"
  - "backend/app/services/recommend.py"
  - "backend/app/services/symbols.py"
---

# 시장 데이터 — 외부 소스와 조회 정책

`app/services/data/` + `app/services/metrics/`

## 소스별 성격

| 소스 | 모듈 | 인증 | 안정성 | 용도 |
|---|---|---|---|---|
| KRX MDC (pykrx) | `krx_index.py` | **PIT 지수구성 조회는 KRX 로그인 필요**(`KRX_ID/PW` → `app.core.config`) | 보통 | 지수 구성종목·업종·상장목록 |
| pykrx 시세 | `metrics/fetch.py` | 불필요 | 보통 | 시총·펀더멘털·등락률·OHLCV·투자자별 순매수 |
| OpenDART | `opendart.py` | API 키 | 양호 | 재무제표(ROE·FCF·부채비율·F-Score·성장) |
| KIS 종목마스터 | `kis_master.py` | **불필요**(공개 CDN zip) | 양호 | 관리종목·정리매매·거래정지·액면가·업종 세분류 |
| 금투협 FreeSIS | `kofia.py` | 불필요 | 양호 | 증시자금(미수금·반대매매) |
| KIS REST/WS | `services/kis/` | 앱키/토큰 | — | 실시간 시세·주문(→ [trading-engine.md](trading-engine.md)) |
| FinanceDataReader | — | — | **이 환경에서 불안정** | 폴백 용도로만 |

## 조회 계약 — 4상태를 뭉개지 않는다

`app/services/data/store/frame.py::cached_frame` 이 **로컬 우선 조회의 단일 진입점**이다.

| 상태 | 의미 | 동작 |
|---|---|---|
| 확정 적재됨 | `external_fetches.final=True` | 로컬 반환 |
| 미적재 | 원장에 기록 없음 | 원격 1회 → 로컬 기록 |
| 조회 실패 | 소스 장애 | `DataSourceError` raise |
| 소스 미설정 | 키/계정 없음 | 여기 오기 전에 통과 |

"데이터 없음"과 "미적재"를 뭉개면 백테스트가 **빈 패널 위에서 '성공'한다** — 실제 사고 이력.

## 오류 분류 (`data/errors.py`)

전송·스키마 실패는 원인별 `DataSourceError` 하위 타입으로 raise 한다.

- `SourceUnavailableError` — 네트워크·5xx·타임아웃 (재시도 가치 있음)
- `SourceSchemaError` — 포맷이 바뀐 신호 (재시도 무의미, 사람이 봐야 함)
- `classify_httpx(source, exc)` 로 분류하고 `note_failure(exc)` 로 기록한다.

**부분 실패 보장**: 시장별·종목별로 도는 배치는 하나가 실패해도 나머지를 저장한다.
전부 실패했을 때만 대표 예외를 올린다. 쿨다운 키를 시장 간에 공유하면 한 시장 실패가
다른 시장 시도를 막으므로 금지.

## 팩터 계산 (`metrics/factors.py`)

- 밸류(PER/PBR/DIV)·모멘텀(1M/3M/6M/12-1)·저변동성(vol_ann/mdd_252)·퀄리티·성장
- 윈저화 후 z-score(`_winsorize_zscore`) → 가중합
- 중립화 옵션: 사이즈(적용됨, §P1-3), 섹터(A/B 혼재로 **미적용** 유지, §20)
- **기각된 팩터도 배선은 보존한다**(opt-in 능력): flow(수급, §41)·잔차 모멘텀(§42)·
  PEAD(§43)·변동성 수확 게이트. 전략 등록만 기각이지 코드는 남는다.

## 하위 도구

| 모듈 | 역할 |
|---|---|
| `metrics/stocks.py` | 종목별 지표 스냅샷 (`/api/metrics/stocks`) |
| `metrics/sectors.py` | 섹터(업종) 지표·상대강도 |
| `metrics/panic.py` | 패닉셀 지표(브레드스 S1~S9) — 임계값은 잠정 캘리브레이션 |
| `metrics/vulnerability.py` | 취약도 게이지 |
| `services/screener.py` | 소형주 턴어라운드 스크리너 |
| `services/recommend.py` | KOSPI200 스코어링 추천 |
| `services/symbols.py` | 종목명 해석·검색 |
