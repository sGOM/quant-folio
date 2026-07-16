"""체결 정합 정기 점검(B-2) 배치 태스크 검증 — DB·Redis·리포트 산출을 전부 대역화하고,
RED 등급일 때만 critical 알림(publish_alert)이 발화하는지만 확인한다.
"""
from __future__ import annotations

import worker.tasks as tasks_mod


class _FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, pairs):
        self._pairs = pairs

    async def execute(self, stmt):
        return _FakeRows(self._pairs)


class _FakeDbCtx:
    def __init__(self, pairs):
        self._pairs = pairs

    async def __aenter__(self):
        return _FakeDb(self._pairs)

    async def __aexit__(self, *a):
        return False


class _FakeRedis:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _report(m1_grade="GREEN", m3_grade="GREEN", m1_mean=4.0, assumption=5.0, drag=0.1):
    return {
        "grades": {"m1_exec": m1_grade, "m3_total": m3_grade},
        "m1_exec": {"all": {"mean": m1_mean}},
        "assumptions": {"backtest_slip_bps": assumption},
        "annualized_drag": {"drag_diff_pct_per_yr": drag},
    }


async def _run(monkeypatch, pairs, report_by_strategy):
    import app.api.routes.fill_quality as fq_route
    import app.core.database as db_mod
    import engine.alerts as alerts_mod
    import redis.asyncio as redis_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: _FakeDbCtx(pairs))

    async def fake_compute(db, user_id, *, d_from, d_to, strategy_id=None, **kw):
        return report_by_strategy[strategy_id]

    monkeypatch.setattr(fq_route, "compute_fill_quality_report", fake_compute)

    fake_redis = _FakeRedis()
    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(lambda cls, url: fake_redis))

    alerts: list[dict] = []

    async def fake_publish_alert(redis, *, user_id, strategy_id, severity, message, code=None):
        alerts.append(
            {"user_id": user_id, "strategy_id": strategy_id, "severity": severity, "code": code}
        )

    monkeypatch.setattr(alerts_mod, "publish_alert", fake_publish_alert)

    result = await tasks_mod._check_fill_quality_drift_async()
    return result, alerts, fake_redis


async def test_no_alert_when_all_grades_green(monkeypatch):
    pairs = [(1, 10), (1, 11)]
    reports = {10: _report(), 11: _report(m1_grade="AMBER")}  # AMBER 는 경보 대상 아님

    result, alerts, redis = await _run(monkeypatch, pairs, reports)

    assert result["checked"] == 2
    assert alerts == []
    assert redis.closed  # finally 블록에서 반드시 닫힘


async def test_critical_alert_fires_on_m1_red(monkeypatch):
    pairs = [(7, 20)]
    reports = {20: _report(m1_grade="RED", m1_mean=30.0)}

    result, alerts, _ = await _run(monkeypatch, pairs, reports)

    assert result["alerted"] == [20]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["user_id"] == 7
    assert a["strategy_id"] == 20
    assert a["severity"] == "critical"  # B-1 텔레그램 채널까지 타는 심각도
    assert a["code"] == "fill_quality_drift"


async def test_critical_alert_fires_on_m3_red(monkeypatch):
    pairs = [(7, 21)]
    reports = {21: _report(m3_grade="RED")}

    result, alerts, _ = await _run(monkeypatch, pairs, reports)

    assert result["alerted"] == [21]
    assert alerts[0]["severity"] == "critical"


async def test_report_failure_is_logged_and_skipped(monkeypatch, caplog):
    """리포트 산출 실패(예: 종가 조회 오류)는 예외를 전파하지 않고 다음 전략으로 넘어간다."""
    pairs = [(1, 30), (1, 31)]

    import app.api.routes.fill_quality as fq_route
    import app.core.database as db_mod
    import redis.asyncio as redis_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: _FakeDbCtx(pairs))

    async def fake_compute(db, user_id, *, d_from, d_to, strategy_id=None, **kw):
        if strategy_id == 30:
            raise RuntimeError("boom")
        return _report()

    monkeypatch.setattr(fq_route, "compute_fill_quality_report", fake_compute)
    monkeypatch.setattr(
        redis_mod.Redis, "from_url", classmethod(lambda cls, url: _FakeRedis())
    )

    with caplog.at_level("WARNING"):
        result = await tasks_mod._check_fill_quality_drift_async()

    assert result["checked"] == 2
    assert result["alerted"] == []
    assert "30" in caplog.text
