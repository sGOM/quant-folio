# QuantFolio 학습 가이드 (백엔드 중심)

> 대상: **Spring Boot 1년차** 경험은 있지만 **FastAPI · Next.js 는 처음**인 개발자
>
> 이 폴더는 "이 프로젝트를 공부의 영역에서 접근할 때 어디부터 어떻게 봐야 하는가"를
> 안내한다. 프론트엔드(Next.js)는 의도적으로 최소한만 다루고, **백엔드(FastAPI ·
> 매매 엔진)** 를 깊게 설명한다.

---

## 0. 이 프로젝트 한 줄 요약

> 국내 주식(KRX) 대상으로 **퀀트 전략을 백테스팅**하고, 같은 전략을
> **한국투자증권(KIS) API 로 실시간 자동매매**하는 웹 플랫폼.

Spring Boot 식으로 비유하면:

- `web` = 평범한 `@RestController` 서버 (REST + WebSocket, 인증, CRUD)
- `engine` = 웹과 **별도 프로세스로 도는 배치/데몬** (24시간 도는 `@Scheduled` 워커를
  아예 독립 JVM 으로 떼어냈다고 생각하면 된다)
- `worker` = Celery (Spring 의 `@Async` / 메시지 큐 컨슈머에 해당)
- 둘 사이의 통신은 **Redis pub/sub · 큐 · 분산 락** (Spring 에서 Kafka/Redis 로
  서비스 간 이벤트를 주고받는 것과 같은 그림)

---

## 1. 추천 학습 순서

아래 순서대로 읽으면 "큰 그림 → 언어/프레임워크 차이 → 실제 코드 → 도메인" 으로
좁혀진다. 각 문서 끝에는 **직접 열어볼 파일 경로**가 적혀 있다.

| 순서 | 문서 | 무엇을 얻는가 |
|------|------|----------------|
| 1 | [`01-orientation.md`](01-orientation.md) | 전체 디렉터리 지도, Spring Boot ↔ 이 프로젝트 멘탈 모델 매핑 |
| 2 | [`02-architecture.md`](02-architecture.md) | 왜 web/engine 을 프로세스로 분리했는가, Redis 통신 규약 |
| 3 | [`03-fastapi-for-spring-developers.md`](03-fastapi-for-spring-developers.md) | FastAPI/SQLAlchemy/Pydantic 를 Spring 어휘로 번역 |
| 4 | [`04-request-lifecycle.md`](04-request-lifecycle.md) | 요청 1건이 인증·세션·DB 를 거치는 전 과정 |
| 5 | [`05-trading-engine.md`](05-trading-engine.md) | 매매 엔진: 신호→리스크→주문, 멱등성, 분산 락 |
| 6 | [`06-backtesting-and-signals.md`](06-backtesting-and-signals.md) | 백테스트와 신호 로직, "미래참조" 주의점 |
| 7 | [`07-glossary.md`](07-glossary.md) | 퀀트/증권 도메인 용어 사전 |
| 8 | [`08-docker-containers.md`](08-docker-containers.md) | 7개 컨테이너의 역할·기동 순서·운영 명령 |

> 시간이 없다면 **1 → 2 → 4** 만 읽어도 백엔드 골격은 잡힌다.
> 직접 띄워보며 익히는 스타일이면 **8번(Docker)** 을 1번 다음에 봐도 좋다.

---

## 2. 처음 30분: 직접 돌려보며 감 잡기

```bash
# (최초 1회) 시크릿 파일 + .env 준비는 루트 README.md "실행 방법" 참고
docker compose up -d --build
docker compose exec web alembic upgrade head   # DB 테이블 생성
```

그 다음 브라우저에서:

- <http://localhost:8080/docs> — **Swagger UI**. Spring 의 springdoc-openapi 와 동일.
  여기서 API 목록을 먼저 눈으로 훑는 것이 코드 읽기보다 빠르다.
- <http://localhost:8080/health> — 헬스체크 JSON.

로그를 흐르게 켜 두고 읽으면 이해가 빠르다:

```bash
docker compose logs -f web      # API 서버
docker compose logs -f engine   # 매매 엔진(전략 ON 시 여기서 신호/주문 로그)
```

---

## 3. "어디부터 코드를 열까" 빠른 진입점

| 궁금한 것 | 먼저 열 파일 |
|-----------|--------------|
| 서버가 어떻게 뜨나 (`@SpringBootApplication` 격) | `backend/app/main.py` |
| 설정/환경변수 (`application.yml` 격) | `backend/app/core/config.py` |
| DB 테이블 정의 (`@Entity` 격) | `backend/app/models/models.py` |
| 로그인/인증이 어떻게 도나 | `backend/app/api/routes/auth.py`, `backend/app/api/deps.py` |
| 매매 엔진의 심장 | `backend/engine/runner.py`, `backend/engine/executor.py` |
| 증권사 API 호출 | `backend/app/services/kis/client.py` |
| 백테스트 계산 | `backend/app/services/backtest/engine.py` |

---

## 4. 함께 보면 좋은 기존 문서

- 루트 [`README.md`](../README.md) — 실행/배포(노트북을 24시간 서버로) 절차
- [`docs/PRD.md`](../docs/PRD.md) — 제품 요구사항·데이터 모델 정의
- [`docs/strategies.md`](../docs/strategies.md) — 전략별 수식·금융 근거(매우 상세)
