# 08. Docker 컨테이너 — 누가 무슨 일을 하나

이 문서의 목표: `docker compose up` 하면 뜨는 **7개 컨테이너**가 각각 무슨 역할이고,
언제 누구를 만지면 되는지 정리한다. Spring 개발자가 보통 `mvn spring-boot:run` 하나로
끝내던 것을, 여기선 **역할별로 프로세스를 쪼개** 컨테이너로 띄운다.

> 정의 파일: 루트 `docker-compose.yml` 하나. 아래 모든 설명이 그 파일에서 나온다.

---

## 1. 한눈에 보기

```
                       외부(폰/브라우저)
                             │ :8080
                    ┌────────▼────────┐
                    │     proxy       │  Caddy — 유일한 외부 진입점
                    └───┬─────────┬───┘
              / (그 외) │         │ /api /ws /docs /health
                  ┌─────▼───┐ ┌───▼─────┐
                  │ frontend│ │   web   │  FastAPI REST/WS
                  │ Next.js │ └───┬─────┘
                  └─────────┘     │
       ┌────────────────────┬─────┴──────┬───────────────┐
       │                    │            │               │
  ┌────▼────┐          ┌────▼────┐  ┌────▼────┐    (Redis 공유)
  │ engine  │          │ worker  │  │  redis  │◄────────┘
  │ 매매엔진 │          │ Celery  │  └─────────┘
  └────┬────┘          └────┬────┘
       └──────────┬─────────┘
             ┌────▼────┐
             │   db    │  PostgreSQL + TimescaleDB
             └─────────┘
```

| 컨테이너 | 이미지/빌드 | 한 줄 역할 | 외부 노출 |
|----------|-------------|-----------|-----------|
| **proxy** | `caddy:2-alpine` | 리버스 프록시, 단일 진입점 | ✅ `:8080` (유일) |
| **frontend** | `./frontend` 빌드 | Next.js 대시보드 | ❌ (127.0.0.1:3000) |
| **web** | `./backend` 빌드 | FastAPI API 서버 (인증·CRUD·WS 푸시) | ❌ (127.0.0.1:8000) |
| **engine** | `./backend` 빌드 | 24시간 매매 엔진 (별도 프로세스) | ❌ 포트 없음 |
| **worker** | `./backend` 빌드 | Celery 배치 작업 워커 | ❌ 포트 없음 |
| **db** | `timescale/timescaledb` | PostgreSQL + 시계열 확장 | ❌ (127.0.0.1:5432) |
| **redis** | `redis:7-alpine` | 세션·캐시·pub/sub·락·큐 | ❌ (127.0.0.1:6379) |

> **핵심**: 외부에 뚫린 포트는 `proxy(:8080)` **딱 하나**. 나머지는 `127.0.0.1` 에만
> 바인딩돼 호스트 로컬에서만 접근 가능하다(테일넷·외부 노출 차단). 컨테이너끼리는
> 도커 내부 네트워크에서 서비스 이름(`web`, `db`, `redis`...)으로 통신한다.

---

## 2. 컨테이너별 상세

### proxy (Caddy) — 문지기

- **하는 일**: 들어온 요청을 경로로 분기. `/api/*`, `/ws`, `/docs`, `/health` 는
  `web:8000` 으로, 나머지는 전부 `frontend:3000` 으로 보낸다 (`Caddyfile` 참고).
- **왜 필요**: 프론트와 백엔드를 **한 출처(:8080)** 로 합친다. 그러면
  - 폰이 어떤 주소(Tailscale IP/머신명)로 들어와도 프론트가 **상대경로**로 API 를
    호출 → 주소가 바뀌어도 재빌드 불필요.
  - 같은 출처라 **CORS·교차출처 쿠키 문제가 사라진다**(모바일 Safari 의 엄격한
    SameSite/Secure 정책에서도 인증 안정적).
- **WebSocket**: `reverse_proxy` 가 WS Upgrade 를 자동 처리(`/ws` 실시간 피드).
- **언제 만지나**: 새 경로를 백엔드로 보내야 할 때 `Caddyfile` 의 `@backend` 목록 수정.

### web — API 서버 (가장 자주 보게 될 컨테이너)

- **하는 일**: FastAPI. 로그인/세션, 전략·백테스트 CRUD, 전략 ON/OFF **명령 발행**,
  WebSocket 으로 실시간 이벤트 푸시. **매매 로직은 없다**([02 문서](02-architecture.md)).
- **커맨드**: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
  (`--reload` 없음 — 24시간 운용 중 파일 변경에 의한 의도치 않은 재시작 방지).
  단, 개발 환경에선 `docker-compose.override.yml` 이 자동 병합되어 `--reload` 로 뜬다.
  운영 배포는 `docker compose -f docker-compose.yml up -d --build` 로 override 를 제외.
- **언제 만지나**: API 엔드포인트·인증·DB 모델 작업. 대부분의 백엔드 개발.
- **로그**: `docker compose logs -f web` — 요청 처리, 명령 발행이 보인다.

### engine — 매매 엔진 ⭐

- **하는 일**: web 과 **완전히 분리된 프로세스**. KIS 시세 구독 → 신호 → 리스크 →
  주문 → 체결/포지션 기록. start/stop 은 Redis `engine:control` 로 수신
  ([05 문서](05-trading-engine.md)).
- **커맨드**: `python -m engine.main` (asyncio 이벤트 루프, 포트 없음).
- **왜 분리**: web 을 재배포해도 매매가 안 끊기게. engine 재기동 시 `status=LIVE`
  전략을 DB 기준으로 **복구**한다.
- **언제 만지나**: 매매 판단·주문·리스크·시세 피드 로직.
- **로그**: `docker compose logs -f engine` — **전략 ON 했을 때 신호/주문이 여기 찍힌다.**
  매매가 안 도는 것 같으면 가장 먼저 볼 곳.

### worker — Celery 배치

- **하는 일**: 무거운/주기적 배치 작업. 헬스체크(`ping`) 외에 **beat 스케줄 6개**가
  등록돼 있다 (`worker/celery_app.py` 의 `beat_schedule`, 구현은 `worker/tasks.py`):

  | 태스크 | 주기 | 하는 일 |
  |--------|------|---------|
  | `ingest_daily_ohlcv` | 매일 18:30 | 장 마감·시세 확정 후 당일 일봉을 `price_ticks` 에 적재 |
  | `check_fill_quality_drift` | 매주 월 09:00 | 체결 품질(슬리피지 실측) 드리프트 정기 점검 |
  | `snapshot_sector_map` | 분기 1회(1/4/7/10월 1일 19:00) | 업종분류를 `sector_map_snapshots` 에 적재(섹터 캡 PIT화) |
  | `backup_database` | 매일 03:00 | `pg_dump` 압축 백업 + 보존기간 관리(`docs/db-backup.md`) |
  | `check_backup_freshness` | 매일 09:00 | 백업 성공 기록 신선도 점검 — worker/beat 침묵 사망 감지 |
  | `cleanup_old_alerts` | 매일 04:00 | `alerts` 보존정책 정리(읽음 90일·미확인 180일 초과 삭제) |

- **커맨드**: `celery -A worker.celery_app.celery_app worker --loglevel=info -B`
  — `-B` 가 beat 스케줄러를 워커 프로세스에 **내장**한다(별도 beat 컨테이너 없음).
  단일 워커 replica 전제이며, 워커를 여러 개로 늘리면 스케줄이 중복 발화한다.
- **engine 과 차이**: engine 은 "항상 도는 실시간 데몬", worker 는 "요청받으면 처리하는
  작업 큐 컨슈머"(Spring 의 `@Async`/메시지 컨슈머). 브로커·백엔드 모두 Redis 사용.
- **언제 만지나**: 시간이 오래 걸리거나 스케줄링이 필요한 작업을 추가할 때.

### db — PostgreSQL + TimescaleDB

- **하는 일**: 모든 영속 데이터(users, strategies, backtests, orders, executions,
  positions, sector_map_snapshots, risk_limits). `price_ticks` 는
  **TimescaleDB hypertable**(시계열 최적화).
- **이미지**: `timescale/timescaledb:latest-pg16` (PostgreSQL + 시계열 확장).
- **영속성**: `pgdata` named volume 에 저장 → 컨테이너 지워도 데이터 유지.
- **마이그레이션**: `docker compose exec web alembic upgrade head`
  (Flyway 격. **web 컨테이너 안에서** alembic 을 돌린다).
- **언제 만지나**: 직접 거의 안 만진다. 스키마 변경은 alembic 마이그레이션으로.

### redis — 만능 중간자

세션·캐시·pub/sub·락·큐를 전부 담당하는 핵심 인프라. 끊기면 인증·매매 다 멈춘다.

| 용도 | 키/채널 | 누가 |
|------|---------|------|
| 로그인 세션 | `session:{sid}` | web |
| KIS 토큰 캐시 | `kis:token:...` | web/engine |
| 현재가 캐시 | `price:{symbol}` | engine(피드)→engine(러너) |
| 전략 제어 | `engine:control` (pub/sub) | web→engine |
| 실시간 이벤트 | `engine:events:{uid}` (pub/sub) | engine→web→브라우저 |
| 주문/포지션 락 | `lock:order:*`, `lock:position:*` | engine |
| 엔진 하트비트 | `engine:heartbeat` | engine |
| 러너 헬스 상태 | `engine:health:{strategy_id}` | engine→web |
| Celery 브로커 | (큐) | worker |

### frontend — Next.js (이 가이드 범위 밖)

대시보드 UI. `npm run dev` 로 뜬다. **익명 볼륨 `/app/node_modules`** 가 걸려 있어
호스트의 node_modules 와 격리된다 → **패키지 추가는 컨테이너 안에서** 해야 반영된다.

---

## 3. 한 이미지로 web/engine/worker 3개를 띄우는 트릭

`web`, `engine`, `worker` 는 **모두 `./backend` 같은 이미지**를 빌드해서 쓴다.
`backend/Dockerfile` 의 기본 `CMD` 는 web(uvicorn)이고, engine/worker 는
compose 에서 `command:` 로 **덮어쓴다**:

```yaml
web:    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
engine: command: python -m engine.main
worker: command: celery -A worker.celery_app.celery_app worker --loglevel=info -B
```

→ 코드 한 벌, 의존성 한 벌. **역할만 커맨드로 분기**한다. 셋이 같은 코드베이스
(`app.*` 모듈)를 공유하므로 engine 도 `app.core`, `app.models` 를 그대로 import 한다.

---

## 4. 의존성·기동 순서 (`depends_on` + healthcheck)

```
db (healthy) ─┐
redis (healthy)─┼─► web ──► proxy
              ├─► engine
              └─► worker(redis만)
frontend ─────────► proxy
```

- `db`/`redis` 는 **healthcheck** 가 통과해야 `web`/`engine` 이 뜬다
  (`pg_isready`, `redis-cli ping`). DB 가 덜 떴는데 web 이 붙다 죽는 걸 방지.
- 모든 서비스에 `restart: unless-stopped` → 크래시·재부팅 후 자동 복구.

---

## 5. 시크릿 주입 (compose → 컨테이너)

`web`/`engine`/`worker` 는 공통으로 시크릿 파일을 `/run/secrets/*`(tmpfs)로 마운트
받는다. compose 의 `x-backend-secret-env` 앵커가 `*_FILE` 환경변수를 넣고,
config 로더가 그 파일을 읽어 평문 env 보다 우선 사용한다([04 문서](04-request-lifecycle.md) §4).
→ 비밀이 `docker inspect`·이미지 레이어에 안 남는다.

```
secrets/*.txt (호스트) ─► /run/secrets/* (컨테이너 tmpfs) ─► config._load_secret_files
```

---

## 6. 코드 변경이 반영되는 법 (volume 마운트)

`web`/`engine`/`worker` 는 `./backend:/app` 을 **bind mount** 한다 → 호스트에서 코드를
고치면 컨테이너 안에 즉시 반영된다. 단:

- **web**: 개발 환경(override 병합)에선 `--reload` 로 즉시 반영. 운영(override 제외)
  기동이면 `docker compose restart web` 필요.
- **engine/worker 는** 항상 재시작 필요: `docker compose restart engine`.
- **requirements.txt(의존성) 변경** 시엔 이미지 재빌드: `docker compose up -d --build`.

---

## 7. 자주 쓰는 명령 모음 (치트시트)

```bash
# 전체 기동/중지
docker compose up -d --build        # 빌드 + 백그라운드 기동
docker compose down                 # 중지(볼륨 유지). -v 붙이면 DB까지 삭제(주의!)

# 상태/로그
docker compose ps                   # 컨테이너 상태(running/healthy)
docker compose logs -f web          # web 로그 실시간
docker compose logs -f engine       # 매매 엔진 로그(신호·주문 디버깅 1순위)

# 특정 서비스만
docker compose restart web          # 코드 변경 반영
docker compose restart engine

# 컨테이너 안에서 실행
docker compose exec web alembic upgrade head   # DB 마이그레이션
docker compose exec web pytest                 # 백엔드 테스트
docker compose exec web bash                   # 셸 진입(디버깅)
docker compose exec db psql -U quant quant     # DB 직접 접속
docker compose exec redis redis-cli            # Redis 직접 접속

# 백업
docker compose exec db pg_dump -U quant quant > backup.sql
```

---

## 8. "어디가 문제지?" 트러블슈팅 매핑

| 증상 | 먼저 볼 컨테이너 |
|------|------------------|
| 로그인/페이지가 안 뜸 | `proxy`, `web`, `frontend` 로그 |
| 로그인은 되는데 인증이 자꾸 풀림 | `redis`(세션), 쿠키 설정(`COOKIE_SECURE`) |
| 전략 켰는데 매매가 안 됨 | **`engine` 로그** → 장 운영시간? 자격증명? 신호? |
| 실시간 화면이 갱신 안 됨 | `web`(WS), `redis`(pub/sub), `engine`(이벤트 발행) |
| 백테스트가 느리거나 실패 | `web` 로그(스레드풀), 데이터 적재 실패 여부 |
| 컨테이너가 안 뜸 | `docker compose ps` → `depends_on`/healthcheck, 시크릿 누락 |
| 엔진 살아있나 확인 | `GET /api/engine/status`(하트비트) 또는 `engine` 로그 |

---

## 직접 열어볼 파일
- `docker-compose.yml` — 모든 서비스 정의. 이 문서와 나란히 놓고 읽어라.
- `Caddyfile` — 프록시 라우팅 규칙.
- `backend/Dockerfile` — web/engine/worker 공용 이미지.
- 루트 `README.md` "실행 방법" — 노트북을 24시간 서버로 만드는 배포 절차.
