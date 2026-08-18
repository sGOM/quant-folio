"""app/api/routes/auth.py 라우트 핸들러(register/login/logout/me/update_me) +
app/api/deps.get_current_user 검증(§신규발굴 12차 1위).

브루트포스 방어 내부 헬퍼(_login_blocked 등, test_login_bruteforce.py)와 세션
저장소 자체(test_session.py)는 이미 검증됐지만, 이를 실제로 엮어 쓰는 라우트
핸들러와 모든 인증 요청이 거치는 get_current_user 의존성은 한 번도 실행된
적이 없었다. HTTP 계층 없이 핸들러 함수를 직접 호출하는 conftest 관례
(test_alerts_route.py)를 따르고, Redis 는 conftest.FakeRedis, DB 는
conftest.FakeDB/_Store 를 재사용한다.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

import app.api.deps as deps_mod
import app.api.routes.auth as auth_mod
import app.core.session as session_mod
from app.core.security import verify_password
from app.models import User
from app.schemas.auth import UserProfileUpdate, UserRegister
from tests.conftest import FakeDB, FakeRedis, _Store


def _make_db(*users: User) -> FakeDB:
    store = _Store()
    for u in users:
        store.rows.setdefault(User, []).append(u)
    return FakeDB(store)


def _user(id_: int, email: str, *, password: str = "hunter22") -> User:
    from app.core.security import hash_password

    u = User(email=email, password_hash=hash_password(password), broker="kis")
    u.id = id_
    return u


class _FakeRequest:
    """auth._client_ip 와 logout 이 읽는 최소 속성(headers/client/cookies)만 흉내낸다."""

    def __init__(self, *, headers=None, client_host="203.0.113.5", cookies=None):
        self.headers = headers or {}
        self.client = SimpleNamespace(host=client_host)
        self.cookies = cookies or {}


class _RegisterDb(FakeDB):
    """register() 전용 대역 — 실제 DB 라면 INSERT 시 server_default("kis")가 적용되고
    이어지는 refresh() 로 반영되는데, FakeDB.refresh() 는 no-op 이라 이를 흉내낸다.
    """

    async def refresh(self, obj) -> None:
        if getattr(obj, "broker", None) is None:
            obj.broker = "kis"


class _RaisingDb:
    """호출되면 즉시 실패하는 대역 — "DB 를 건드리지 않았다"를 증명하는 용도."""

    async def scalar(self, _stmt):
        raise AssertionError("브루트포스 차단 후에는 DB 조회가 발생하면 안 된다")


def _patch_redis(monkeypatch) -> FakeRedis:
    """로그인 카운터(auth 모듈)와 세션 저장소(session 모듈)가 같은 Redis 를 보게 한다."""
    redis = FakeRedis()
    monkeypatch.setattr(auth_mod, "redis_client", redis)
    monkeypatch.setattr(session_mod, "redis_client", redis)
    return redis


# ─────────────────────────── register ───────────────────────────


async def test_register_creates_user_with_hashed_password():
    db = _RegisterDb(_Store())
    payload = UserRegister(email="new@example.com", password="hunter22")

    out = await auth_mod.register(payload, db=db)

    assert out.email == "new@example.com"
    stored = db._store.all(User)[0]
    # 평문 저장 금지 — 원본 비밀번호 문자열이 해시에 그대로 남으면 안 되고,
    # verify_password 로만 검증 가능해야 한다.
    assert stored.password_hash != "hunter22"
    assert verify_password("hunter22", stored.password_hash) is True


async def test_register_conflicts_on_duplicate_email():
    existing = _user(1, "dup@example.com")
    db = _make_db(existing)
    payload = UserRegister(email="dup@example.com", password="hunter22")

    with pytest.raises(HTTPException) as exc_info:
        await auth_mod.register(payload, db=db)
    assert exc_info.value.status_code == 409
    assert len(db._store.all(User)) == 1  # 신규 유저가 추가되지 않았다


# ─────────────────────────── login ───────────────────────────


async def test_login_success_sets_cookie_and_resets_failure_counter(monkeypatch):
    redis = _patch_redis(monkeypatch)
    user = _user(1, "user@example.com", password="hunter22")
    db = _make_db(user)
    await auth_mod._record_login_failure("user@example.com", "203.0.113.5")  # 이전 실패 1회

    response = Response()
    form = SimpleNamespace(username="user@example.com", password="hunter22")
    out = await auth_mod.login(_FakeRequest(), response, form=form, db=db)

    assert out.email == "user@example.com"
    assert "qf_session=" in response.headers.get("set-cookie", "")
    assert f"{auth_mod._LOGIN_FAIL_PREFIX}email:user@example.com" not in redis.store


async def test_login_wrong_password_returns_401_and_increments_counter(monkeypatch):
    redis = _patch_redis(monkeypatch)
    user = _user(1, "user@example.com", password="hunter22")
    db = _make_db(user)

    form = SimpleNamespace(username="user@example.com", password="wrong-pw")
    with pytest.raises(HTTPException) as exc_info:
        await auth_mod.login(_FakeRequest(), Response(), form=form, db=db)

    assert exc_info.value.status_code == 401
    assert redis.store[f"{auth_mod._LOGIN_FAIL_PREFIX}email:user@example.com"] == "1"


async def test_login_unknown_email_returns_401_and_increments_counter(monkeypatch):
    redis = _patch_redis(monkeypatch)
    db = _make_db()

    form = SimpleNamespace(username="nobody@example.com", password="whatever")
    with pytest.raises(HTTPException) as exc_info:
        await auth_mod.login(_FakeRequest(), Response(), form=form, db=db)

    assert exc_info.value.status_code == 401
    assert redis.store[f"{auth_mod._LOGIN_FAIL_PREFIX}email:nobody@example.com"] == "1"


async def test_login_blocked_returns_429_without_touching_db(monkeypatch):
    redis = _patch_redis(monkeypatch)
    email = "victim@example.com"
    ip = "203.0.113.5"
    for _ in range(auth_mod._LOGIN_FAIL_LIMIT_EMAIL):
        await auth_mod._record_login_failure(email, ip)

    form = SimpleNamespace(username=email, password="whatever")
    with pytest.raises(HTTPException) as exc_info:
        await auth_mod.login(_FakeRequest(client_host=ip), Response(), form=form, db=_RaisingDb())

    assert exc_info.value.status_code == 429
    assert redis.published == []  # 세션 발급(Redis SET)도 일어나지 않았다


# ─────────────────────────── logout ───────────────────────────


async def test_logout_destroys_session_and_clears_cookie(monkeypatch):
    redis = _patch_redis(monkeypatch)
    sid = await session_mod.create_session(1)
    request = _FakeRequest(cookies={auth_mod.SESSION_COOKIE: sid})
    response = Response()

    await auth_mod.logout(request, response)

    assert await session_mod.get_session_user_id(sid) is None
    set_cookie = response.headers.get("set-cookie", "")
    assert auth_mod.SESSION_COOKIE in set_cookie


async def test_logout_is_noop_without_cookie(monkeypatch):
    _patch_redis(monkeypatch)
    request = _FakeRequest(cookies={})

    await auth_mod.logout(request, Response())  # 예외 없이 반환돼야 한다


# ─────────────────────────── me / update_me ───────────────────────────


async def test_me_returns_current_user():
    user = _user(1, "user@example.com")
    out = await auth_mod.me(current=user)
    assert out.id == 1
    assert out.email == "user@example.com"


async def test_update_me_trims_and_sets_display_name():
    user = _user(1, "user@example.com")
    db = _make_db(user)

    out = await auth_mod.update_me(
        UserProfileUpdate(display_name="  홍길동  "), current=user, db=db
    )

    assert out.display_name == "홍길동"
    assert user.display_name == "홍길동"


async def test_update_me_normalizes_blank_to_none():
    user = _user(1, "user@example.com")
    user.display_name = "기존닉네임"
    db = _make_db(user)

    out = await auth_mod.update_me(
        UserProfileUpdate(display_name="   "), current=user, db=db
    )

    assert out.display_name is None
    assert user.display_name is None


# ─────────────────────────── get_current_user (deps.py) ───────────────────────────


async def test_get_current_user_401_without_cookie():
    request = _FakeRequest(cookies={})
    with pytest.raises(HTTPException) as exc_info:
        await deps_mod.get_current_user(request, db=_RaisingDb())
    assert exc_info.value.status_code == 401


async def test_get_current_user_401_when_session_invalid(monkeypatch):
    _patch_redis(monkeypatch)
    request = _FakeRequest(cookies={deps_mod.SESSION_COOKIE: "does-not-exist"})

    with pytest.raises(HTTPException) as exc_info:
        await deps_mod.get_current_user(request, db=_RaisingDb())
    assert exc_info.value.status_code == 401


async def test_get_current_user_401_when_user_missing_in_db(monkeypatch):
    _patch_redis(monkeypatch)
    sid = await session_mod.create_session(999)  # DB 에는 없는 user_id
    request = _FakeRequest(cookies={deps_mod.SESSION_COOKIE: sid})
    db = _make_db()  # 빈 유저 테이블

    with pytest.raises(HTTPException) as exc_info:
        await deps_mod.get_current_user(request, db=db)
    assert exc_info.value.status_code == 401


async def test_get_current_user_returns_user_for_valid_session(monkeypatch):
    _patch_redis(monkeypatch)
    user = _user(1, "user@example.com")
    db = _make_db(user)
    sid = await session_mod.create_session(1)
    request = _FakeRequest(cookies={deps_mod.SESSION_COOKIE: sid})

    result = await deps_mod.get_current_user(request, db=db)

    assert result is user
