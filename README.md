# QuantFolio

> 국내 주식(KRX) 대상 퀀트 전략 **백테스팅** 및 **실시간 자동매매** 웹 플랫폼

개인 투자자가 코드 작성 없이(또는 최소한의 설정으로) 퀀트 매매 전략을 구성하고, 과거 데이터로 백테스팅한 뒤, 동일한 전략을 한국투자증권 API를 통해 실시간 자동매매로 운용할 수 있게 합니다. 매매 엔진은 웹 서버와 분리되어 24시간 안정적으로 동작하며, 사용자는 웹 대시보드로 잔고·체결·손익을 실시간 모니터링합니다.

자세한 제품 정의는 [`docs/PRD.md`](docs/PRD.md)를 참고하세요.

---

## 목적

1. **전략 빌더 & 백테스팅** — 매수/매도 조건(기술적 지표, 리밸런싱 주기)을 구성하고 과거 KRX 데이터로 수익률·MDD·샤프지수 등 성과를 검증합니다.
2. **실시간 자동매매 엔진** — 검증된 전략을 KIS WebSocket 시세에 연결해 신호를 생성하고, 리스크 관리 규칙(손절·최대 포지션·일일 손실 한도)을 거쳐 자동 주문을 실행합니다.
3. **실시간 모니터링 대시보드** — 보유 잔고, 미체결/체결 주문, 실현·평가 손익, 전략별 성과를 WebSocket으로 실시간 갱신해 보여줍니다.

> MVP 목표: **"전략 1개를 모의투자로 자동매매하고 모니터링한다"**

---

## 기술 스택

| 구분 | 기술 |
|------|------|
| **Frontend** | Next.js 15 (React 19, App Router), TypeScript, TanStack Query, Tailwind CSS, Lucide React |
| **Backend (web)** | Python, FastAPI, Pydantic v2, SQLAlchemy 2 (async), 서버측 세션(Redis), bcrypt, cryptography(Fernet) |
| **매매 엔진 / 워커** | asyncio 이벤트 루프 엔진, Celery, websockets |
| **백테스팅 / 데이터** | pandas, numpy, vectorbt, pykrx, FinanceDataReader |
| **Database / Infra** | PostgreSQL + TimescaleDB, Redis, Docker Compose |

---

## Open API — 한국투자증권 KIS Developers

국내 증권사 중 유일하게 OS 독립적인 공식 **REST + WebSocket** API를 제공해, 리눅스/Docker/클라우드에서 24시간 자동매매를 운영할 수 있고 실거래와 동일한 인터페이스의 **모의투자 도메인**으로 안전하게 검증할 수 있습니다. 본 프로젝트는 기본값으로 **모의투자(vts)**를 사용합니다.

| 항목 | 링크 |
|------|------|
| KIS Developers 포털 | https://apiportal.koreainvestment.com |
| API 문서(국내주식 시세·주문·잔고) | https://apiportal.koreainvestment.com/apiservice |
| GitHub 공식 예제 | https://github.com/koreainvestment/open-trading-api |

본 프로젝트에서 사용하는 주요 KIS API:

- **OAuth 접근토큰 발급** (`/oauth2/tokenP`) — REST 호출 인증 (분당 1회 제한 → Redis 캐시·분산 락으로 single-flight)
- **실시간 시세 approval_key** (`/oauth2/Approval`) — WebSocket 인증용
- **국내주식 현재가** (`FHKST01010100`) / **실시간 체결가 WebSocket** (`H0STCNT0`)
- **현금 주문** (`order-cash`, 모의 `VTTC080*U` / 실전 `TTTC080*U`)
- **주문체결 조회** (`inquire-daily-ccld`) — 실제 체결가/수량 확인
- **주식 잔고 조회** (`inquire-balance`)

> 보조 시세 데이터 소스: [pykrx](https://github.com/sharebook-kr/pykrx), [FinanceDataReader](https://github.com/FinanceData/FinanceDataReader) (백테스트용 과거 일봉 적재)

---

## 아키텍처

웹 서버와 매매 엔진을 **물리적으로 분리**한 이벤트 기반 구조입니다. 매매 로직을 HTTP 핸들러에 두면 웹 재배포·재시작 시 매매가 끊겨 손실로 직결되므로, 엔진을 독립 프로세스로 두고 **Redis(pub/sub·큐·분산 락)**로 통신합니다.

```
                         ┌──────────────────────────────┐
 폰/브라우저  ──HTTP(S)──▶ │  proxy (Caddy, :8080)        │ 단일 외부 진입점
                         │  /  →frontend  /api,/ws→web  │
                         └───────┬───────────────┬──────┘
                                 ▼               ▼
                         ┌──────────┐      ┌───────────┐
                         │ Next.js  │      │  web      │  FastAPI (REST + WS 푸시)
                         │ frontend │      │  (8000)   │  세션 쿠키 인증
                         │ (3000)   │      └─────┬─────┘
                         └──────────┘            │ Redis pub/sub · 큐 · 락 · 세션
                                 ┌───────────────┼────────────────┐
                                 ▼               ▼                 ▼
                            ┌─────────┐    ┌─────────┐       ┌──────────┐
                            │ engine  │    │ worker  │       │  redis   │
                            │ 매매엔진 │    │ Celery  │       │          │
                            └────┬────┘    └────┬────┘       └──────────┘
                                 │              │
                                 ▼              ▼
                            ┌──────────────────────┐
                            │ PostgreSQL+TimescaleDB│
                            └──────────────────────┘
```

- **proxy** (`Caddyfile`): 리버스 프록시. 프론트와 백엔드를 **한 출처(`:8080`)**로 합치는 유일한 외부 진입점. `web`/`frontend`/`db`/`redis` 의 호스트 포트는 `127.0.0.1` 에만 바인딩되어 외부에 노출되지 않는다.
- **web** (`backend/app`): REST/WebSocket API, 인증(HttpOnly 세션 쿠키 + Redis 서버측 세션), 설정 CRUD, 실시간 푸시. **매매 로직 없음.**
- **engine** (`backend/engine`): 독립 프로세스. KIS WebSocket 시세 구독 → 신호 → 리스크 체크 → 주문 → 체결/포지션 기록.
- **worker** (`backend/worker`): Celery 배치 작업(데이터 적재, 백테스트 실행 등).
- **frontend** (`frontend`): Next.js 대시보드.

### 데이터 모델 (요약)

`users` · `strategies` · `backtests` · `orders` · `executions` · `positions` · `risk_limits` · `price_ticks`(TimescaleDB hypertable). 자세한 정의는 [`docs/PRD.md` §4](docs/PRD.md)와 `backend/app/models/models.py` 참고.

---

## 실행 방법 — 노트북을 24시간 서버로 만들기

이 앱은 **노트북에서 24시간 띄워 두고 폰·외부 기기에서 접속**하는 1인 서버
운용을 전제로 한다. 아래 **1→8단계**를 순서대로 따라 하면 된다.

**구조 한눈에:** Caddy 리버스 프록시가 프론트(Next.js)와 백엔드(FastAPI)를
**한 포트(`:8080`)로 합치고**, **Tailscale** 사설망으로 외부에서 접속한다.
외부 진입점은 `:8080` 하나뿐이며, 나머지 포트(`web`/`frontend`/`db`/`redis`)는
`127.0.0.1` 에만 바인딩되어 외부에 노출되지 않는다.

```
폰(Tailscale 앱) ──테일넷(암호화)──▶ 노트북:8080 (Caddy proxy)
                                       ├─ /        → frontend:3000
                                       └─ /api,/ws → web:8000
```

### 사전 요구사항
- **Windows 노트북 + Docker Desktop**(Docker Compose 포함)
- (외부/폰 접속용) **Tailscale 계정** — 개인 무료
- (자동매매 검증용, 선택) KIS Developers 계정 + **모의투자** App Key/Secret/계좌번호

---

### 1단계 — 보안 키 & 시크릿 파일 생성

마스터 키와 브로커 키는 평문 `.env` 가 아니라 **시크릿 파일**(`secrets/*.txt`)로
둔다(아래 [🔐 키 관리](#-키-관리-시크릿-파일) 참고). 최초 1회만:

```bash
# secrets/ 는 .gitignore 로 제외됨
openssl rand -hex 32 > secrets/secret_key.txt
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > secrets/credential_enc_key.txt
# 미사용 브로커 키는 빈 파일로(파일은 존재해야 compose 기동)
: > secrets/kis_app_key.txt;  : > secrets/kis_app_secret.txt
: > secrets/toss_app_key.txt; : > secrets/toss_app_secret.txt
```

### 2단계 — 환경변수(.env) 설정

```bash
cp .env.example .env
```

서버 운용에서 확인·조정할 주요 변수:

| 변수 | 설명 | 서버 운용 권장값 |
|------|------|------------------|
| `APP_ENV` | `dev`(누락 시크릿 임시 생성) / `prod`(누락·약한 키·비보안 쿠키면 부팅 거부) | `prod` |
| `APP_PORT` | **외부 접속 단일 진입 포트(proxy)** | `8080` |
| `NEXT_PUBLIC_API_BASE_URL` | 프론트가 호출할 API 주소. **비우면 상대경로(같은 출처)** → 폰이 어떤 주소로 들어와도 동작 | **(비움)** |
| `KIS_ENV` | `vts`(모의투자) / `prod`(실전) | `vts`(검증 전) |
| `COOKIE_SECURE` | HTTPS 에서만 쿠키 전송 | `false`→**6단계 후 `true`** |
| `COOKIE_SAMESITE` | 단일 출처 운용이면 `lax` 로 충분 | `lax` |
| `SESSION_TTL_MINUTES` | 로그인 세션 유효기간(분, 슬라이딩 갱신) | `20160`(14일) |
| `SECRET_KEY` / `CREDENTIAL_ENC_KEY` | 1단계 시크릿 파일로 주입(이 변수는 비워둠) | (파일 사용) |

> `APP_ENV=prod` 면 시크릿 누락·약한 키·`COOKIE_SECURE=false` 일 때 부팅이
> 거부된다. **HTTPS(6단계) 적용 전까지는** `APP_ENV=dev` 로 두거나
> `COOKIE_SECURE=false` 를 유지하고, HTTPS 적용 후 `prod`+`true` 로 올린다.

### 3단계 — 컨테이너 기동 + DB 마이그레이션

```bash
docker compose up -d --build

# 최초 1회 — 테이블 생성(TimescaleDB hypertable 포함)
docker compose exec web alembic upgrade head
```

모든 서비스에 `restart: unless-stopped` 가 걸려 있어 크래시·재부팅 후 자동 복구된다.

### 4단계 — 로컬에서 동작 확인

- **앱 접속(단일 진입점)**: <http://localhost:8080>
- **API 문서(Swagger)**: <http://localhost:8080/docs>
- **헬스체크**: <http://localhost:8080/health>

`/login` 에서 회원가입·로그인이 되면 정상이다.

### 5단계 — Tailscale 로 외부(폰) 접속 열기

공인 IP·포트포워딩 없이, URL 노출 없이 폰과 노트북을 사설망으로 잇는다.

1. **노트북**: <https://tailscale.com> 가입 → Windows 클라이언트 설치·로그인.
   트레이에서 이 기기의 주소를 확인한다.
   - Tailscale IP: `100.x.y.z`
   - (권장) 관리자 콘솔 → DNS → **MagicDNS** 켜면 머신명으로 접속:
     `my-laptop.<tailnet>.ts.net`
2. **폰**: App Store/Play 스토어에서 **Tailscale** 설치 → **같은 계정** 로그인 →
   VPN 연결 ON(상시 켜두면 LTE·외부에서도 자동 연결).
3. **접속**: 폰 브라우저에서
   - `http://100.x.y.z:8080` 또는
   - `http://my-laptop.<tailnet>.ts.net:8080`

> 상대경로 + 단일 출처 구성이라 **어떤 주소로 들어와도 그대로 동작**하며,
> 주소가 바뀌어도 재빌드·재설정이 필요 없다.

### 6단계 — HTTPS 적용 (권장)

평문 HTTP 라도 테일넷 트래픽은 종단간 암호화되지만, 브라우저 자물쇠·HTTPS 전용
기능·로그인 payload 암호화를 위해 Tailscale 인증서로 TLS 를 씌운다.

```bash
# 노트북에서 — :8080(프록시) 앞에 HTTPS 종단 추가
tailscale serve --bg --https=443 http://localhost:8080
```

이후 `.env` 에서 쿠키 보안을 올리고 web 을 재시작한다.

```bash
APP_ENV=prod
COOKIE_SECURE=true
```
```bash
docker compose up -d
```

이제 폰에서 `https://my-laptop.<tailnet>.ts.net` (포트 없이)로 접속한다.

### 7단계 — 부팅 시 자동 시작 (24시간)

재부팅·정전 후에도 자동으로 다시 뜨게 한다.

1. **Docker Desktop** → Settings → General → **"Start Docker Desktop when you
   sign in"** 체크. 데몬이 뜨면 `restart: unless-stopped` 에 따라 컨테이너가
   자동 기동된다.
2. (잠금 화면에서도 운용하려면) `netplwiz` 로 **자동 로그인** 설정.

### 8단계 — 절전/덮개 방지 (필수)

노트북이 잠들면 매매 엔진도 멈춘다. **제어판 → 전원 옵션**에서:

- 전원 연결 시 **절전: 안 함**
- **덮개를 닫을 때**(전원 연결): **아무 것도 안 함**
- 가능하면 상시 전원 연결 상태로 둔다.

---

### ✅ 점검 체크리스트

- [ ] `docker compose ps` — 모든 서비스 `running`/`healthy`
- [ ] 노트북 <http://localhost:8080> 로그인 OK
- [ ] 폰 Tailscale 연결 후 머신명:8080(또는 HTTPS) 로그인 OK
- [ ] `실시간` 화면 WebSocket 갱신 동작
- [ ] 노트북 재부팅 후 자동 기동 확인
- [ ] 절전/덮개 설정으로 화면 꺼져도 엔진 유지 확인

### 운영 팁

```bash
docker compose ps                 # 상태 확인
docker compose logs -f web        # 로그 실시간(web/engine/worker 등)
docker compose restart web        # 특정 서비스 재시작
git pull && docker compose up -d --build   # 코드 업데이트 후 반영
docker compose exec db pg_dump -U quant quant > backup.sql   # DB 백업
```

---

## 사용 흐름 (모의투자 종단 검증)

1. 프론트(`/login`)에서 **회원가입·로그인** — 서버측 세션이 생성되고 세션 ID 가 HttpOnly 쿠키로 발급됩니다(세션 데이터는 Redis 에 보관).
2. `설정`에서 **KIS 모의투자 App Key/Secret/계좌번호** 등록(서버에서 암호화 저장) → 대시보드에서 연동 상태 확인.
3. `전략`에서 **SMA 골든크로스 전략** 생성 → 상세에서 기간 지정 후 **백테스트 실행**(수익률·MDD·샤프·승률·자산곡선).
4. `실시간`에서 전략 **시작(ON)** → 분리된 매매 엔진이 시세 구독·신호·리스크 체크·주문을 수행.
5. 실시간 포지션·체결 이벤트가 **WebSocket으로 갱신**되고, 모든 주문은 감사 로그로 기록됩니다.

---

## 테스트

```bash
docker compose exec web pytest          # 신호·보안·장운영시간·멱등성 단위 테스트
```

---

## 구현 진행 (PRD §7)

- [x] 1. 기반 구축 — 뼈대 분리, Docker Compose, 인증, KIS 연동 검증
- [x] 2. 백테스팅 코어 — 데이터 적재, vectorbt 엔진, 백테스트 API·결과 화면
- [x] 3. 매매 엔진 — KIS WS 시세→신호→리스크→주문, Redis 분산 락 멱등성, 주문/체결 기록
- [x] 4. 실시간 대시보드 — FastAPI WS 푸시, 실시간 잔고·포지션·체결, 전략 ON/OFF
- [x] 5. 안정화 & 검증 — 장 운영시간/휴장일 처리, WS 재연결·상태복구, 감사 로그, 테스트

---

## 디렉터리 구조

```
quant/
├── backend/
│   ├── app/            # FastAPI web (API·인증·모델·서비스)
│   │   ├── api/        # 라우트·의존성
│   │   ├── core/       # 설정·보안·세션·DB·Redis·채널 규약
│   │   ├── models/     # SQLAlchemy 모델
│   │   ├── schemas/    # Pydantic 스키마
│   │   └── services/   # KIS 클라이언트·백테스트·데이터 적재
│   ├── engine/         # 독립 매매 엔진 프로세스
│   ├── worker/         # Celery 워커
│   └── alembic/        # DB 마이그레이션
├── frontend/           # Next.js 대시보드
├── docs/PRD.md         # 제품 요구사항 정의서
├── Caddyfile           # 리버스 프록시(단일 출처) 설정
└── docker-compose.yml
```

---

## 증권사(브로커) 선택

자격증명 등록(`PUT /api/kis/credentials`) 시 `broker` 필드로 증권사를 선택합니다.
엔진·라우트는 `app/services/broker`의 `BrokerClient` 인터페이스에만 의존하며,
`make_broker_for_user(user)` 팩토리가 사용자 설정에 따라 구현체를 주입합니다.

**자격증명 우선순위**: 사용자가 앱에서 등록한 DB 값(암호화)을 우선 사용하고,
없으면 기본 자격증명(`KIS_APP_KEY`/`KIS_APP_SECRET`, `TOSS_APP_KEY`/`TOSS_APP_SECRET`)을
폴백으로 사용합니다. 개인(단일 운영자)은 웹 등록 없이 폴백만으로 매매할 수 있고,
멀티 유저로 운영하면 폴백을 비워 두고 사용자별 등록 API 를 쓰는 것이 안전합니다.
이 키들은 평문 `.env` 대신 **시크릿 파일**로 두는 것을 권장합니다(아래 *키 관리* 참고).

## 🔐 키 관리 (시크릿 파일)

마스터 키(`SECRET_KEY`, `CREDENTIAL_ENC_KEY`)와 브로커 폴백 키는 평문 `.env` 가
아니라 **시크릿 파일**(`secrets/*.txt`)로 보관합니다. docker compose 가 이를
`/run/secrets/*`(tmpfs)로 마운트하고, 설정 로더가 `<FIELD>_FILE`(예:
`CREDENTIAL_ENC_KEY_FILE=/run/secrets/credential_enc_key`)을 통해 평문 env 보다
**우선** 읽습니다. 덕분에 비밀이 `docker inspect`·이미지 레이어·프로세스 환경에
남지 않습니다.

```bash
# 최초 1회 — 시크릿 파일 생성(secrets/ 는 .gitignore 로 제외됨)
openssl rand -hex 32 > secrets/secret_key.txt
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > secrets/credential_enc_key.txt
# 미사용 브로커 키는 빈 파일로(파일은 존재해야 compose 기동)
: > secrets/kis_app_key.txt;  : > secrets/kis_app_secret.txt
: > secrets/toss_app_key.txt; : > secrets/toss_app_secret.txt
chmod 600 secrets/*.txt   # 선택, 권장
```

자세한 내용은 [`secrets/README.md`](secrets/README.md) 참고. `CREDENTIAL_ENC_KEY` 를
교체하면 기존에 암호화 저장된 DB 자격증명을 복호화할 수 없으니 주의하세요.

| 브로커 | 자격증명(공통 컬럼 재사용) | 실시간 시세 | 모의투자 |
|--------|---------------------------|-------------|----------|
| `kis`(기본) | app_key / app_secret / 계좌번호 | WebSocket | 지원(vts) |
| `toss` | client_id / client_secret / accountSeq | **REST 폴링**(WS 미지원) | **없음(항상 실거래)** |

- 토스 사용자는 엔진이 WS 피드를 띄우지 않고 runner 가 `get_quote` REST 폴링으로 현재가를 얻습니다. 토스가 향후 WebSocket을 제공하면 별도 WS 클라이언트를 `PriceFeed`에 연동하면 됩니다(`TOSS_WS_URL` 추가 지점 주석 참고).
- 토스는 그룹별 rate limit(예: ORDER 6req/s, 개장 09:00–09:10 3req/s, ACCOUNT 1req/s)이 있어 429 시 `Retry-After`를 존중해 1회 재시도합니다.

## ⚠️ 주의

- 기본값은 **모의투자(vts)**입니다. 실전(`KIS_ENV=prod`) 전환은 충분한 검증 후에만 하세요. 실거래는 **자금 손실 위험**이 있습니다.
- **토스는 모의투자 환경이 없어 등록 즉시 실거래로 동작합니다.** 충분히 검증된 전략에만 사용하세요.
- App Key/Secret(=토스 client_id/secret)은 평문으로 저장·로깅하지 않으며 Fernet으로 암호화됩니다. `CREDENTIAL_ENC_KEY`를 분실하면 저장된 자격증명을 복호화할 수 없습니다.
- 운영 배포 시 `APP_ENV=prod`, `COOKIE_SECURE=true`로 설정하고, 교차 출처면 `COOKIE_SAMESITE=none`을 사용하세요.
