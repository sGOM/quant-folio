"""야간 선적재 태스크 검증 — 외부는 전부 대역, 호출 여부와 실패 집계만 본다."""
from datetime import date

import pytest

from app.services.data.errors import SourceUnavailableError
from worker import tasks


def test_전날_확정분을_적재한다(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [("펀더멘털", lambda: called.append("fund")),
                     ("시가총액", lambda: called.append("cap"))],
    )

    out = tasks.ingest_daily_snapshots()

    assert called == ["fund", "cap"]
    assert out == {"date": "20260805", "ok": 2, "failed": 0}


def test_일부_실패해도_나머지를_계속한다(monkeypatch):
    """한 종류가 막혔다고 나머지 선적재를 포기하면 다음 백테스트가 그만큼 더 조회한다."""

    def _boom():
        raise SourceUnavailableError("krx", "차단")

    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [("펀더멘털", _boom), ("시가총액", lambda: None)],
    )

    out = tasks.ingest_daily_snapshots()

    assert out == {"date": "20260805", "ok": 1, "failed": 1}
