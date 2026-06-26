# PRD: QuantFolio (가칭)

> 국내 주식(KRX) 대상 퀀트 전략 백테스팅 및 실시간 자동매매 웹 플랫폼

| 항목 | 내용 |
|------|------|
| 문서 버전 | v0.1 |
| 작성일 | 2026-06-21 |
| 대상 시장 | 국내 주식 (KRX) |
| 주력 언어 | Python |

---

## 1. 프로젝트 개요

- **프로젝트명**: QuantFolio — 퀀트 전략을 설계·검증하고, 검증된 전략을 실시간으로 자동 매매하는 개인 투자자용 웹 플랫폼

- **목적**:
  사용자가 코드 작성 없이(또는 최소한의 설정으로) 퀀트 매매 전략을 구성하고, 과거 데이터로 백테스팅한 뒤, 동일한 전략을 한국투자증권 API를 통해 실시간 자동매매로 운용할 수 있게 한다. 매매 엔진은 웹 서버와 분리되어 24시간 안정적으로 동작하며, 사용자는 웹 대시보드로 잔고·체결·손익을 실시간 모니터링한다.

- **스택을 선택한 이유**:
  1. **Python (FastAPI) 백엔드**: 퀀트 분석의 핵심 생태계(pandas, numpy, vectorbt)가 Python에 집중되어 있고, FastAPI는 비동기(WebSocket) 지원과 자동 API 문서화로 실시간 시세·주문 처리에 적합하다. 백테스팅 연구 코드와 운영 코드를 동일 언어로 공유할 수 있다.
  2. **Next.js (React) 프론트엔드**: SSR 기반으로 대시보드 초기 로딩이 빠르고, TypeScript 타입 안정성과 풍부한 금융 차트 생태계(TradingView)를 활용해 실시간 모니터링 UI를 효율적으로 구축할 수 있다.

- **Open API 선택 이유**:
  1. **한국투자증권 KIS Developers API**: 국내 증권사 중 유일하게 OS 독립적인 공식 REST + WebSocket API를 제공한다. 키움 구 OpenAPI+(32bit OCX, Windows 종속)와 달리 리눅스/Docker/클라우드 서버에서 24시간 자동매매를 운영할 수 있고, 실거래와 동일한 인터페이스의 **모의투자 도메인**을 제공해 안전한 검증이 가능하다.

- **아키텍처 선택 이유**:
  **웹 서버와 매매 엔진을 물리적으로 분리**한 이벤트 기반 아키텍처를 채택한다. 자동매매 로직을 HTTP 요청 핸들러에 두면 웹 배포·재시작 시 매매가 중단되어 손실로 직결된다. 매매 엔진을 독립 프로세스로 분리하고 Redis(pub/sub·큐·분산 락)로 통신하면, 웹 서버 장애와 무관하게 매매가 지속되고 각 컴포넌트를 독립적으로 재시작·확장·모니터링할 수 있다.

---

## 2. 주요 기능

1. **전략 빌더 & 백테스팅**: 사용자가 매수/매도 조건(기술적 지표, 펀더멘털 필터, 리밸런싱 주기)을 구성하고, 과거 KRX 데이터로 수익률·MDD·샤프지수 등 성과를 검증한다.
2. **실시간 자동매매 엔진**: 검증된 전략을 KIS WebSocket 시세에 연결해 신호를 생성하고, 리스크 관리 규칙(손절·최대 포지션·일일 한도)을 거쳐 자동 주문을 실행한다.
3. **실시간 모니터링 대시보드**: 보유 잔고, 미체결/체결 주문, 실현·평가 손익, 전략별 성과를 WebSocket으로 실시간 갱신해 보여준다.

---

## 3. 기술 스택

- **Frontend**: Next.js (React), TypeScript, TanStack Query
- **Backend**: Python, FastAPI, Celery (+ asyncio 매매 엔진)
- **Styling**: Tailwind CSS, shadcn/ui, TradingView Lightweight Charts
- **Icons**: Lucide React, Heroicons, React Icons
- **Database / Infra**: PostgreSQL + TimescaleDB, Redis, Docker Compose
- **데이터 소스**: 한국투자증권 KIS API, pykrx, FinanceDataReader

---

## 4. 데이터베이스 구조

- **users**: `id, email, password_hash, kis_app_key(enc), kis_app_secret(enc), kis_account_no, created_at` — 사용자 계정 및 암호화된 증권사 API 자격증명
- **strategies**: `id, user_id, name, config(jsonb), status(draft/backtested/live), created_at, updated_at` — 전략 정의(진입·청산 조건, 리밸런싱 규칙)를 JSON으로 저장
- **backtests**: `id, strategy_id, period_start, period_end, total_return, mdd, sharpe, result(jsonb), created_at` — 백테스팅 실행 결과 및 성과 지표
- **orders**: `id, user_id, strategy_id, symbol, side(buy/sell), qty, price, order_type, kis_order_id, status, idempotency_key, created_at` — 주문 요청 및 체결 상태(멱등성 키로 중복 주문 방지)
- **executions**: `id, order_id, filled_qty, filled_price, fee, executed_at` — 실제 체결 내역(감사 로그)
- **price_ticks** (TimescaleDB hypertable): `time, symbol, open, high, low, close, volume` — 시계열 시세 데이터(백테스팅·차트용)
- **positions**: `id, user_id, symbol, qty, avg_price, updated_at` — 현재 보유 포지션 스냅샷
- **risk_limits**: `id, user_id, strategy_id, max_position_size, daily_loss_limit, stop_loss_pct` — 전략별 리스크 관리 한도

---

## 5. 화면 구성

- **대시보드 (홈)**: 총 자산·일일 손익·운용 중인 전략 요약, 실시간 잔고/포지션 카드, 최근 체결 내역
- **전략 목록**: 사용자가 만든 전략 카드 리스트(상태 배지: 초안/백테스트 완료/운용 중), 신규 생성 버튼
- **전략 빌더**: 진입·청산 조건, 종목 유니버스, 리밸런싱 주기, 리스크 한도 설정 폼
- **백테스트 결과**: 수익률 곡선 차트, 성과 지표 테이블(수익률/MDD/샤프/승률), 매매 시점 마커
- **실시간 모니터링**: TradingView 차트 + 실시간 시세, 미체결/체결 주문 테이블, 전략 ON/OFF 토글
- **설정**: KIS API 키 등록(모의/실전 전환), 알림 설정, 계정 관리

---

## 6. MVP 범위

> "전략 1개를 모의투자로 자동매매하고 모니터링한다"를 최소 검증 목표로 한다.

**포함**
- 이메일 기반 회원가입/로그인, KIS 모의투자 API 키 등록
- 단순 전략 1종(예: 이동평균 골든크로스) 설정 폼
- 단일 종목/소수 종목 대상 백테스팅(기본 성과 지표만)
- KIS **모의투자** WebSocket 시세 구독 → 신호 생성 → 자동 주문(매수/매도)
- 기본 리스크 관리(손절 %, 최대 포지션)
- 실시간 대시보드(잔고·포지션·체결 내역)
- 모든 주문/체결 감사 로그 기록

**제외 (추후)**
- 실전 투자 연동, 다중 전략 동시 운용, 복잡한 전략 조합, 다중 사용자 확장, 알림(이메일/푸시), 종목 스크리너, 펀더멘털 데이터 필터

---

## 7. 구현 단계

1. **기반 구축**: 프로젝트 뼈대(web/engine/worker 분리), Docker Compose(FastAPI · PostgreSQL+TimescaleDB · Redis · Next.js), 인증, KIS 토큰 발급·모의투자 시세 조회 연동 검증
2. **백테스팅 코어**: pykrx/FinanceDataReader로 과거 데이터 적재, 단순 전략 엔진(vectorbt) 구현, 백테스트 실행 API + 결과 화면
3. **매매 엔진 (핵심)**: 독립 프로세스로 KIS WebSocket 시세 구독 → 신호 → 리스크 체크 → 주문 실행, Redis 분산 락으로 멱등성 보장, 모든 주문/체결 DB 기록
4. **실시간 대시보드**: FastAPI WebSocket 푸시 + 프론트 TanStack Query/WS로 잔고·포지션·체결 실시간 표시, 전략 ON/OFF 제어
5. **안정화 & 검증**: 장 운영시간/휴장일 처리, 네트워크 재연결·상태복구, 모의투자 환경에서 종단 검증, 감사 로그·모니터링 보강
