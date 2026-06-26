"""공통 의존성 — 현재 사용자 인증(서버측 세션).

세션 ID 는 HttpOnly 쿠키(qf_session)로만 주고받는다. 쿠키에는 불투명한
세션 ID 만 담기고, 실제 사용자 정보(user_id)는 Redis 에 보관된다.
"""
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.session import get_session_user_id
from app.models import User

# 쿠키 이름 — 프론트는 세션 ID 를 직접 다루지 않고 이 HttpOnly 쿠키만 전송한다.
SESSION_COOKIE = "qf_session"

_credentials_exc = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="자격 증명을 확인할 수 없습니다.",
)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """현재 요청의 세션 쿠키를 검증해 인증 사용자를 반환하는 FastAPI 의존성.

    쿠키(qf_session)에서 세션 ID 를 읽어 Redis 세션을 조회하고(유효하면 TTL
    슬라이딩 갱신), 해당 user_id 로 DB 에서 사용자를 조회한다.

    :param request: 세션 쿠키를 추출할 요청
    :param db: DB 세션
    :return: 인증된 User
    :raises HTTPException: 세션이 없거나 만료·무효이거나 사용자가 없으면 401
    """
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        raise _credentials_exc

    user_id = await get_session_user_id(sid)
    if user_id is None:
        raise _credentials_exc

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise _credentials_exc
    return user
