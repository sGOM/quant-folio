# OpenDART 재무데이터 연동 — 배선 완료(퀄리티 팩터)

> 상태: **키 주입·배선 완료.** OpenDART 재무데이터(ROE·부채비율·FCF)를 퀄리티
> 팩터로 리밸런싱 점수 엔진에 연결했고, 우량가치 전략(전략 id=17)을 등록했다.
> 배경: financial-expert·backend-fastapi 조사 결과, "우량가치·실적상향·소형주
> 턴어라운드" 전략의 핵심 팩터(ROE·F-Score·FCF·부채비율·영업이익 시계열)가
> 현재 데이터 계층(PER/PBR/DIV 뿐)에 없어 구현 불가했던 것을 OpenDART 로 해금.

## 배선 완료(현 상태)

- `opendart.derive_metrics()` — account_id(IFRS 표준 태그) 기반 파생: ROE·부채비율·
  영업이익·순이익·FCF·ROA. 연결(CFS) 우선·개별(OFS) 폴백, 계정명 변형/귀속분 배제.
  실측 검증(삼성·현대차·NAVER·SK하이닉스 등) 및 단위테스트 `tests/test_opendart.py`.
- `opendart.metrics_by_symbol(codes, as_of)` — PIT 공시지연(`announcement_lagged_year`)
  반영해 유니버스 종목의 재무지표를 반환. corp_code 매핑·연간 재무제표를 프로세스
  캐시(일 20,000건 요율·종목당 개별호출 대비).
- 퀄리티 팩터: `metrics._compute_stock_scores` 에 `quality` 카테고리 추가
  (ROE↑·부채비율↓·FCF흑자). `schemas.strategy.FactorWeights.quality`(기본 0.0,
  합=1.0). 백테스트(`backtests._fundamentals_provider`)·실거래
  (`metrics.compute_universe_scores`) 양쪽에 배선. 키 부재 시 자동 중립(무영향).
- 우량가치 전략 등록(전략 id=17): quality 0.35·value 0.35·momentum 0.2·lowvol 0.1,
  KOSPI MA200 레짐 필터, 대형·배당·흑자주 24종(우선주·무실적주 제외).

## 추가 배선(성장 팩터·전략)

- 성장(growth) 팩터: `opendart.metrics_by_symbol` 이 당해·전년 재무로 영업이익/순이익
  YoY 성장률(`op_growth`/`net_growth`, 전년 ≤0 이면 None)과 흑자전환(`turnaround`)을
  파생. `_compute_stock_scores` 에 growth 카테고리(z(op_growth)+z(net_growth)+z(turnaround)),
  `FactorWeights.growth`(기본 0.0) 추가. 백테스트·실거래 provider 양쪽 배선.
- 프론트: `StrategyForm` 에 선정 방식 "멀티팩터 종합점수(score)" + 5팩터 가중치 슬라이더
  (모멘텀/밸류/저변동/퀄리티/성장) + 비중 방식(동일/점수순위) UI 추가. 합=1.0 배지 검증.
- 등록 전략: id=17 우량가치, id=18 실적상향(growth 0.4+momentum 0.35), id=19 턴어라운드
  틸트(growth 0.5+quality 0.25).

## 추가 배선(F-Score·분기 PIT·턴어라운드 스크리너)

- **F-Score(수정 Piotroski 8점)**: `opendart.piotroski_f_score(cur, prev)` — 당해·전년
  재무로 수익성(ROA·CFO·ΔROA·발생액)·레버리지/유동성(Δ레버리지·Δ유동비율)·운영효율
  (Δ매출총이익률·Δ자산회전율) 8항목. 표준 9점 중 신주발행(외부 주식수 필요)은 제외.
  계산가능 항목<5면 None. `derive_metrics` 가 F-Score 원자료(cfo·유동자산/부채·매출·
  매출총이익)를 함께 반환. quality 카테고리에 f_score z-score 편입.
- **분기 PIT 세분**: `opendart.latest_report_period(as_of)` — 1Q 5/16·반기 8/16·3Q
  11/16·사업보고서 4/1 마감으로 최신 (연도, 보고서코드) 반환(팩터는 연간 비교라
  announcement_lagged_year 유지, 스크리너 freshness·향후 TTM 용).
- **소형주 턴어라운드 하드 스크리너**: `services/screener.py::screen_turnaround` +
  `GET /api/screener/turnaround` + 프론트 `/screener` 페이지. 전 시장 스캔 → 시총 하위
  20% → 거래대금 급증(5일/60일)·유동성 → OpenDART 하드 필터(부채비율 ≤ 한도·최근 3년
  만성적자 제외) → 흑자전환·순이익YoY·F-Score·수급 종합점수 정렬. 라이브 검증 통과
  (예: 베셀 F6·흑자전환·부채41%). 키 없으면 재무필터 생략(수급 기준만).

## 아직 남은 확장

- 이익추정치 **리비전**(애널리스트 컨센서스): 무료 API 없음 → 발표 실적 YoY 서프라이즈로 근사.
- 분기 **TTM**(트레일링 4분기) 지표로 팩터 신선도 향상(현재 연간 기준).
- 상폐 포함 **Point-in-Time 유니버스**(생존편향): KIND 스크래핑 별도 과제.

## 왜 필요한가 (부족 데이터 → OpenDART로 해금)

| 데이터 | 용도 전략 | OpenDART 원천 |
|--------|-----------|----------------|
| ROE | 우량가치 | 손익계산서 순이익 ÷ 재무상태표 자본총계 |
| 부채비율(D/E) | 소형주 안전필터 | 부채총계 ÷ 자본총계 |
| 영업이익·순이익(분기 시계열) | 실적상향·턴어라운드 | 손익계산서(reprt_code별) |
| FCF/영업현금흐름 | 우량가치 | 현금흐름표 − CAPEX |
| F-Score(9항목) | 우량가치 | 다년치 재무제표 조립 |
| 자사주 매입/소각 | 밸류업 | 지분·자기주식 공시 |

> KIS 오픈API로도 ROE·부채비율·성장성비율·EPS/BPS·시총은 얻을 수 있으나 **이력이
> 얕고 벌크 조회가 없어** 과거 깊은 백테스트엔 부족. **깊은 히스토리 + 공시일 PIT +
> 현금흐름표(FCF/F-Score)는 OpenDART가 정석.** (라이브 스크리닝은 KIS, 백테스트는
> OpenDART 로 역할 분담 권장.)

## 이미 준비된 것 (이 커밋)

- `app/core/config.py`: `OPENDART_API_KEY`(시크릿 파일 필드) + `OPENDART_BASE_URL` +
  `settings.has_opendart` 프로퍼티.
- `secrets/opendart_api_key.txt`(빈 파일) + docker-compose secret 배선 +
  `.env.example`(`OPENDART_API_KEY_FILE`) + `secrets/README.md`.
- `app/services/data/opendart.py`: 클라이언트 스캐폴딩.
  - `is_enabled()` — 키 없으면 False(전 조회 비활성).
  - `_get()` — 공통 호출부(status "000"만 성공, "013" 무자료 None, 실패 None).
  - `corp_code_map()` — 종목코드→corp_code(모든 재무 API 선행 필요). **동작 구현됨.**
  - `single_company_accounts()` — 단일회사 전체 재무제표 원계정. **동작 구현됨.**
  - `derive_metrics()` — ROE·부채비율·영업이익·FCF 파생 **⚠️ TODO(미구현, None 반환).**
  - `announcement_lagged_year()` — 룩어헤드 방지용 공시지연 규칙(연간 기준).

## 승인 후 배선 순서(To-Do)

1. **키 주입 확인**: 발급키를 `secrets/opendart_api_key.txt`에 넣고
   `docker compose exec -T web python -c "from app.services.data import opendart as o; print(o.is_enabled(), len(o.corp_code_map() or {}))"` 로 corp_code 매핑 로드 검증.
2. **`derive_metrics()` 구현** — 가장 큰 작업. `account_nm` 표준화가 관건(회사/업종/
   작성기준별 계정명 상이). 표준 계정 매핑 테이블을 만들고, 연결(CFS) 우선·개별(OFS)
   폴백. 우선 ROE·부채비율·영업이익·순이익부터, 그다음 FCF, 마지막 F-Score(다년치).
3. **캐시 계층** — corp_code 매핑(하루 1회)·재무제표(연·분기 단위)를 DB 또는 Redis에
   캐시. 일 20,000건 요율·종목당 개별 호출이라 벌크 백테스트 시 필수.
4. **PIT/룩어헤드** — `announcement_lagged_year`(+분기별 세분: 1Q 5월중순 / 2Q 8월중순
   / 3Q 11월중순 / 4Q 이듬해 3월말)를 백테스트 fundamentals_provider에 반영해, 공시일
   이전 시점엔 해당 실적을 쓰지 않도록 강제.
5. **팩터 배선** — `app/services/metrics/factors.py::_compute_stock_scores` / `metrics/fetch.py::_fetch_fundamentals`
   와 `backtests.py::_fundamentals_provider`에 OpenDART 파생지표를 합류. 새 팩터군(quality)
   또는 기존 value군 확장. **factor_weights 스키마 확장 필요 시** `schemas/strategy.py`.
6. **전략 정의** — 우량가치(저PBR·저PER + 고ROE·저부채) / 실적상향(영업이익 YoY 서프라이즈
   + 가격모멘텀) 전략 config 등록. 소형주 턴어라운드는 시총(KIS/KRX)·상폐필터까지 필요.
7. **테스트** — 파생 계산 단위테스트(대표 종목 수치 검증), 공시지연 경계 테스트.

## 아직 OpenDART로도 안 되는 것

- **애널리스트 컨센서스(이익추정치 리비전)** — 무료 공식 API 없음. OpenDART 실적
  서프라이즈(발표 실적 YoY)로 근사 권장.
- **상폐 포함 Point-in-Time 유니버스(생존편향)** — corp_code.xml에 상폐일 없음.
  KIND 공시 스크래핑 필요(별도 과제). 초기엔 생존편향 존재를 리포트에 명시.

## 의존성

새 파이썬 패키지 불필요(httpx + 표준 라이브러리). `OpenDartReader`/`dart-fss`를 쓰고
싶으면 `requirements.txt`에 추가할 수 있으나, 현재 얇은 httpx 클라이언트로 충분.
