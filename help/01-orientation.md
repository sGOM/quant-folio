# 01. 오리엔테이션 — 디렉터리 지도와 멘탈 모델

이 문서의 목표: **"전체 그림을 머릿속에 올리고, Spring Boot 경험을 이 프로젝트에
매핑"** 하는 것. 코드 한 줄도 아직 읽지 않아도 된다.

---

## 1. 디렉터리 지도

```
quant/
├── backend/                  ← 우리가 집중할 곳 (Python)
│   ├── app/                  ← FastAPI "web" 서비스 = REST/WS API 서버
│   │   ├── main.py           ← 진입점 (@SpringBootApplication + WebConfig)
│   │   ├── core/             ← 횡단 관심사 (설정·보안·DB·Redis·세션·채널 규약)
│   │   ├── models/           ← SQLAlchemy 엔티티 (JPA @Entity 격)
│   │   ├── schemas/          ← Pydantic DTO (요청/응답 검증·직렬화)
│   │   ├── api/
│   │   │   ├── deps.py       ← 공통 의존성 (현재 사용자 등) = Spring 의 Resolver/Filter
│   │   │   └── routes/       ← @RestController 들 (auth, strategies, backtests...)
│   │   └── services/         ← 비즈니스 로직 (@Service 격)
│   │       ├── kis/          ← 한국투자증권 API 클라이언트
│   │       ├── broker/       ← 증권사 추상화 (KIS/토스 공통 인터페이스)
│   │       ├── backtest/     ← 백테스트 엔진 + 신호 생성
│   │       └── data/         ← 과거 시세 적재(loader)
│   │
│   ├── engine/               ← ★ 독립 프로세스: 24시간 매매 엔진 (web 과 분리!)
│   │   ├── main.py           ← 엔진 진입점 (asyncio 이벤트 루프)
│   │   ├── runner.py         ← 전략 1개를 굴리는 실행기 (신호→리스크→주문)
│   │   ├── executor.py       ← 주문 실행 + 멱등성 + DB 기록
│   │   ├── risk.py           ← 리스크 관리 (손절·포지션·일일 한도)
│   │   └── price_feed.py     ← KIS WebSocket 시세 구독 → Redis 캐시
│   │
│   ├── worker/               ← Celery 워커 (배치 작업)
│   └── alembic/              ← DB 마이그레이션 (Flyway/Liquibase 격)
│
├── frontend/                 ← Next.js (이 가이드에선 거의 다루지 않음)
├── docker-compose.yml        ← 6개 컨테이너 오케스트레이션
├── Caddyfile                 ← 리버스 프록시 (단일 진입점 :8080)
└── docs/                     ← PRD, 전략 수식 문서
```

핵심만 기억하라: **`backend/app` = 평범한 API 서버**, **`backend/engine` = 따로 도는
매매 데몬**. 이 둘이 물리적으로 분리돼 있다는 게 이 프로젝트의 가장 중요한 설계다
(이유는 [02-architecture.md](02-architecture.md)).

---

## 2. Spring Boot ↔ 이 프로젝트 대응표

언어/프레임워크 어휘만 번역하면 대부분 익숙한 개념이다.

| Spring Boot 세계 | 이 프로젝트 (FastAPI 세계) | 비고 |
|------------------|----------------------------|------|
| `@SpringBootApplication` 메인 | `app/main.py` 의 `app = FastAPI(...)` | 앱 객체를 만들고 라우터를 등록 |
| `application.yml` + `@ConfigurationProperties` | `app/core/config.py` 의 `Settings` | pydantic-settings 가 env 를 타입검증해 로드 |
| `@RestController` + `@RequestMapping` | `app/api/routes/*.py` 의 `APIRouter` | 파일 1개 = 컨트롤러 1개 느낌 |
| `@GetMapping`, `@PostMapping` | `@router.get(...)`, `@router.post(...)` | 데코레이터 위치만 다름 |
| `@Service` 빈 | `app/services/*` 모듈/클래스 | DI 컨테이너 대신 그냥 import 해서 씀 |
| JPA `@Entity` | `app/models/models.py` (SQLAlchemy) | `Mapped[...]` 타입으로 컬럼 선언 |
| DTO / `record` | `app/schemas/*.py` (Pydantic) | 요청/응답 검증 + 직렬화 자동 |
| `JpaRepository` | 별도 Repository 없음 — `select(...)` 쿼리를 서비스/라우트에서 직접 | 얇은 계층 선호 |
| `@Transactional` | `async with db.begin()` / 라우트 끝 `await db.commit()` | 명시적 커밋 |
| `@Autowired` 생성자 주입 | `Depends(...)` 함수 파라미터 주입 | "의존성 = 함수" |
| `HandlerInterceptor` / `OncePerRequestFilter` | `Depends(get_current_user)` 같은 의존성 | 인증을 의존성으로 표현 |
| Spring Security 세션 | `core/session.py` (Redis 서버측 세션) | JWT 아님! 뒤에서 설명 |
| `@Scheduled` 24시간 작업 | `backend/engine` (별도 프로세스) | 가장 큰 차이 |
| Flyway / Liquibase | `alembic/` | `alembic upgrade head` |
| springdoc Swagger | 자동 제공 `/docs` | 코드에서 자동 생성 |
| Lombok `@Slf4j` | `logging.getLogger(__name__)` | 표준 logging |

> **번역만 해두면 70% 는 익숙하다.** 정말 새로운 건 ① 프로세스 분리,
> ② `async/await` 비동기, ③ 세션 인증 방식 — 이 셋뿐이다.

---

## 3. 가장 큰 3가지 차이 (미리 경고)

### (1) `async def` — 모든 게 비동기다

Spring MVC 는 요청당 스레드를 잡는 동기 모델이 기본이다. FastAPI 는 **단일
이벤트 루프 + 코루틴** 모델이다(Spring WebFlux/리액터에 가깝다).

```python
async def login(...):          # 코루틴
    user = await db.scalar(...) # await = I/O 동안 다른 요청에 양보
```

규칙: **I/O(DB·Redis·HTTP) 앞엔 거의 항상 `await`** 가 붙는다. `await` 를 빠뜨리면
"코루틴 객체"가 그대로 흘러 버그가 난다. CPU 무거운 작업(백테스트)은 이벤트 루프를
막으므로 **스레드풀로 던진다**(`run_in_threadpool`, `asyncio.to_thread`).

### (2) Repository 계층이 거의 없다

JPA 의 `findByEmail` 같은 메서드 대신, 라우트/서비스 안에서 SQLAlchemy 쿼리를
직접 쓴다:

```python
user = await db.scalar(select(User).where(User.email == form.username))
```

처음엔 허전하지만, "쿼리가 쓰이는 곳에 쿼리가 보인다"는 장점이 있다.

### (3) 인증이 JWT 가 아니라 서버측 세션이다

쿠키엔 **불투명한 세션 ID** 만 담고, 진짜 정보(`user_id`)는 Redis 에 있다. 로그아웃
= Redis 키 삭제로 **즉시 무효화**. (자세히는 [04-request-lifecycle.md](04-request-lifecycle.md))

---

## 4. 다음 단계

전체 지도를 그렸으니, 이제 **왜 이렇게 나눴는지**(아키텍처)를 보자.

→ [02-architecture.md](02-architecture.md)

### 직접 열어볼 파일
- `backend/app/main.py` — 30줄. 앱이 어떻게 조립되는지 한눈에 들어온다.
- `docker-compose.yml` — 어떤 프로세스들이 같이 도는지(서비스 목록)를 본다.
