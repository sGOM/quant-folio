"""슬리피지 캘리브레이션 피드백 루프 검증.

propose_slippage_calibration 의 표본 게이트·클램프·유의변화 게이트·정상 제안을 순수함수로
검증하고, apply 경로(apply_slippage_calibration)의 승인 반영·검증 거절을 DB 대역(FakeSession)과
compute_fill_quality_report mock 으로 확인한다(네트워크·실 DB 불필요).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.backtest.slippage_calibration import (
    DEFAULT_MAX_STEP_BPS,
    propose_slippage_calibration,
)


def _report(*, n, median, current_bps=5.0):
    """propose 가 읽는 최소 리포트 형태(m1_exec.all.{n,median} + assumptions)."""
    return {
        "m1_exec": {"all": {"n": n, "median": median}},
        "assumptions": {"backtest_slip_bps": current_bps},
    }


# ─────────────────────── propose: 표본 게이트 ───────────────────────


def test_insufficient_sample_returns_none():
    # 표본 29(<30)면 제안하지 않는다 — 관측이 크게 달라도 None.
    assert propose_slippage_calibration(_report(n=29, median=30.0)) is None


def test_missing_median_returns_none():
    assert propose_slippage_calibration(_report(n=100, median=None)) is None


# ─────────────────────── propose: 클램프 ───────────────────────


def test_clamp_upper_bound():
    # 현재 5bp, 관측 중앙값 20bp → +3bp 로 클램프 → 8bp.
    p = propose_slippage_calibration(_report(n=50, median=20.0, current_bps=5.0))
    assert p is not None
    assert p.proposed_bps == pytest.approx(8.0)
    assert 5.0 - DEFAULT_MAX_STEP_BPS <= p.proposed_bps <= 5.0 + DEFAULT_MAX_STEP_BPS
    assert p.clamped is True
    assert p.observed_median_bps == pytest.approx(20.0)
    assert p.sample_size == 50


def test_clamp_lower_bound_and_zero_floor():
    # 현재 2bp, 관측 -10bp → 하한 max(-1, 0)=0bp.
    p = propose_slippage_calibration(_report(n=50, median=-10.0, current_bps=2.0))
    assert p is not None
    assert p.proposed_bps == pytest.approx(0.0)
    assert p.clamped is True


# ─────────────────────── propose: 유의 변화 게이트 ───────────────────────


def test_below_min_change_returns_none():
    # 현재 5bp, 관측 5.5bp → 차이 0.5bp(<1) → 제안 생략.
    assert propose_slippage_calibration(_report(n=50, median=5.5, current_bps=5.0)) is None


def test_exactly_min_change_boundary_returns_none():
    # 차이 정확히 1bp 미만 판정: 5.9 → 0.9bp 차 → None.
    assert propose_slippage_calibration(_report(n=50, median=5.9, current_bps=5.0)) is None


# ─────────────────────── propose: 정상 제안 ───────────────────────


def test_normal_proposal_within_step():
    # 현재 5bp, 관측 7bp(±3 이내) → 제안 7bp, 클램프 안 걸림.
    p = propose_slippage_calibration(_report(n=40, median=7.0, current_bps=5.0))
    assert p is not None
    assert p.proposed_bps == pytest.approx(7.0)
    assert p.clamped is False
    assert p.current_bps == pytest.approx(5.0)


def test_downward_proposal():
    # 관측이 가정보다 낮으면 하향 제안(현재 8, 관측 4 → -3 클램프 → 5).
    p = propose_slippage_calibration(_report(n=40, median=4.0, current_bps=8.0))
    assert p is not None
    assert p.proposed_bps == pytest.approx(5.0)
    assert "하향" in p.reason


def test_default_current_when_assumption_missing():
    # assumptions 없으면 DEFAULT_SLIP_BPS(5.0)를 현재값으로 본다.
    rep = {"m1_exec": {"all": {"n": 40, "median": 9.0}}}
    p = propose_slippage_calibration(rep)
    assert p is not None
    assert p.current_bps == pytest.approx(5.0)
    assert p.proposed_bps == pytest.approx(8.0)  # 5+3 클램프


# ─────────────────────── apply 경로: DB 대역 ───────────────────────


class _FakeSession:
    """apply_slippage_calibration 가 쓰는 db.scalar/commit/refresh 만 대역한다."""

    def __init__(self, strategy):
        self._strategy = strategy
        self.committed = False

    async def scalar(self, _query):
        return self._strategy

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


def _patch_report(monkeypatch, report):
    import app.api.routes.fill_quality as fq

    async def _fake(db, user_id, **kwargs):  # noqa: ARG001
        return report

    monkeypatch.setattr(fq, "compute_fill_quality_report", _fake)


async def test_apply_updates_config(monkeypatch):
    from app.api.routes.fill_quality import apply_slippage_calibration

    strat = SimpleNamespace(id=23, user_id=1, config={"slippage_bps": 5.0, "top_n": 20})
    db = _FakeSession(strat)
    _patch_report(monkeypatch, _report(n=50, median=20.0, current_bps=5.0))  # 제안 8bp

    res = await apply_slippage_calibration(
        db, 1, strategy_id=23, proposed_bps=8.0, actor="tester@example.com",
    )
    assert db.committed is True
    assert strat.config["slippage_bps"] == pytest.approx(8.0)
    assert strat.config["top_n"] == 20  # 다른 키는 보존
    assert res["previous_bps"] == pytest.approx(5.0)
    assert res["applied_bps"] == pytest.approx(8.0)


async def test_apply_rejects_mismatched_value(monkeypatch):
    from app.api.routes.fill_quality import apply_slippage_calibration

    strat = SimpleNamespace(id=23, user_id=1, config={"slippage_bps": 5.0})
    db = _FakeSession(strat)
    _patch_report(monkeypatch, _report(n=50, median=20.0, current_bps=5.0))  # 제안 8bp

    with pytest.raises(HTTPException) as exc:
        await apply_slippage_calibration(
            db, 1, strategy_id=23, proposed_bps=12.0, actor="tester@example.com",
        )
    assert exc.value.status_code == 409
    assert db.committed is False
    assert strat.config["slippage_bps"] == pytest.approx(5.0)  # 미변경


async def test_apply_rejects_when_no_proposal(monkeypatch):
    from app.api.routes.fill_quality import apply_slippage_calibration

    strat = SimpleNamespace(id=23, user_id=1, config={"slippage_bps": 5.0})
    db = _FakeSession(strat)
    # 표본 부족 → 제안 없음 → 409.
    _patch_report(monkeypatch, _report(n=5, median=20.0, current_bps=5.0))

    with pytest.raises(HTTPException) as exc:
        await apply_slippage_calibration(
            db, 1, strategy_id=23, proposed_bps=8.0, actor="tester@example.com",
        )
    assert exc.value.status_code == 409
    assert db.committed is False


async def test_apply_404_when_strategy_missing(monkeypatch):
    from app.api.routes.fill_quality import apply_slippage_calibration

    db = _FakeSession(None)  # 소유 전략 없음
    _patch_report(monkeypatch, _report(n=50, median=20.0))

    with pytest.raises(HTTPException) as exc:
        await apply_slippage_calibration(
            db, 1, strategy_id=999, proposed_bps=8.0, actor="tester@example.com",
        )
    assert exc.value.status_code == 404
