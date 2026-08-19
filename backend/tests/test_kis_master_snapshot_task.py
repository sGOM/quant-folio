"""worker/tasks.py::snapshot_kis_stock_master 실패 시 알림 발행 검증.

매일 배치라 조용히 실패하면 다음날까지 kis_stock_master_snapshots 가 스테일
상태로 남을 수 있다. user_id=None + severity="warning" 조합은 publish_alert 가
WS·텔레그램을 건너뛰고 DB 영속화만 시도하므로(test_alerts_cleanup.py 와 동일
사유), 로그로 발행 여부를 확인한다.
"""
import logging

import pytest

from tests.conftest import FakeRedis


class _FakeSessionBoom:
    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def rollback(self) -> None:
        return None


class _FakeAsyncRedisConn(FakeRedis):
    async def aclose(self) -> None:
        return None


class _FakeRedisCls:
    last: "_FakeAsyncRedisConn | None" = None

    @classmethod
    def from_url(cls, _url):
        cls.last = _FakeAsyncRedisConn()
        return cls.last


async def test_kis_master_snapshot_failure_publishes_alert_and_reraises(monkeypatch, caplog):
    from app.core import database
    from app.services.data import kis_master
    from worker import tasks

    monkeypatch.setattr(database, "AsyncSessionLocal", _FakeSessionBoom())

    async def _boom(_db):
        raise RuntimeError("KIS 종목마스터 조회 실패")

    monkeypatch.setattr(kis_master, "snapshot_stock_master", _boom)
    monkeypatch.setattr("redis.asyncio.Redis", _FakeRedisCls)

    with caplog.at_level(logging.WARNING, logger="engine.alerts"):
        with pytest.raises(RuntimeError, match="KIS 종목마스터 조회 실패"):
            await tasks._snapshot_kis_stock_master_async()

    assert "kis_master_outage" in caplog.text
