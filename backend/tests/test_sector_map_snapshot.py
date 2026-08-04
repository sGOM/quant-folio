"""worker/tasks.py::snapshot_sector_map 실패 시 알림 발행 검증(§24).

분기 1회 배치라 조용히 실패하면 최소 3개월간 아무도 모른 채 PIT 업종 스냅샷이
스테일 상태로 남을 수 있었다(§20 판정 근거로 쓰인 데이터). user_id=None +
severity="warning" 조합은 publish_alert 가 WS·텔레그램을 건너뛰고 DB 영속화만
시도하므로(test_alerts_cleanup.py 와 동일 사유), 로그로 발행 여부를 확인한다.
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


async def test_snapshot_failure_publishes_alert_and_reraises(monkeypatch, caplog):
    from app.core import database
    from app.services.data import krx_index
    from worker import tasks

    monkeypatch.setattr(database, "AsyncSessionLocal", _FakeSessionBoom())

    async def _boom(_db):
        raise RuntimeError("KRX 조회 실패")

    monkeypatch.setattr(krx_index, "snapshot_sector_map", _boom)
    monkeypatch.setattr("redis.asyncio.Redis", _FakeRedisCls)

    with caplog.at_level(logging.WARNING, logger="engine.alerts"):
        with pytest.raises(RuntimeError, match="KRX 조회 실패"):
            await tasks._snapshot_sector_map_async()

    assert "sector_map_outage" in caplog.text


class _FakeSnapshotDB:
    """krx_index.snapshot_sector_map 이 sector_map() 을 부르기 전에 필요한 최소 대역.

    이 count(*) 스칼라 조회 이후 바로 sector_map() 이 raise 하므로 그 뒤 메서드
    (execute/add_all/flush)는 호출되지 않아야 한다 — 테스트가 그걸 확인한다.
    """

    def __init__(self):
        self.touched_after_failure = False

    async def scalar(self, stmt):
        return 0  # existing=0 → 스킵 분기를 타지 않고 sector_map() 호출까지 진행

    async def execute(self, stmt):
        self.touched_after_failure = True
        return None

    def add_all(self, objs):
        self.touched_after_failure = True

    async def flush(self):
        self.touched_after_failure = True


async def test_snapshot_sector_map_propagates_data_source_error(monkeypatch):
    """sector_map() 이 전량 실패로 DataSourceError 를 올리면 snapshot_sector_map 은
    이를 삼키지 않고 그대로 전파해야 한다(Task 3 이후 계약 — §44-1 재발 방지).

    분기 Celery beat 태스크가 이 예외를 받아 알림을 발행할 수 있으려면 여기서 삼키면 안 된다.
    """
    from datetime import date

    from app.services.data import krx_index
    from app.services.data.errors import SourceUnavailableError

    def _boom():
        raise SourceUnavailableError("krx", "연결 실패")

    monkeypatch.setattr(krx_index, "sector_map", _boom)
    db = _FakeSnapshotDB()

    with pytest.raises(SourceUnavailableError):
        await krx_index.snapshot_sector_map(db, as_of=date(2026, 7, 1))

    assert not db.touched_after_failure  # 실패 이후 적재 시도로 넘어가지 않았다
