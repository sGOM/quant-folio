# 03. Spring 개발자를 위한 FastAPI 번역

이 문서의 목표: FastAPI · SQLAlchemy · Pydantic 을 **Spring 어휘로 옮겨** 거부감을
없애는 것. 실제 이 레포의 코드로 설명한다.

---

## 1. 앱 부트스트랩 — `main.py`

```python
# backend/app/main.py
app = FastAPI(title="QuantFolio API", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=[settings.FRONTEND_ORIGIN], ...)

app.include_router(auth.router)        # = 컨트롤러 등록
app.include_router(strategies.router)
...
```

- `FastAPI(...)` = `@SpringBootApplication` 으로 뜨는 앱 컨텍스트.
- `add_middleware` = 서블릿 필터 / `WebMvcConfigurer`. 여기선 CORS 설정.
- `include_router` = `@RestController` 들을 스캔해 등록하는 것. **Spring 은
  컴포넌트 스캔으로 자동 등록**하지만, FastAPI 는 **명시적으로 router 를 붙인다.**
- `lifespan` = 앱 시작/종료 훅 (`@PostConstruct` / `@PreDestroy` + `ApplicationRunner`).
  여기선 종료 시 Redis 연결을 닫는다.

### `/health` 엔드포인트

```python
@app.get("/health")
async def health():
    return {"status": "ok", "redis": await redis_client.ping(), ...}
```

Spring Actuator `/actuator/health` 의 수제 버전. dict 를 리턴하면 JSON 으로 자동 직렬화.

---

## 2. 컨트롤러 — `APIRouter`

```python
# backend/app/api/routes/auth.py
router = APIRouter(prefix="/api/auth", tags=["auth"])

@router.post("/register", response_model=UserOut, status_code=201)
async def register(payload: UserRegister, db: AsyncSession = Depends(get_db)):
    ...
```

| 이 코드 | Spring 등가물 |
|---------|----------------|
| `APIRouter(prefix="/api/auth")` | `@RequestMapping("/api/auth")` (클래스 레벨) |
| `@router.post("/register")` | `@PostMapping("/register")` |
| `response_model=UserOut` | 반환 타입 + 직렬화 뷰. **응답을 이 스키마로 강제 필터링** |
| `status_code=201` | `@ResponseStatus(CREATED)` |
| `payload: UserRegister` | `@RequestBody UserRegister payload` (자동 검증) |
| `tags=["auth"]` | Swagger 그룹 태그 |

`response_model` 은 강력하다: 핸들러가 `User`(비밀번호 해시 포함)를 리턴해도,
`UserOut` 에 선언된 필드만 응답에 나간다. **민감 필드 누출 방지가 타입으로 보장**된다.

---

## 3. 의존성 주입 — `Depends`

Spring 의 `@Autowired` 가 "타입으로 빈을 찾아 넣는다" 면, FastAPI 의 `Depends` 는
**"이 함수를 먼저 실행해서 그 리턴값을 파라미터에 넣는다"** 이다.

```python
# backend/app/api/deps.py
async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    sid = request.cookies.get(SESSION_COOKIE)      # 쿠키에서 세션 ID
    user_id = await get_session_user_id(sid)       # Redis 조회
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(401, ...)
    return user
```

이걸 컨트롤러에서:

```python
@router.get("/me")
async def me(current: User = Depends(get_current_user)):
    return _user_out(current)
```

→ `me` 가 실행되기 **전에** `get_current_user` 가 돌고, 인증 실패면 401 을 던져
핸들러는 시작도 안 한다. **Spring Security 의 인증 필터 + `@AuthenticationPrincipal`
을 한 줄로 합친 것**이다.

`Depends` 는 중첩된다: `get_current_user` 자신이 `Depends(get_db)` 로 DB 세션을
받는다. 의존성 그래프를 FastAPI 가 알아서 위상정렬해 실행한다.

---

## 4. 엔티티 — SQLAlchemy 2.0

```python
# backend/app/models/models.py
class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    broker: Mapped[str] = mapped_column(String(16), default="kis")
    strategies: Mapped[list["Strategy"]] = relationship(back_populates="user")
```

| 이 코드 | JPA 등가물 |
|---------|------------|
| `class User(Base)` | `@Entity class User` |
| `__tablename__` | `@Table(name=...)` |
| `Mapped[int] = mapped_column(primary_key=True)` | `@Id @Column private Long id` |
| `relationship(back_populates=...)` | `@OneToMany(mappedBy=...)` |
| `Numeric(18, 4)` 사용 | 금액은 `BigDecimal` 처럼 — **float 금지** (오차) |

> 금액/수량은 전부 `Numeric`(Python `Decimal`)이다. 부동소수점 `float` 로 돈을
> 계산하면 0.1+0.2≠0.3 류 오차가 누적된다. Java 에서 돈에 `BigDecimal` 쓰는 것과 동일.

### 마이그레이션 = Alembic

`backend/alembic/versions/*.py` 가 Flyway 의 `V1__init.sql` 에 해당. 적용:

```bash
docker compose exec web alembic upgrade head
```

`price_ticks` 같은 TimescaleDB hypertable 은 마이그레이션 안에서 특별 처리된다.

---

## 5. DTO·검증 — Pydantic

```python
# backend/app/schemas/auth.py 류
class UserRegister(BaseModel):
    email: EmailStr
    password: str
```

- `BaseModel` = Java 의 `record` + Bean Validation(`@Valid`)을 합친 것.
- 요청 body 가 스키마에 안 맞으면 FastAPI 가 **자동으로 422** 를 돌려준다
  (직접 검증 코드 안 짬).
- `EmailStr` 같은 타입이 곧 제약조건.

`config.py` 의 `Settings` 도 Pydantic(`BaseSettings`)이다 → 환경변수를
**타입검증하며** 로드. 잘못된 값이면 **부팅이 실패**한다(잘못 뜬 채 도는 것보다 안전).

---

## 6. DB 세션과 트랜잭션

```python
# backend/app/core/database.py
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session                # 핸들러에 세션을 빌려줌
        except Exception:
            await session.rollback()     # 예외 시 롤백
            raise
```

- 요청 1건당 세션 1개 (Spring 의 OSIV/`EntityManager` per-request 와 같은 결).
- **자동 커밋이 아니다.** 라우트 끝에서 `await db.commit()` 을 직접 호출한다.
  `auth.py` 의 `register` 를 보면 `db.add(user)` 후 `await db.commit()` 이 명시적.
- `db.scalar(select(...))` = 단건 조회, `db.scalars(...)` = 여러 건, `db.execute(...)`
  = 원시 결과. JPQL/Criteria 대신 **SQLAlchemy select 식**을 쓴다.

---

## 7. async/await 실전 규칙 (가장 헷갈리는 부분)

1. **I/O 앞엔 `await`**: `await db.scalar(...)`, `await redis_client.get(...)`,
   `await client.post(...)`. 빠뜨리면 코루틴 객체가 그대로 흘러 조용히 깨진다.
2. **CPU 무거운 건 스레드풀로**: 백테스트(vectorbt)는 이벤트 루프를 막으므로
   `await run_in_threadpool(run_backtest, ...)` (backtests.py) 또는
   `await asyncio.to_thread(load_ohlcv, ...)` (runner.py) 로 던진다.
   → Spring 의 `@Async` / 별도 스레드풀로 블로킹 작업 빼는 것과 같은 동기.
3. **여러 비동기를 동시에**: `asyncio.create_task(...)` + `asyncio.wait(...)`
   (ws.py 의 relay/drain 두 태스크 동시 대기가 좋은 예).

---

## 다음 단계

문법을 번역했으니, 이제 **요청 1건이 실제로 흐르는 길**(인증→세션→DB)을 따라가자.

→ [04-request-lifecycle.md](04-request-lifecycle.md)

### 직접 열어볼 파일
- `backend/app/api/routes/auth.py` — 가장 읽기 쉬운 컨트롤러. 여기서 시작.
- `backend/app/core/database.py` — 20줄. 세션 의존성의 전부.
- `backend/app/schemas/` — DTO 들이 어떻게 생겼는지.
