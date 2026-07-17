"""인증 라우트 — 회원가입, 로그인, 로그아웃(서버측 세션)."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SESSION_COOKIE, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import redis_client
from app.core.security import hash_password, verify_password
from app.core.session import create_session, destroy_session
from app.models import User
from app.schemas.auth import UserOut, UserProfileUpdate, UserRegister
from app.services.broker import user_has_toss_quote

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── 로그인 브루트포스 방어 ──
# 온라인 무차별 대입을 막는 1차 방어선. 같은 이메일(및 오리진 IP)로 실패가 누적되면
# 윈도우 동안 429 로 차단한다. 카운터는 Redis 고정 윈도우(INCR + EXPIRE)로 관리하며
# 로그인 성공 시 해당 이메일 카운터를 리셋한다. bcrypt 해싱(오프라인 방어)과 별개로
# 자금이 걸린 계정의 온라인 시도 자체를 제한하는 목적이다.
_LOGIN_FAIL_PREFIX = "login:fail:"
_LOGIN_FAIL_WINDOW = 15 * 60  # 초 — 실패 카운트 유지 시간
_LOGIN_FAIL_LIMIT_EMAIL = 10   # 윈도우 내 이메일당 허용 실패 횟수
_LOGIN_FAIL_LIMIT_IP = 50      # 윈도우 내 IP당 허용 실패 횟수(다계정 스프레이 방어)


def _client_ip(request: Request) -> str:
    """오리진 IP — 단일 출처 proxy(Caddy)가 붙이는 X-Forwarded-For 첫 항목 우선."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _login_blocked(email: str, ip: str) -> bool:
    """이메일/IP 실패 카운터가 임계를 넘었는지 확인한다(Redis 실패 시 차단 안 함)."""
    try:
        by_email = await redis_client.get(f"{_LOGIN_FAIL_PREFIX}email:{email}")
        by_ip = await redis_client.get(f"{_LOGIN_FAIL_PREFIX}ip:{ip}")
    except Exception:  # noqa: BLE001 — 카운터 조회 실패로 로그인 자체를 막지 않는다
        return False
    return (
        (by_email is not None and int(by_email) >= _LOGIN_FAIL_LIMIT_EMAIL)
        or (by_ip is not None and int(by_ip) >= _LOGIN_FAIL_LIMIT_IP)
    )


async def _record_login_failure(email: str, ip: str) -> None:
    """실패 카운터를 올린다(고정 윈도우: 최초 실패 시점부터 WINDOW 초 유지)."""
    try:
        for key in (f"{_LOGIN_FAIL_PREFIX}email:{email}", f"{_LOGIN_FAIL_PREFIX}ip:{ip}"):
            count = await redis_client.incr(key)
            if count == 1:
                await redis_client.expire(key, _LOGIN_FAIL_WINDOW)
    except Exception:  # noqa: BLE001
        pass


async def _reset_login_failures(email: str) -> None:
    """로그인 성공 — 이메일 카운터 리셋(IP 카운터는 윈도우 만료에 맡긴다)."""
    try:
        await redis_client.delete(f"{_LOGIN_FAIL_PREFIX}email:{email}")
    except Exception:  # noqa: BLE001
        pass


async def _start_session(response: Response, user_id: int) -> None:
    """새 세션을 만들고 세션 ID 를 HttpOnly 쿠키로 발급한다.

    쿠키에는 불투명한 세션 ID 만 담기므로 JS(XSS)가 세션을 읽어도 의미가 없고,
    세션 데이터는 서버(Redis)에만 존재한다.
    """
    sid = await create_session(user_id)
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=settings.SESSION_TTL_MINUTES * 60,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )


def _user_out(user: User) -> UserOut:
    """User 모델을 외부 응답 스키마(UserOut)로 변환한다(민감 필드 제외)."""
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        broker=user.broker,
        kis_account_no=user.kis_account_no,
        has_kis_credentials=bool(user.kis_app_key),
        has_toss_quote=user_has_toss_quote(user),
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: UserRegister, db: AsyncSession = Depends(get_db)):
    """이메일/비밀번호로 신규 사용자를 생성한다. 이메일 중복 시 409.

    비밀번호는 bcrypt + 사용자별 salt 로 해싱해 저장하며 평문은 보관하지 않는다.
    """
    exists = await db.scalar(select(User).where(User.email == payload.email))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 등록된 이메일입니다.")

    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _user_out(user)


@router.post("/login", response_model=UserOut)
async def login(
    request: Request,
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """이메일/비밀번호로 로그인하고 세션 쿠키를 발급한다.

    실패가 누적된 이메일/IP 는 일정 시간(429) 차단한다(브루트포스 방어).
    """
    # OAuth2 form 은 username 필드에 이메일을 받는다.
    email = form.username
    ip = _client_ip(request)
    if await _login_blocked(email, ip):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.",
        )
    user = await db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(form.password, user.password_hash):
        await _record_login_failure(email, ip)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "이메일 또는 비밀번호가 올바르지 않습니다."
        )
    await _reset_login_failures(email)
    await _start_session(response, user.id)
    return _user_out(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response):
    """현재 세션을 폐기(Redis 삭제)하고 쿠키를 제거한다."""
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        await destroy_session(sid)
    response.delete_cookie(SESSION_COOKIE, domain=settings.COOKIE_DOMAIN, path="/")


@router.get("/me", response_model=UserOut)
async def me(current: User = Depends(get_current_user)):
    return _user_out(current)


@router.patch("/me", response_model=UserOut)
async def update_me(
    payload: UserProfileUpdate,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """프로필(닉네임)을 갱신한다. 공백만/빈 문자열은 미설정(null)으로 정규화한다."""
    name = (payload.display_name or "").strip()
    current.display_name = name or None
    await db.commit()
    await db.refresh(current)
    return _user_out(current)
