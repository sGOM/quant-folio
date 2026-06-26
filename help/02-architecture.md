# 02. 아키텍처 — 왜 web 과 engine 을 분리했나

이 문서의 목표: **프로세스 분리의 이유**와 **둘 사이의 통신 규약(Redis)** 을 이해하는 것.
이걸 이해하면 이 프로젝트의 절반은 끝난 것이다.

---

## 1. 전체 그림

```
                         ┌──────────────────────────────┐
 폰/브라우저  ──HTTP(S)──▶ │  proxy (Caddy, :8080)        │ ← 유일한 외부 진입점
                         │  /  →frontend  /api,/ws→web  │
                         └───────┬───────────────┬──────┘
                                 ▼               ▼
                         ┌──────────┐      ┌───────────┐
                         │ Next.js  │      │  web      │  FastAPI (REST + WS 푸시)
                         │ frontend │      │  (8000)   │  세션 쿠키 인증, 매매로직 없음
                         └──────────┘      └─────┬─────┘
                                                 │  Redis (pub/sub · 큐 · 락 · 세션)
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

`docker-compose.yml` 의 서비스 6개가 정확히 이 그림이다: `proxy`, `frontend`,
`web`, `engine`, `worker`, `db`, `redis`.

---

## 2. 핵심 질문: 매매 로직을 왜 web 에 안 두나?

Spring 1년차 시절 본능대로면 "`TradingController` 에서 주문 쏘면 되지 않나?" 싶다.
하지만 이 프로젝트는 **매매 로직을 HTTP 핸들러에 절대 두지 않는다.** 이유:

> **웹 서버는 자주 재시작된다.** 코드 배포, OOM, 설정 변경, `--reload`...
> 그런데 매매가 HTTP 핸들러 안에서 돌고 있으면, **재배포 한 번에 진행 중인 매매가
> 끊겨 실제 금전 손실**로 직결된다.

그래서:

- **web** (`backend/app`): 사용자 요청 응답, 인증, 설정 CRUD, 실시간 푸시.
  "전략을 켜라/꺼라" 라는 **명령만** 받아서 엔진에 전달한다. **직접 주문하지 않는다.**
- **engine** (`backend/engine`): web 과 무관하게 24시간 도는 별도 프로세스.
  시세 구독 → 신호 생성 → 리스크 체크 → 주문 → 체결/포지션 기록을 담당.

web 을 재배포해도 engine 은 계속 돈다. engine 을 재배포해도 (start/stop 명령은
DB 상태로 남아 있어) 재기동 시 **운용 중이던 전략을 복구**한다
(`engine/main.py` 의 `_recover()`).

> Spring 으로 치면: 결제/정산 데몬을 API 서버와 같은 JVM 에 두지 않고 별도
> 마이크로서비스로 떼어, API 서버 무중단 배포가 정산을 흔들지 않게 하는 것과 같다.

---

## 3. 그럼 둘은 어떻게 대화하나? → Redis

web 과 engine 은 메모리를 공유하지 않는 **별개의 프로세스**다. 그래서 모든 제어와
이벤트는 **Redis** 를 거친다. 규약(채널/키 이름)은 양쪽이 공유하는 한 파일에
상수로 정의돼 있다 → `backend/app/core/channels.py` (반드시 한 번 열어볼 것).

### 통신 4가지 패턴

| 패턴 | Redis 도구 | 용도 | 코드 |
|------|-----------|------|------|
| **제어 명령** | pub/sub `engine:control` | web → engine "전략 start/stop" | `routes/engine.py` → `engine/main.py` |
| **실시간 이벤트** | pub/sub `engine:events:{user_id}` | engine → web "체결/주문 발생" | `engine/executor.py` → `routes/ws.py` |
| **분산 락** | `SET NX` | 중복 주문/이중 매수 방지 | `engine/executor.py`, `runner.py` |
| **캐시/상태** | `GET/SET` (TTL) | 세션, KIS 토큰, 현재가, 하트비트 | `core/session.py`, `kis/client.py` |

### (a) 제어: 전략 ON 을 누르면

```
[프론트] 전략 시작 버튼
   → POST /api/engine/strategies/3/start        (routes/engine.py)
   → DB: strategy.status = LIVE
   → Redis PUBLISH engine:control {"action":"start","strategy_id":3}
   → [engine/main.py] 구독 중이던 _control_loop 가 수신
   → _start_strategy(3): StrategyRunner 태스크 생성
```

web 은 "켜라" 고 외치고 끝. 실제 매매는 engine 이 한다.

### (b) 이벤트: 주문이 체결되면

```
[engine/executor.py] 주문 체결 기록
   → Redis PUBLISH engine:events:42 {"type":"execution", ...}
   → [routes/ws.py] user 42 의 WebSocket 이 그 채널을 구독 중
   → 브라우저로 즉시 푸시 → 화면 갱신
```

> 사용자별 채널(`engine:events:{user_id}`)을 쓰는 이유: 모든 소켓이 공용 채널을
> 구독하면 남의 이벤트까지 받아 팬아웃이 커진다. 자기 채널만 구독해 트래픽을 줄인다.

### (c) 하트비트: 엔진 살아있나?

engine 은 5초마다 `engine:heartbeat` 키를 TTL 15초로 갱신한다
(`engine/main.py` `_heartbeat_loop`). web 의 `GET /api/engine/status` 는 그 키가
있는지로 엔진 생존을 판단한다. (TTL 이 지나 키가 사라지면 = 엔진 죽음)

---

## 4. 데이터 흐름 한 장 요약

```
                  ┌─── 백테스트 경로 (과거) ───┐
 FinanceDataReader/pykrx → price_ticks(DB) → vectorbt 계산 → backtests(DB)
                  └────────────────────────────┘

                  ┌─── 실시간 매매 경로 (현재) ───┐
 KIS WebSocket 시세 → Redis price:{symbol} 캐시
        → engine runner 가 신호 평가 → risk 체크
        → KIS 주문 → orders/executions/positions(DB)
        → Redis 이벤트 → web WS → 브라우저
                  └──────────────────────────────┘
```

**중요**: 백테스트와 실시간 매매가 **같은 신호 함수**
(`app/services/backtest/signals.py`)를 쓴다. 그래야 "백테스트에서 좋았던 전략이
실거래에서 다르게 동작" 하는 일이 없다. ([06 문서](06-backtesting-and-signals.md)에서 상술)

---

## 5. 보안 토폴로지 (왜 포트가 127.0.0.1 인가)

`docker-compose.yml` 을 보면 `web`/`db`/`redis`/`frontend` 의 호스트 포트가
`127.0.0.1:...` 로 바인딩돼 있다. 외부(테일넷)에 노출되는 건 **proxy(:8080) 하나뿐**.

- 외부 → `proxy(:8080)` → 내부에서 `web`/`frontend` 로 라우팅
- DB·Redis 는 호스트 로컬에서만 접근 가능 (외부 침투면 차단)

Spring 으로 치면 API Gateway 한 대만 공개하고 나머지는 사설망에 두는 구성.

---

## 다음 단계

큰 그림과 통신을 봤으니, 이제 **FastAPI 언어/관용구**를 Spring 어휘로 번역하자.

→ [03-fastapi-for-spring-developers.md](03-fastapi-for-spring-developers.md)

### 직접 열어볼 파일
- `backend/app/core/channels.py` — **40줄, 필독.** web↔engine 규약의 단일 출처.
- `backend/engine/main.py` — 엔진이 제어 명령을 받고 전략을 켜고 끄는 루프.
- `backend/app/api/routes/engine.py` — web 이 명령을 발행하는 쪽.
