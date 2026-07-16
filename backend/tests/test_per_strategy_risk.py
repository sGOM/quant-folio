"""전략별 일일 손실 한도 검사(engine.risk) 검증.

engine.risk.check_daily_loss_limit 은 두 개의 독립적 하드 게이트를 적용한다:
  1) 계좌 하드 게이트 — 사용자 공통(strategy_id=NULL) daily_loss_limit × 계좌 전체 손익.
  2) 전략 하드 게이트 — 명시적 per-strategy daily_loss_limit × 전략 스코프 손익.

핵심 검증 축(우선순위 순):
  - per-strategy 한도 미설정 시 결과가 기존과 동일(회귀 없음) — 최우선.
  - 전략 A 에만 낮은 한도 → A 는 전략 스코프 손실로 차단되지만 계좌 손실은 안 넘어
    다른 전략 B(한도 미설정)는 통과.
  - 계좌 공통 한도 초과 → 전략별 한도 유무와 무관하게 전 전략 차단.
  - strategy_id=NULL(비귀속) Position/Execution 은 계좌 스코프엔 포함되나 전략 스코프엔 제외.

conftest 의 FakeDB 를 재사용하되, 실현손익(Execution⨝Order) 조인 쿼리만 정확히
해석하도록 execute() 를 오버라이드한다(기본 FakeDB 는 이 조인을 빈 결과로 둔다).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.models import Execution, Order, OrderSide, Position, RiskLimit
from engine import risk

from tests.conftest import FakeDB, _ExecResult, _Store, _extract_predicates


# ─────────────────────────── 실현손익 조인을 해석하는 FakeDB ───────────────────────────
class _RiskFakeDB(FakeDB):
    """conftest.FakeDB 확장. _daily_pnl 의 실현손익 조인 쿼리를 정확히 해석한다.

    select(Execution, Order.side).join(Order).where(Order.user_id==u,
    Execution.executed_at>=start[, Execution.strategy_id==sid]) 를 인메모리 저장소에서
    재현해 (Execution, side) 튜플을 돌려준다. Position/RiskLimit 단순 쿼리는 부모 FakeDB
    가 그대로 처리한다(user_id/strategy_id eq, strategy_id IS NULL 지원).
    """

    async def execute(self, stmt):  # type: ignore[override]
        preds = _extract_predicates(stmt.whereclause)
        orders = {o.id: o for o in self._store.all(Order)}
        out = []
        for ex in self._store.all(Execution):
            order = orders.get(ex.order_id)
            if order is None:
                continue
            ok = True
            for col, op, val in preds:
                if col == "user_id":
                    if order.user_id != val:
                        ok = False
                        break
                elif col == "executed_at":
                    if not (ex.executed_at is not None and ex.executed_at >= val):
                        ok = False
                        break
                elif col == "strategy_id":
                    if ex.strategy_id != val:
                        ok = False
                        break
            if ok:
                out.append((ex, order.side))
        return _ExecResult(out)


# ─────────────────────────── 픽스처·헬퍼 ───────────────────────────
_USER = 1
_A = 101  # 전략 A
_B = 202  # 전략 B


@pytest.fixture
def store():
    return _Store()


@pytest.fixture
def db(store):
    return _RiskFakeDB(store)


def _add(store: _Store, obj):
    obj.id = store.next_id()
    store.rows.setdefault(type(obj), []).append(obj)
    return obj


def _position(store, *, strategy_id, symbol, qty, avg_price):
    return _add(
        store,
        Position(
            user_id=_USER,
            strategy_id=strategy_id,
            symbol=symbol,
            qty=Decimal(str(qty)),
            avg_price=Decimal(str(avg_price)),
        ),
    )


def _execution(store, *, strategy_id, side, qty, price, fee):
    """당일 체결 한 건(연결 Order 포함)을 만든다. executed_at 은 당일 시작 이후로 둔다."""
    order = _add(store, Order(user_id=_USER, strategy_id=strategy_id, side=side))
    return _add(
        store,
        Execution(
            order_id=order.id,
            strategy_id=strategy_id,
            filled_qty=Decimal(str(qty)),
            filled_price=Decimal(str(price)),
            fee=Decimal(str(fee)),
            executed_at=risk._today_start_utc() + timedelta(hours=1),
        ),
    )


def _limit(store, *, strategy_id, daily_loss_limit=None, max_position_size=None):
    return _add(
        store,
        RiskLimit(
            user_id=_USER,
            strategy_id=strategy_id,
            daily_loss_limit=None if daily_loss_limit is None else Decimal(str(daily_loss_limit)),
            max_position_size=None if max_position_size is None else Decimal(str(max_position_size)),
        ),
    )


# ─────────────────────────── _daily_pnl 스코프 검증 ───────────────────────────
async def test_daily_pnl_scope_excludes_unattributed(store, db):
    """비귀속(strategy_id=NULL) Position/Execution 은 계좌 스코프엔 포함, 전략 스코프엔 제외.

    포지션 평가손익:
      - A(101) AAA: (994-1000)*10 = -60
      - 비귀속     CCC: (990-1000)*10 = -100
    실현손익(둘 다 SELL 5주 @1000, 수수료 10):
      - A(101): 5*1000 - 10 = 4990
      - 비귀속 : 5*1000 - 10 = 4990
    """
    _position(store, strategy_id=_A, symbol="AAA", qty=10, avg_price=1000)
    _position(store, strategy_id=None, symbol="CCC", qty=10, avg_price=1000)
    _execution(store, strategy_id=_A, side=OrderSide.SELL, qty=5, price=1000, fee=10)
    _execution(store, strategy_id=None, side=OrderSide.SELL, qty=5, price=1000, fee=10)

    prices = {"AAA": Decimal("994"), "CCC": Decimal("990")}

    # 계좌 스코프: 비귀속 포함 → 실현 9980 + 미실현 -160 = 9820
    account = await risk._daily_pnl(db, _USER, prices)
    assert account == Decimal("9820")

    # 전략 스코프(A): A 귀속만 → 실현 4990 + 미실현 -60 = 4930 (비귀속 CCC/체결 제외)
    strat_a = await risk._daily_pnl(db, _USER, prices, strategy_id=_A)
    assert strat_a == Decimal("4930")

    # 전략 스코프(B): B 귀속 없음 → 0
    strat_b = await risk._daily_pnl(db, _USER, prices, strategy_id=_B)
    assert strat_b == Decimal("0")


# ─────────────────────────── 회귀: per-strategy 한도 미설정 ───────────────────────────
async def test_no_per_strategy_limit_matches_legacy_account_gate(store, db):
    """per-strategy 한도가 없을 때, 결과는 공통 한도 × 계좌 전체 손익과 정확히 일치(회귀 없음).

    공통 한도 100, per-strategy 한도 없음. 계좌 전체 손실을 -120 으로 만들어 초과 차단을,
    -80 케이스로 통과를 각각 확인한다.
    """
    _limit(store, strategy_id=None, daily_loss_limit=100)
    # A 포지션 손실 -120 (계좌 전체와 동일: 다른 포지션 없음)
    pos = _position(store, strategy_id=_A, symbol="AAA", qty=10, avg_price=1000)

    prices_block = {"AAA": Decimal("988")}  # (988-1000)*10 = -120 <= -100 → 차단
    account = await risk._daily_pnl(db, _USER, prices_block)
    assert account == Decimal("-120")
    decision = await risk.check_daily_loss_limit(db, _USER, _A, prices_block)
    assert decision.approved is False
    assert decision.reason == "일일 손실 한도 초과"

    # 손실을 -80 으로 완화하면 통과(한도 100 미도달)
    pos.avg_price = Decimal("1000")
    prices_ok = {"AAA": Decimal("992")}  # -80 > -100
    assert await risk._daily_pnl(db, _USER, prices_ok) == Decimal("-80")
    ok = await risk.check_daily_loss_limit(db, _USER, _A, prices_ok)
    assert ok.approved is True


async def test_no_limit_at_all_always_approved(store, db):
    """RiskLimit 자체가 없으면 손실이 아무리 커도 통과(기존 동작)."""
    _position(store, strategy_id=_A, symbol="AAA", qty=100, avg_price=1000)
    prices = {"AAA": Decimal("500")}  # -50000
    decision = await risk.check_daily_loss_limit(db, _USER, _A, prices)
    assert decision.approved is True


async def test_strategy_row_with_null_daily_loss_does_not_regress(store, db):
    """per-strategy 행이 있으나 daily_loss_limit=NULL 이면 기존처럼 daily loss 검사를 건너뛴다.

    공통 한도 100(계좌 -120 으로 초과 상태)이 있어도, A 에 daily_loss_limit=NULL 행(다른
    한도만 설정)이 있으면 기존 `_get_limit` 이 그 행을 우선 선택해 검사를 통째로 건너뛰던
    동작을 그대로 보존한다 → A 는 통과.
    """
    _limit(store, strategy_id=None, daily_loss_limit=100)
    _limit(store, strategy_id=_A, daily_loss_limit=None, max_position_size=5000)
    _position(store, strategy_id=_A, symbol="AAA", qty=10, avg_price=1000)
    prices = {"AAA": Decimal("988")}  # 계좌 -120 (공통 한도 100 초과 상태)

    # A 는 NULL 억제로 통과(회귀 보존)
    decision_a = await risk.check_daily_loss_limit(db, _USER, _A, prices)
    assert decision_a.approved is True

    # 같은 계좌라도 한도행이 없는 B 는 공통 게이트가 그대로 작동해 차단
    decision_b = await risk.check_daily_loss_limit(db, _USER, _B, prices)
    assert decision_b.approved is False
    assert decision_b.reason == "일일 손실 한도 초과"


# ─────────────────────────── 전략 하드 게이트 격리 ───────────────────────────
async def test_per_strategy_limit_isolates_strategy(store, db):
    """전략 A 에만 낮은 한도 → A 는 전략 스코프 손실로 차단, B(한도 미설정)는 통과.

    공통 한도 100000(계좌 게이트는 안 걸림). A 전용 daily_loss_limit=50.
      - A 포지션 AAA: -60 (전략 스코프)
      - B 포지션 BBB: +40
      - 계좌 전체: -20 (공통 100000 미도달)
    → A: 계좌 게이트 통과(-20 > -100000)하지만 전략 게이트 차단(-60 <= -50).
    → B: 전략 전용 한도 없음 → 계좌 게이트만, -20 > -100000 → 통과.
    """
    _limit(store, strategy_id=None, daily_loss_limit=100000)
    _limit(store, strategy_id=_A, daily_loss_limit=50)
    _position(store, strategy_id=_A, symbol="AAA", qty=10, avg_price=1000)
    _position(store, strategy_id=_B, symbol="BBB", qty=10, avg_price=1000)
    prices = {"AAA": Decimal("994"), "BBB": Decimal("1004")}

    # 스코프 손익 사전 검증
    assert await risk._daily_pnl(db, _USER, prices) == Decimal("-20")  # 계좌
    assert await risk._daily_pnl(db, _USER, prices, strategy_id=_A) == Decimal("-60")

    decision_a = await risk.check_daily_loss_limit(db, _USER, _A, prices)
    assert decision_a.approved is False
    assert decision_a.reason == "전략별 일일 손실 한도 초과"

    decision_b = await risk.check_daily_loss_limit(db, _USER, _B, prices)
    assert decision_b.approved is True


async def test_account_limit_blocks_all_strategies(store, db):
    """계좌 공통 한도 초과 → per-strategy 한도 유무·값과 무관하게 전 전략 차단.

    공통 한도 15. 계좌 전체 손익 -20. B 에는 높은 전용 한도(100000)가 있어 전략 게이트는
    안 걸리지만, 계좌 게이트가 독립적으로 작동해 B 도 차단되어야 한다(전략 한도로 계좌
    게이트 우회 불가).
    """
    _limit(store, strategy_id=None, daily_loss_limit=15)
    _limit(store, strategy_id=_B, daily_loss_limit=100000)
    _position(store, strategy_id=_A, symbol="AAA", qty=10, avg_price=1000)
    _position(store, strategy_id=_B, symbol="BBB", qty=10, avg_price=1000)
    prices = {"AAA": Decimal("994"), "BBB": Decimal("1004")}  # 계좌 -20 <= -15

    decision_a = await risk.check_daily_loss_limit(db, _USER, _A, prices)
    assert decision_a.approved is False
    assert decision_a.reason == "일일 손실 한도 초과"

    decision_b = await risk.check_daily_loss_limit(db, _USER, _B, prices)
    assert decision_b.approved is False
    assert decision_b.reason == "일일 손실 한도 초과"  # 계좌 게이트가 먼저 차단
