"""`wait_for_db` — DB 기동 중이면 재시도하고, 시한을 넘기면 포기한다."""
import asyncpg

from worker import celery_app as mod


def _patch(monkeypatch, outcomes):
    calls = []

    class _Conn:
        async def close(self):
            pass

    async def fake_connect(dsn, timeout):
        calls.append(dsn)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if outcome is not None:
            raise outcome
        return _Conn()

    monkeypatch.setattr(mod.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    return calls


def test_기동_중이면_준비될_때까지_재시도한다(monkeypatch):
    starting = asyncpg.CannotConnectNowError("the database system is starting up")
    calls = _patch(monkeypatch, [starting, ConnectionRefusedError(), None])

    assert mod.wait_for_db() is True
    assert len(calls) == 3
    assert "+asyncpg" not in calls[0]  # asyncpg 는 SQLAlchemy 방언 접두를 모른다


def test_시한을_넘기면_예외_없이_False(monkeypatch):
    _patch(monkeypatch, [ConnectionRefusedError()])

    assert mod.wait_for_db(timeout=0) is False
