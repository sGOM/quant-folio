"""app/core/session.py 서버측 세션(Redis 기반) 로직 검증(§신규발굴 11차 1위).

모든 인증 요청이 거치는 보안 핵심 경로인데, `create_session`/
`get_session_user_id`/`destroy_session`을 직접 호출하는 테스트가 전무했다
(다른 라우트 테스트들이 간접적으로만 거쳤을 뿐). 슬라이딩 만료(조회 성공
시 TTL 갱신), 빈 sid 단락 처리, 파싱 불가한 저장값에 대한 방어적 None
반환, 존재하지 않는 sid 폐기의 무해성을 검증한다.
"""
from __future__ import annotations

import app.core.session as session_mod
from tests.conftest import FakeRedis


class _TrackingRedis(FakeRedis):
    """FakeRedis 확장 — expire 호출 인자를 기록해 슬라이딩 갱신 여부를 검증한다."""

    def __init__(self):
        super().__init__()
        self.expire_calls: list[tuple[str, int]] = []

    async def expire(self, k, seconds):
        self.expire_calls.append((k, seconds))
        return await super().expire(k, seconds)


async def test_create_session_stores_user_id_under_opaque_sid(monkeypatch):
    redis = _TrackingRedis()
    monkeypatch.setattr(session_mod, "redis_client", redis)

    sid = await session_mod.create_session(42)

    assert isinstance(sid, str) and len(sid) > 20  # 추측 불가 난수(불투명 ID)
    assert redis.store[session_mod._key(sid)] == "42"


async def test_get_session_user_id_returns_none_for_unknown_sid(monkeypatch):
    monkeypatch.setattr(session_mod, "redis_client", _TrackingRedis())
    assert await session_mod.get_session_user_id("does-not-exist") is None


async def test_get_session_user_id_short_circuits_on_empty_sid(monkeypatch):
    redis = _TrackingRedis()
    monkeypatch.setattr(session_mod, "redis_client", redis)

    assert await session_mod.get_session_user_id("") is None
    assert redis.expire_calls == []  # Redis 조회 자체를 시도하지 않는다.


async def test_get_session_user_id_renews_ttl_on_success(monkeypatch):
    """유효한 세션 조회 성공 시 TTL 을 갱신한다(슬라이딩 만료 — 활동 중 로그인 유지)."""
    redis = _TrackingRedis()
    monkeypatch.setattr(session_mod, "redis_client", redis)
    sid = await session_mod.create_session(7)

    user_id = await session_mod.get_session_user_id(sid)

    assert user_id == 7
    assert redis.expire_calls == [(session_mod._key(sid), session_mod._ttl_seconds())]


async def test_get_session_user_id_returns_none_for_unparseable_value(monkeypatch):
    """저장된 값이 정수로 파싱되지 않으면(데이터 오염 등) 예외 대신 None 을 반환한다."""
    redis = _TrackingRedis()
    monkeypatch.setattr(session_mod, "redis_client", redis)
    redis.store[session_mod._key("bad-sid")] = "not-an-int"

    assert await session_mod.get_session_user_id("bad-sid") is None
    # 파싱 실패 전에 이미 TTL 갱신은 시도된다(값 파싱과 무관하게 세션 자체는 유효했으므로).
    assert redis.expire_calls == [(session_mod._key("bad-sid"), session_mod._ttl_seconds())]


async def test_destroy_session_removes_stored_key(monkeypatch):
    redis = _TrackingRedis()
    monkeypatch.setattr(session_mod, "redis_client", redis)
    sid = await session_mod.create_session(1)

    await session_mod.destroy_session(sid)

    assert session_mod._key(sid) not in redis.store
    assert await session_mod.get_session_user_id(sid) is None


async def test_destroy_session_is_noop_for_empty_sid(monkeypatch):
    redis = _TrackingRedis()
    monkeypatch.setattr(session_mod, "redis_client", redis)

    await session_mod.destroy_session("")  # 예외 없이 반환돼야 한다.
    assert redis.store == {}
