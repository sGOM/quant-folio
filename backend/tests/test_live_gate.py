"""실전(prod) 전환 게이트 검증 — app/services/live_gate.evaluate_live_gate.

세 조건(체결 정합 등급·주문 금액 상한·prod 2단계 승인)의 하드 게이트 동작을 검증한다.
compute_fill_quality_report 는 mock(정합 리포트 dict 주입)하고, DB 는 FakeSession 으로
대역해 네트워크·실 DB 없이 게이트 상태기계만 본다(test_engine_health 의 FakeRedis 방식,
test_fill_quality 의 report mock 방식 참고).
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import live_gate
from app.services.live_gate import evaluate_live_gate


# ─────────────────────── DB 대역(FakeSession) ───────────────────────
class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    """live_gate 가 쓰는 db.scalar(RiskLimit 조회)·db.execute(당일 체결 조회)만 대역한다."""

    def __init__(self, limit=None, executions=None):
        self._limit = limit
        self._executions = executions or []

    async def scalar(self, _query):
        return self._limit

    async def execute(self, _query):
        return _FakeResult(self._executions)


def _limit(max_position_size):
    """max_position_size 만 가진 RiskLimit 대역."""
    return SimpleNamespace(max_position_size=Decimal(str(max_position_size)))


def _execution(qty, price):
    return SimpleNamespace(filled_qty=Decimal(str(qty)), filled_price=Decimal(str(price)))


def _patch_report(monkeypatch, *, n=40, m1="GREEN", m3="GREEN"):
    """compute_fill_quality_report 를 고정 등급·표본수 dict 로 대체한다."""
    async def _fake(db, user_id, **kwargs):  # noqa: ARG001
        return {
            "sample": {"n_orders": n, "min_sample": 30},
            "grades": {"m1_exec": m1, "m3_total": m3},
        }

    monkeypatch.setattr(live_gate, "compute_fill_quality_report", _fake)


@pytest.fixture
def prod_env(monkeypatch):
    """실전(prod)·승인됨·상한 넉넉으로 설정한 기본 환경(개별 테스트가 덮어쓴다)."""
    monkeypatch.setattr(settings, "KIS_ENV", "prod")
    monkeypatch.setattr(settings, "KIS_PROD_APPROVED", True)
    monkeypatch.setattr(settings, "KIS_DAILY_ORDER_NOTIONAL_CAP", None)


# ─────────────────────── 정합(fill quality) 게이트 ───────────────────────


async def test_fill_quality_red_blocks(monkeypatch, prod_env):
    """M1 등급이 RED 면 실전 게이트 실패."""
    _patch_report(monkeypatch, n=40, m1="RED", m3="GREEN")
    res = await evaluate_live_gate(_FakeSession(), user_id=1, strategy_id=23)
    assert res.passed is False
    assert any("정합 등급 미달" in r for r in res.reasons)


async def test_fill_quality_insufficient_sample_blocks(monkeypatch, prod_env):
    """표본이 min_sample 미만이면 판단 불가로 보수적 차단."""
    _patch_report(monkeypatch, n=5, m1="GREEN", m3="GREEN")
    res = await evaluate_live_gate(_FakeSession(), user_id=1, strategy_id=23)
    assert res.passed is False
    assert any("표본 부족" in r for r in res.reasons)


# ─────────────────────── 주문 금액 상한 게이트 ───────────────────────


async def test_order_notional_exceeds_position_cap_blocks(monkeypatch, prod_env):
    """정합은 통과(GREEN)해도 1회 주문 금액이 max_position_size 초과면 차단."""
    _patch_report(monkeypatch, n=40, m1="GREEN", m3="GREEN")
    db = _FakeSession(limit=_limit(1_000_000))
    res = await evaluate_live_gate(
        db, user_id=1, strategy_id=23, order_notional=Decimal("1_500_000"),
    )
    assert res.passed is False
    assert any("최대 포지션 한도" in r for r in res.reasons)


async def test_daily_notional_cap_blocks(monkeypatch, prod_env):
    """당일 체결 누적 + 이번 주문이 일일 상한을 넘으면 차단."""
    _patch_report(monkeypatch, n=40, m1="GREEN", m3="GREEN")
    monkeypatch.setattr(settings, "KIS_DAILY_ORDER_NOTIONAL_CAP", Decimal("2_000_000"))
    # 당일 이미 1,800,000원 체결(600주 × 3,000원).
    db = _FakeSession(limit=_limit(10_000_000), executions=[_execution(600, 3000)])
    res = await evaluate_live_gate(
        db, user_id=1, strategy_id=23, order_notional=Decimal("500_000"),
    )
    assert res.passed is False
    assert any("일일 누적 주문 금액" in r for r in res.reasons)


# ─────────────────────── prod 2단계 승인 게이트 ───────────────────────


async def test_prod_not_approved_blocks(monkeypatch, prod_env):
    """prod 인데 승인 플래그가 꺼져 있으면 정합·금액이 좋아도 차단."""
    monkeypatch.setattr(settings, "KIS_PROD_APPROVED", False)
    _patch_report(monkeypatch, n=40, m1="GREEN", m3="GREEN")
    res = await evaluate_live_gate(_FakeSession(), user_id=1, strategy_id=23)
    assert res.passed is False
    assert any("KIS_PROD_APPROVED" in r for r in res.reasons)


# ─────────────────────── vts(모의투자)는 항상 통과 ───────────────────────


async def test_vts_always_passes(monkeypatch):
    """vts(모의투자)면 정합 RED·금액 초과여도 차단하지 않고 통과(로깅만)."""
    monkeypatch.setattr(settings, "KIS_ENV", "vts")
    monkeypatch.setattr(settings, "KIS_PROD_APPROVED", False)
    monkeypatch.setattr(settings, "KIS_DAILY_ORDER_NOTIONAL_CAP", Decimal("1000"))
    _patch_report(monkeypatch, n=5, m1="RED", m3="RED")
    db = _FakeSession(limit=_limit(1000), executions=[_execution(600, 3000)])
    res = await evaluate_live_gate(
        db, user_id=1, strategy_id=23, order_notional=Decimal("10_000_000"),
    )
    assert res.passed is True
    assert res.reasons == []
    # 로깅용으로는 실패 사유가 기록돼 있어야 한다.
    assert res.details["logged_failures"]


# ─────────────────────── 모든 조건 통과 ───────────────────────


async def test_all_conditions_pass(monkeypatch, prod_env):
    """정합 GREEN·표본 충분·금액 상한 이내·prod 승인이면 게이트 성공."""
    _patch_report(monkeypatch, n=40, m1="GREEN", m3="GREEN")
    monkeypatch.setattr(settings, "KIS_DAILY_ORDER_NOTIONAL_CAP", Decimal("100_000_000"))
    db = _FakeSession(limit=_limit(10_000_000), executions=[_execution(100, 3000)])
    res = await evaluate_live_gate(
        db, user_id=1, strategy_id=23, order_notional=Decimal("1_000_000"),
    )
    assert res.passed is True
    assert res.reasons == []
