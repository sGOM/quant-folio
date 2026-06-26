"""인증 라우트 — 회원가입, 로그인, 로그아웃(서버측 세션)."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SESSION_COOKIE, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.security import hash_password, verify_password
from app.core.session import create_session, destroy_session
from app.models import User
from app.schemas.auth import UserOut, UserProfileUpdate, UserRegister
from app.services.broker import user_has_toss_quote

router = APIRouter(prefix="/api/auth", tags=["auth"])


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
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """이메일/비밀번호로 로그인하고 세션 쿠키를 발급한다."""
    # OAuth2 form 은 username 필드에 이메일을 받는다.
    user = await db.scalar(select(User).where(User.email == form.username))
    if user is None or not verify_password(form.password, user.password_hash):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "이메일 또는 비밀번호가 올바르지 않습니다."
        )
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
