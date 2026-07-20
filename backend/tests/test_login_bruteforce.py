"""app/api/routes/auth.py 로그인 브루트포스 방어 로직 검증(§신규발굴 10차 1위).

`_login_blocked`/`_record_login_failure`/`_reset_login_failures`(Redis 고정
윈도우 기반 이메일·IP별 실패 카운터, 이메일당 10회/IP당 50회 임계로 429
차단)는 자금이 걸린 계정의 온라인 무차별 대입 방어(1차 방어선, bcrypt 오프라인
방어와 별개)인데 참조 테스트가 전무했다(`test_security.py`는 해싱만 검증).
임계 도달 경계값, 로그인 성공 시 이메일 카운터만 리셋되고 IP 카운터는 유지
되는 비대칭, Redis 장애 시 "차단 안 함"으로 안전하게 열리는 폴백(가용성
우선 설계)을 검증한다. conftest.FakeRedis 에 incr/expire 를 추가해 재사용.
"""
from __future__ import annotations

import app.api.routes.auth as auth_mod
from tests.conftest import FakeRedis

_EMAIL = "user@example.com"
_IP = "203.0.113.1"


async def test_login_not_blocked_when_no_failures_recorded(monkeypatch):
    monkeypatch.setattr(auth_mod, "redis_client", FakeRedis())
    assert await auth_mod._login_blocked(_EMAIL, _IP) is False


async def test_login_blocked_at_email_failure_limit_not_before(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(auth_mod, "redis_client", redis)

    for _ in range(auth_mod._LOGIN_FAIL_LIMIT_EMAIL - 1):
        await auth_mod._record_login_failure(_EMAIL, _IP)
    assert await auth_mod._login_blocked(_EMAIL, _IP) is False  # 9회 — 아직 미차단

    await auth_mod._record_login_failure(_EMAIL, _IP)
    assert await auth_mod._login_blocked(_EMAIL, _IP) is True  # 10회째 — 차단


async def test_login_blocked_by_ip_limit_across_different_emails(monkeypatch):
    """이메일을 바꿔가며 시도해도(다계정 스프레이) 같은 IP 실패가 누적되면 차단된다."""
    redis = FakeRedis()
    monkeypatch.setattr(auth_mod, "redis_client", redis)

    for i in range(auth_mod._LOGIN_FAIL_LIMIT_IP):
        await auth_mod._record_login_failure(f"victim{i}@example.com", _IP)

    # 개별 이메일은 임계 미도달이지만, 같은 IP 로 시도하는 새 이메일은 IP 카운터로 차단.
    assert await auth_mod._login_blocked("fresh@example.com", _IP) is True


async def test_record_login_failure_increments_both_email_and_ip_counters(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(auth_mod, "redis_client", redis)

    await auth_mod._record_login_failure(_EMAIL, _IP)
    await auth_mod._record_login_failure(_EMAIL, _IP)

    assert redis.store[f"{auth_mod._LOGIN_FAIL_PREFIX}email:{_EMAIL}"] == "2"
    assert redis.store[f"{auth_mod._LOGIN_FAIL_PREFIX}ip:{_IP}"] == "2"


async def test_reset_login_failures_clears_email_but_not_ip_counter(monkeypatch):
    """로그인 성공 시 이메일 카운터는 리셋되지만 IP 카운터는 윈도우 만료에 맡겨 유지된다."""
    redis = FakeRedis()
    monkeypatch.setattr(auth_mod, "redis_client", redis)

    await auth_mod._record_login_failure(_EMAIL, _IP)
    await auth_mod._record_login_failure(_EMAIL, _IP)
    await auth_mod._reset_login_failures(_EMAIL)

    assert f"{auth_mod._LOGIN_FAIL_PREFIX}email:{_EMAIL}" not in redis.store
    assert redis.store[f"{auth_mod._LOGIN_FAIL_PREFIX}ip:{_IP}"] == "2"


async def test_login_blocked_fails_open_when_redis_unavailable(monkeypatch):
    """Redis 조회 자체가 실패하면 로그인을 막지 않는다(가용성 우선 — 명시된 설계)."""
    class _BrokenRedis:
        async def get(self, k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(auth_mod, "redis_client", _BrokenRedis())
    assert await auth_mod._login_blocked(_EMAIL, _IP) is False


async def test_record_login_failure_swallows_redis_errors(monkeypatch):
    """실패 카운터 기록 자체가 실패해도 예외가 로그인 흐름으로 전파되지 않는다."""
    class _BrokenRedis:
        async def incr(self, k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(auth_mod, "redis_client", _BrokenRedis())
    await auth_mod._record_login_failure(_EMAIL, _IP)  # 예외 없이 반환돼야 한다.


async def test_reset_login_failures_swallows_redis_errors(monkeypatch):
    class _BrokenRedis:
        async def delete(self, k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(auth_mod, "redis_client", _BrokenRedis())
    await auth_mod._reset_login_failures(_EMAIL)  # 예외 없이 반환돼야 한다.
