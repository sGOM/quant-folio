"""GET /api/backtest/fill-quality 의 읽기 전용 캘리브레이션 제안 필드(calibration) 검증.

compute_fill_quality_report 를 대역화해 리포트 내용을 통제하고, get_fill_quality 핸들러가
그 위에 propose_slippage_calibration 을 읽기 전용으로 얹어 "calibration" 키를 반환하는지,
그리고 이 과정에서 Strategy.config 가 절대 변경되지 않는지 확인한다. DB·네트워크 불필요.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.api.routes.fill_quality import get_fill_quality


def _report(*, n, median, current_bps=5.0):
    return {
        "m1_exec": {"all": {"n": n, "median": median}},
        "assumptions": {"backtest_slip_bps": current_bps},
        "sample": {"n_orders": n},
        "grades": {"m1_exec": "GREEN", "m3_total": "GREEN"},
        "window": {},
    }


class _FakeSession:
    """get_fill_quality 가 strategy_id 지정 시 소유권 확인에 쓰는 db.scalar 만 대역한다."""

    def __init__(self, strategy):
        self._strategy = strategy

    async def scalar(self, _query):
        return self._strategy


def _patch_report(monkeypatch, report):
    import app.api.routes.fill_quality as fq

    async def _fake(db, user_id, **kwargs):  # noqa: ARG001
        return report

    monkeypatch.setattr(fq, "compute_fill_quality_report", _fake)


async def test_calibration_present_when_sample_sufficient(monkeypatch):
    # 현재 5bp, 관측 중앙값 20bp(표본 50) → 유의 변화 → +3bp 클램프 → 8bp 제안.
    _patch_report(monkeypatch, _report(n=50, median=20.0, current_bps=5.0))
    current = SimpleNamespace(id=1, email="tester@example.com")
    db = _FakeSession(None)

    res = await get_fill_quality(current=current, db=db)

    assert res["calibration"] is not None
    calib = res["calibration"]
    assert calib["current_bps"] == 5.0
    assert calib["proposed_bps"] == 8.0
    assert calib["observed_median_bps"] == 20.0
    assert calib["sample_size"] == 50
    assert calib["clamped"] is True
    assert isinstance(calib["reason"], str) and calib["reason"]


async def test_calibration_null_when_sample_insufficient(monkeypatch):
    # 표본 5 (< min_sample 기본 30) → 제안 없음.
    _patch_report(monkeypatch, _report(n=5, median=20.0, current_bps=5.0))
    current = SimpleNamespace(id=1, email="tester@example.com")
    db = _FakeSession(None)

    res = await get_fill_quality(current=current, db=db)

    assert res["calibration"] is None


async def test_calibration_null_when_change_insignificant(monkeypatch):
    # 표본 충분하지만 관측(5.5bp)이 현재값(5.0bp)과 거의 같음(<1bp) → 제안 없음.
    _patch_report(monkeypatch, _report(n=50, median=5.5, current_bps=5.0))
    current = SimpleNamespace(id=1, email="tester@example.com")
    db = _FakeSession(None)

    res = await get_fill_quality(current=current, db=db)

    assert res["calibration"] is None


async def test_get_does_not_mutate_strategy_config(monkeypatch):
    """GET 은 읽기 전용이어야 한다 — 유의한 제안이 있어도 Strategy.config 를 바꾸지 않는다."""
    strat = SimpleNamespace(id=23, user_id=1, config={"slippage_bps": 5.0, "top_n": 20})
    db = _FakeSession(strat)
    current = SimpleNamespace(id=1, email="tester@example.com")
    # 유의한 변화(제안 8bp)가 있는 리포트를 반환하도록 대역.
    _patch_report(monkeypatch, _report(n=50, median=20.0, current_bps=5.0))

    config_before = dict(strat.config)
    res = await get_fill_quality(
        strategy_id=23,
        current=current,
        db=db,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 3, 1),
    )

    assert res["calibration"] is not None
    assert res["calibration"]["proposed_bps"] == 8.0
    # config 는 호출 전후로 완전히 동일해야 한다(GET 은 부작용 없음).
    assert strat.config == config_before
    assert strat.config["slippage_bps"] == 5.0
