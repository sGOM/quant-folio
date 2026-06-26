"""서버측 세션 — Redis 기반 로그인 세션.

쿠키에는 불투명한 세션 ID(추측 불가 난수)만 담고, 실제 세션 데이터(user_id)는
서버(Redis)에 보관한다. JWT 와 달리 토큰 자체에 정보가 없어, 세션을 지우면
즉시 무효화된다(로그아웃·탈취 대응이 단순).

- 키: ``session:{sid}`` → 값: ``user_id`` (문자열)
- TTL: SESSION_TTL_MINUTES. 인증이 성공할 때마다 TTL 을 갱신해(슬라이딩 만료)
  활동 중인 사용자는 로그인 상태가 유지되고, 비활성 사용자는 자동 만료된다.
"""
import secrets

from app.core.channels import SESSION_PREFIX
from app.core.config import settings
from app.core.redis import redis_client


def _key(sid: str) -> str:
    return f"{SESSION_PREFIX}{sid}"


def _ttl_seconds() -> int:
    return settings.SESSION_TTL_MINUTES * 60


async def create_session(user_id: int) -> str:
    """새 세션을 생성하고 세션 ID(sid)를 반환한다.

    :param user_id: 로그인한 사용자 ID
    :return: 쿠키에 담을 불투명 세션 ID(URL-safe 난수)
    """
    sid = secrets.token_urlsafe(32)
    await redis_client.set(_key(sid), str(user_id), ex=_ttl_seconds())
    return sid


async def get_session_user_id(sid: str) -> int | None:
    """세션 ID 로 사용자 ID 를 조회한다. 유효하면 TTL 을 갱신(슬라이딩)한다.

    :param sid: 쿠키에서 읽은 세션 ID
    :return: 유효한 세션이면 user_id, 없거나 만료면 None
    """
    if not sid:
        return None
    raw = await redis_client.get(_key(sid))
    if raw is None:
        return None
    # 슬라이딩 만료 — 활동이 있으면 만료 시계를 다시 늦춘다.
    await redis_client.expire(_key(sid), _ttl_seconds())
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def destroy_session(sid: str) -> None:
    """세션을 즉시 폐기한다(로그아웃). 존재하지 않아도 무해하다."""
    if sid:
        await redis_client.delete(_key(sid))
