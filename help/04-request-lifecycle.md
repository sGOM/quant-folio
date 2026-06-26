# 04. 요청 한 건의 일생 — 인증 · 세션 · DB

이 문서의 목표: **"브라우저가 보낸 요청 1건이 서버 안에서 어떤 경로를 지나는가"**
를 끝까지 추적하는 것. 인증/세션 설계가 Spring 과 가장 다른 지점이므로 집중해서 본다.

---

## 1. 인증 방식: JWT 가 아니라 "서버측 세션"

요즘 토이 프로젝트는 JWT 가 흔하지만, 이 프로젝트는 **Redis 기반 서버측 세션**을
택했다. 비교:

| | JWT (stateless) | 이 프로젝트: 서버측 세션 |
|---|---|---|
| 쿠키/토큰에 담기는 것 | 사용자 정보(payload) 자체 | **불투명한 난수 ID 만** |
| 진짜 정보 위치 | 토큰 안 (클라이언트가 들고 다님) | **서버(Redis)** |
| 로그아웃/강제만료 | 어렵다(만료까지 유효) | **Redis 키 삭제로 즉시 무효화** |
| XSS 로 토큰 탈취 시 | 정보까지 노출 | ID 만 노출(그 자체론 의미 없음) |

자금을 다루는 앱이라 **"즉시 차단 가능"** 이 중요해서 세션을 택한 것이다.
Spring Security 의 `HttpSession`(단, 저장소가 Redis)과 같은 모델이다.

### 세션의 자료구조 (`core/session.py`)

```
Redis:  key = "session:{sid}"   value = "{user_id}"   TTL = 14일(기본)
```

- `create_session(user_id)`: `secrets.token_urlsafe(32)` 로 추측 불가 ID 생성 →
  Redis 에 `user_id` 저장 → ID 반환.
- `get_session_user_id(sid)`: Redis 조회. **있으면 TTL 을 다시 늘린다(슬라이딩 만료)**
  → 활동 중이면 로그인 유지, 방치하면 자동 만료.
- `destroy_session(sid)`: 키 삭제(로그아웃).

---

## 2. 회원가입 → 로그인 → 인증요청 전 과정

### (a) 회원가입 `POST /api/auth/register`

```python
# routes/auth.py
exists = await db.scalar(select(User).where(User.email == payload.email))
if exists: raise HTTPException(409, "이미 등록된 이메일")
user = User(email=payload.email, password_hash=hash_password(payload.password))
db.add(user); await db.commit()
```

- 비밀번호는 `hash_password`(`core/security.py`)에서 **bcrypt + 사용자별 salt** 로
  해싱. 평문은 어디에도 저장/로깅하지 않는다. (Spring 의 `BCryptPasswordEncoder` 동일)
- 응답은 `UserOut` 으로 필터링 → `password_hash` 는 절대 안 나간다.

### (b) 로그인 `POST /api/auth/login`

```python
user = await db.scalar(select(User).where(User.email == form.username))
if user is None or not verify_password(form.password, user.password_hash):
    raise HTTPException(401, "이메일 또는 비밀번호가 올바르지 않습니다.")
await _start_session(response, user.id)     # 세션 생성 + 쿠키 발급
```

`_start_session` 이 쿠키를 굽는다:

```python
sid = await create_session(user_id)
response.set_cookie(SESSION_COOKIE, sid,
    httponly=True,                 # JS 가 못 읽음 → XSS 방어
    secure=settings.COOKIE_SECURE, # HTTPS 에서만 전송(prod)
    samesite=settings.COOKIE_SAMESITE,
    max_age=...)
```

> `OAuth2PasswordRequestForm` 때문에 로그인은 JSON 이 아니라 **form 데이터**이고,
> 이메일이 `username` 필드로 들어온다. Swagger 의 "Authorize" 버튼과 호환되는 관용구.

### (c) 이후 인증된 요청 (예: `GET /api/auth/me`)

```
브라우저 → 쿠키 qf_session=<sid> 자동 첨부
   → FastAPI 가 Depends(get_current_user) 실행:
        ① 쿠키에서 sid 추출
        ② Redis 에서 session:{sid} → user_id (+ TTL 갱신)
        ③ DB 에서 User 조회
        ④ 실패 시 401, 성공 시 User 객체를 핸들러에 주입
   → 핸들러 본문 실행
```

이 `get_current_user` 의존성을 붙이기만 하면 **그 엔드포인트는 인증 필수**가 된다.
인증을 코드 흐름이 아니라 **함수 시그니처로 선언**하는 게 FastAPI 스타일이다.

---

## 3. WebSocket 인증도 같은 세션을 쓴다

실시간 화면(`routes/ws.py`)은 같은 `qf_session` 쿠키로 인증한다:

```python
async def _authenticate(websocket):
    sid = websocket.cookies.get(SESSION_COOKIE)
    return await get_session_user_id(sid)     # REST 와 동일 로직 재사용
```

인증 실패면 `close(code=4401)`. 성공하면 `engine:events:{user_id}` 채널을 구독해
엔진 이벤트를 받아 브라우저로 중계한다. (세부는 [02 문서](02-architecture.md) 3-(b))

> **세션 ID 를 URL 쿼리스트링에 넣지 않는다** — 로그/리퍼러에 남을 수 있어서.
> 쿠키로만 주고받는다.

---

## 4. 민감정보 암호화: KIS 자격증명

사용자가 등록한 증권사 App Key/Secret 은 **로그인 비번과 다른 종류의 비밀**이다.
비번은 단방향 해시(검증만 하면 됨)지만, 증권사 키는 **나중에 복호화해서 실제 API
호출에 써야** 하므로 양방향 **대칭키 암호화(Fernet)** 를 쓴다.

```python
# core/security.py
_fernet = Fernet(settings.CREDENTIAL_ENC_KEY.encode())
def encrypt_secret(plaintext): return _fernet.encrypt(...)   # 저장 시
def decrypt_secret(ciphertext): return _fernet.decrypt(...)  # 사용 시
```

- DB 의 `users.kis_app_key/secret` 컬럼엔 **암호문만** 저장된다.
- `CREDENTIAL_ENC_KEY` 를 잃어버리면 기존 자격증명을 영영 복호화 못 한다(주의).
- 이 키는 평문 `.env` 가 아니라 **시크릿 파일**(`secrets/*.txt` → `/run/secrets/*`)로
  주입한다(`config.py` 의 `_load_secret_files`). `docker inspect` 나 이미지 레이어에
  비밀이 안 남게 하기 위함.

---

## 5. 설정이 부팅을 막는다 — fail-fast

`config.py` 의 `Settings` 는 단순 로더가 아니라 **부팅 게이트**다:

- `APP_ENV=prod` 인데 `SECRET_KEY`/`CREDENTIAL_ENC_KEY` 가 없으면 → **부팅 거부**
- `prod` 인데 `COOKIE_SECURE=false` 면 → **부팅 거부** (쿠키 탈취 위험)
- `CREDENTIAL_ENC_KEY` 가 유효한 Fernet 키가 아니면 → **부팅 거부**
  (첫 암호화 때 터지는 것보다 부팅 때 막는 게 안전)
- `APP_ENV=dev` 면 누락 키를 임시 생성해 편의상 띄워준다(로컬 전용).

> "잘못 설정된 채로 떠서 운영 중에 터지는 것" 보다 "아예 안 뜨는 것" 이 안전하다는
> 철학. Spring 의 `@Validated @ConfigurationProperties` + 커스텀 검증과 같은 결.

---

## 6. 흐름 요약도

```
register → (bcrypt 해시) → users 테이블
login    → 비번검증 → create_session → Redis session:{sid} → Set-Cookie(HttpOnly)
요청     → Cookie → Depends(get_current_user) → Redis→user_id → DB→User → 핸들러
KIS키등록 → Fernet 암호화 → users.kis_app_key(암호문)
매매시   → Fernet 복호화 → KisClient 생성 → 실제 주문
```

---

## 다음 단계

인증된 사용자가 "전략 시작"을 누른 뒤 **엔진에서 벌어지는 일**로 들어가자.
이 프로젝트에서 가장 흥미로운 부분이다.

→ [05-trading-engine.md](05-trading-engine.md)

### 직접 열어볼 파일
- `backend/app/core/session.py` — 60줄. 세션의 전부.
- `backend/app/api/deps.py` — 인증 의존성.
- `backend/app/core/security.py` — 해싱 + 암호화.
- `backend/app/core/config.py` — 부팅 게이트(`_ensure_secrets`).
