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

## 추가 배선(분기 TTM — 트레일링 4분기)

- **`opendart.ttm_metrics(corp_code, bsns_year, reprt_code)`**: `latest_report_period`
  가 반환한 분기(1Q/반기/3Q)에 대해 "전년 연간 − 전년 동기 누적 + 당해 동기 누적"
  텔레스코핑으로 트레일링 4분기 flow(매출·영업이익·순이익·FCF·CFO·매출총이익)를
  합성한다. OpenDART 분기 보고서 금액이 사업연도 초부터의 **누적치**라는 점을
  이용한 표준 TTM 계산법이다. 재무상태표(자산·부채·자본 등 저량 항목)는 텔레스코핑
  대상이 아니라 당해 분기 시점값을 그대로 쓰고, ROE/부채비율/ROA 는 텔레스코핑된
  순이익과 최신 시점 자본/자산으로 재계산한다(비율 자체를 합산하지 않음).
  reprt_code 가 사업보고서(연간)면 그 자체가 이미 TTM. 당해 분기 원자료가
  없으면(상장 이력 짧음 등) 가장 최근 확정 연간으로 안전 폴백한다.
- **`opendart.metrics_by_symbol(codes, as_of, use_ttm=False)`**: 기본값은 기존
  연간(사업보고서 only, `announcement_lagged_year`) 경로 그대로다 — id=23/24 등
  기존 등록 전략의 백테스트 재현성을 깨지 않기 위함. `use_ttm=True` 로 호출하면
  `latest_report_period(as_of)` 로 PIT 안전한 최신 분기/연간을 정하고 `ttm_metrics`
  로 조회한다. F-Score·YoY 성장·흑자전환·만성적자(3년) 판정도 전년·전전년
  **동일 reprt_code 기준 TTM** 으로 비교해 계절성을 제거한다. 소비측
  (`factors.py`/`screener.py`/`recommend.py`)은 출력 스키마(필드명)가 동일해
  변경 없이 그대로 수용하며, TTM 전환은 신규 전략 config에서 명시적으로 옵트인
  해야 한다.
- PIT 안전성: TTM 이 참조하는 (당해분기, 전년동기, 전년연간) 은 모두
  `latest_report_period` 가 이미 공시됐다고 판정한 시점이거나 그보다 과거뿐이라
  룩어헤드가 없다. 단위테스트(`tests/test_opendart.py`)에 텔레스코핑 합산·저량
  보존·연간 폴백·PIT 회귀(전년/전전년이 미래 분기를 절대 조회하지 않음) 검증 포함.
- **A/B 판정(2026-07-19, `scripts/validate_ttm_ab.py`)**: id=23·24 를 PIT KOSPI200
  에서 연간 vs TTM 재무로 반기 2-fold 워크포워드 비교(판정 alpha/Sharpe). 두 전략
  모두 H1(횡보·하락장) 은 TTM 우위, H2(강세장) 는 연간 우위로 **혼재 — 승격 기각,
  옵트인 유지**. id=23 FULL 은 연간이 우위(Sharpe 1.04/alpha +19.3% vs 0.98/+17.8%),
  id=24 FULL 은 TTM 이 근소 우위(0.88/+12.0% vs 0.86/+11.7%, MDD −16.8% vs −18.5%)
  였으나 양 반기 일관 우위 기준 미달. 회전율도 TTM 이 +5~7%p 높아 비용 역풍.
  가설(분기 신선도 → 성과 개선)은 방어 국면에서만 부분 성립한다.

## 아직 남은 확장

- 이익추정치 **리비전**(애널리스트 컨센서스): 무료 API 없음 → 발표 실적 YoY 서프라이즈로 근사.

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

## 인프라 배선(참고)

- `app/core/config.py`: `OPENDART_API_KEY`(시크릿 파일 필드) + `OPENDART_BASE_URL` +
  `settings.has_opendart` 프로퍼티.
- `secrets/opendart_api_key.txt` + docker-compose secret 배선 +
  `.env.example`(`OPENDART_API_KEY_FILE`) + `secrets/README.md`.
- `app/services/data/opendart.py`: `is_enabled()`(키 없으면 전 조회 비활성) ·
  `corp_code_map()` · `single_company_accounts()` · `derive_metrics()` ·
  `announcement_lagged_year()`(룩어헤드 방지 공시지연 규칙) ·
  `ttm_metrics()`/`metrics_by_symbol(..., use_ttm=True)`(분기 TTM, 위 "분기 TTM" 절
  참고) — 모두 구현 완료.
  키 주입 검증: `docker compose exec -T web python -c "from app.services.data import opendart as o; print(o.is_enabled(), len(o.corp_code_map() or {}))"`

## 아직 OpenDART로도 안 되는 것

- **애널리스트 컨센서스(이익추정치 리비전)** — 무료 공식 API 없음. OpenDART 실적
  서프라이즈(발표 실적 YoY)로 근사 권장.
- **corp_code.xml 자체로는 PIT 유니버스가 안 된다** — 상폐일이 없어 OpenDART 단독으로는
  생존편향을 제거할 수 없다. 다만 **지수 유니버스 기준으로는** 이 문제가 OpenDART가
  아니라 **KRX MDC(`krx_index`)의 시점별 지수구성 조회**로 이미 해소돼 있다(신규 전략
  검증 표준 절차, 루트 `CLAUDE.md` "전략 id 관리" 참고) — 손질된(생존편향 있는) 풀은
  성과가 붕괴함이 실측으로 확인됐다. 지수 밖 상폐 종목까지 포함하는 전 시장 PIT
  유니버스는 여전히 미해결.

## 의존성

새 파이썬 패키지 불필요(httpx + 표준 라이브러리). `OpenDartReader`/`dart-fss`를 쓰고
싶으면 `requirements.txt`에 추가할 수 있으나, 현재 얇은 httpx 클라이언트로 충분.
