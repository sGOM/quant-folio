"""리스크 관리 — 주문 전 검증. 손절·최대 포지션·일일 한도.

리스크 통과 못 하면 주문을 생성하지 않는다(안전 우선).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Execution, Order, OrderSide, Position, RiskLimit

logger = logging.getLogger("engine.risk")

# ─────────────────────────── 리스크 귀속 규칙(비귀속 포지션) ───────────────────────────
# strategy_id=NULL 인 Position/Execution 은 "비귀속"이다: 전략 삭제(ondelete SET NULL)로
# 소유 전략이 사라졌거나, 자동매매 엔진이 소유하지 않는 수동·레거시 포지션이다.
# 리스크 계산에서의 취급을 아래 두 스코프로 명확히 구분한다.
#   - 계좌 스코프(_daily_pnl(strategy_id=None)): 계좌 전체를 보호하는 하드 게이트이므로
#     비귀속 포지션/체결도 모두 합산한다(계좌 단위 손실은 누가 소유하든 계좌를 위협한다).
#   - 전략 스코프(_daily_pnl(strategy_id=X)): 정확히 X 에 귀속된 것만 합산하며,
#     비귀속(strategy_id=NULL) Position/Execution 은 제외된다(특정 전략의 손실이 아니다).


@dataclass
class RiskDecision:
    """리스크 평가 결과.

    :param approved: 주문 허용 여부
    :param qty: 허용 수량(매수는 산정 수량, 매도는 청산 수량)
    :param reason: 거부 사유(approved=False 일 때)
    """
    approved: bool
    qty: int
    reason: str = ""


async def evaluate_buy(
    db: AsyncSession,
    user_id: int,
    strategy_id: int,
    symbol: str,
    price: Decimal,
    desired_cash: Decimal,
) -> RiskDecision:
    """매수 리스크 평가. max_position_size 한도 내에서 수량을 산정."""
    if price <= 0:
        return RiskDecision(False, 0, "유효하지 않은 가격")

    limit = await _get_limit(db, user_id, strategy_id)
    cash = desired_cash
    if limit and limit.max_position_size is not None:
        # 이미 보유 중인 금액(전 전략 합산)을 빼고 남은 한도까지만
        held = await _aggregate_position(db, user_id, symbol)
        held_value = (held.qty * held.avg_price) if held else Decimal("0")
        remaining = limit.max_position_size - held_value
        if remaining <= 0:
            return RiskDecision(False, 0, "최대 포지션 한도 도달")
        cash = min(cash, remaining)

    qty = int(cash // price)
    if qty <= 0:
        return RiskDecision(False, 0, "주문 가능 수량 부족")
    return RiskDecision(True, qty, "")


async def evaluate_sell(
    db: AsyncSession, user_id: int, symbol: str, *, strategy_id: int | None = None
) -> RiskDecision:
    """매도 리스크 평가. 보유 수량 전량 청산.

    :param strategy_id: 지정 시 그 전략에 귀속된 포지션 수량만 청산 대상으로 삼는다.
        같은 종목을 여러 전략이 독립 보유할 수 있으므로(모델의 (user, strategy, symbol)
        유니크), 전략 러너의 청산은 반드시 자기 전략 수량으로 좁혀야 다른 전략의
        보유분을 함께 팔아버리지 않는다. None 이면 계좌 전체 합산 수량.
    """
    if strategy_id is not None:
        pos = await db.scalar(
            select(Position).where(
                Position.user_id == user_id,
                Position.strategy_id == strategy_id,
                Position.symbol == symbol,
            )
        )
        if pos is None or pos.qty <= 0:
            return RiskDecision(False, 0, "보유 수량 없음")
        return RiskDecision(True, int(pos.qty), "")
    held = await _aggregate_position(db, user_id, symbol)
    if held is None or held.qty <= 0:
        return RiskDecision(False, 0, "보유 수량 없음")
    return RiskDecision(True, int(held.qty), "")


async def check_daily_loss_limit(
    db: AsyncSession,
    user_id: int,
    strategy_id: int,
    current_prices: dict[str, Decimal],
) -> RiskDecision:
    """일일 손실 한도 검사. 한도 초과 시 approved=False (신규 진입 차단).

    당일 손익 = 당일 체결의 실현 현금흐름(매도수익 − 매수비용 − 수수료)
              + 현재 보유 포지션의 평가손익(현재가 기준).
    한도(daily_loss_limit)는 양수(허용 최대 손실액)이며, 손익 ≤ −한도이면 차단한다.

    두 개의 독립적 하드 게이트를 순서대로 적용한다(하나라도 걸리면 차단):

    1) 계좌 하드 게이트 — 사용자 공통(strategy_id=NULL) daily_loss_limit 을 **계좌 전체
       손익**(모든 전략 + 비귀속 포지션 합산)에 적용. 계좌를 통째로 보호한다. 이 게이트는
       특정 전략에 per-strategy 한도가 있든 없든 독립적으로 작동한다(전략 한도로 계좌 게이트를
       우회할 수 없다).
    2) 전략 하드 게이트 — 이 전략에 **명시적으로** 설정된 per-strategy daily_loss_limit 이
       있는 경우에만, 그 전략에 귀속된 손익만 합산한 **전략 스코프 손익**에 적용.

    회귀 안전(최우선): 해당 전략에 per-strategy 한도가 없거나(행 없음) daily_loss_limit 이
    NULL 이면 기존 동작과 100% 동일해야 한다.
      - per-strategy 행이 아예 없으면: 기존 `_get_limit` 은 공통 한도로 fallback 했으므로,
        공통 한도를 계좌 전체 손익에 적용하는 (1)의 결과가 기존과 동일하다.
      - per-strategy 행이 있으나 daily_loss_limit 이 NULL 이면: 기존 `_get_limit` 은 그 행을
        우선 선택해 daily loss 검사를 통째로 건너뛰었다. 이 동작을 보존하기 위해, 이 경우
        계좌 공통 게이트도 적용하지 않는다(suppress).
    """
    common = await _get_common_limit(db, user_id)
    strat = await _get_strategy_limit(db, user_id, strategy_id)

    # 회귀 보존: per-strategy 행이 존재하지만 daily_loss_limit 이 NULL 이면, 기존 코드가
    # 그 전략의 daily loss 검사를 통째로 건너뛰던 동작을 그대로 재현한다(계좌 게이트도 skip).
    suppress = strat is not None and strat.daily_loss_limit is None

    # (1) 계좌 하드 게이트: 공통 한도 × 계좌 전체 손익(strategy_id=None).
    if not suppress and common is not None and common.daily_loss_limit is not None:
        account_pnl = await _daily_pnl(db, user_id, current_prices)
        if account_pnl <= -common.daily_loss_limit:
            logger.warning(
                "일일 손실 한도 초과(계좌): 손익 %s <= -%s — 신규 진입 차단",
                account_pnl, common.daily_loss_limit,
            )
            return RiskDecision(False, 0, "일일 손실 한도 초과")

    # (2) 전략 하드 게이트: 명시적 per-strategy 한도 × 전략 스코프 손익(strategy_id 지정).
    if strat is not None and strat.daily_loss_limit is not None:
        strat_pnl = await _daily_pnl(db, user_id, current_prices, strategy_id=strategy_id)
        if strat_pnl <= -strat.daily_loss_limit:
            logger.warning(
                "일일 손실 한도 초과(전략 %s): 손익 %s <= -%s — 신규 진입 차단",
                strategy_id, strat_pnl, strat.daily_loss_limit,
            )
            return RiskDecision(False, 0, "전략별 일일 손실 한도 초과")

    return RiskDecision(True, 0, "")


def _today_start_utc() -> datetime:
    """당일(KST 거래일)의 시작 시각을 UTC 로 반환한다(체결 조회 경계용)."""
    # KST 자정을 기준으로 당일 범위를 잡는다(KRX 거래일 기준).
    from app.services.market import KST

    kst_midnight = datetime.combine(datetime.now(KST).date(), time.min, tzinfo=KST)
    return kst_midnight.astimezone(timezone.utc)


async def _daily_pnl(
    db: AsyncSession,
    user_id: int,
    current_prices: dict[str, Decimal],
    strategy_id: int | None = None,
) -> Decimal:
    """당일 손익(실현 현금흐름 + 보유 포지션 평가손익)을 계산한다.

    :param current_prices: 종목별 현재가(평가손익 계산용). 없는 종목은 평가에서 제외
    :param strategy_id: 스코프 선택.
        - None(기본): 계좌 전체 합산. 사용자의 모든 전략 + 비귀속(strategy_id=NULL)
          Position/Execution 을 포함한다(계좌 단위 하드 게이트용). 이 경우 동작은
          strategy_id 파라미터 도입 이전과 **완전히 동일**하다(회귀 없음).
        - int: 그 전략에 귀속된(Execution.strategy_id / Position.strategy_id 가 정확히
          일치하는) 것만 합산한다. 비귀속(strategy_id=NULL) Position/Execution 은
          **제외**된다(모듈 상단 "리스크 귀속 규칙" 주석 참조).
    :return: 당일 손익(양수=이익, 음수=손실)
    """
    start = _today_start_utc()
    # 실현 현금흐름: side 판별을 위해 Order 조인은 유지한다. 전략 스코프면 비정규화된
    # Execution.strategy_id 로 직접 좁힌다(Order 조인 없이도 되지만 side 가 필요하므로 유지).
    realized_stmt = (
        select(Execution, Order.side)
        .join(Order, Execution.order_id == Order.id)
        .where(Order.user_id == user_id, Execution.executed_at >= start)
    )
    if strategy_id is not None:
        realized_stmt = realized_stmt.where(Execution.strategy_id == strategy_id)
    rows = (await db.execute(realized_stmt)).all()

    realized = Decimal("0")
    for ex, side in rows:
        gross = ex.filled_qty * ex.filled_price
        if side == OrderSide.SELL:
            realized += gross - ex.fee
        else:  # BUY — 현금 유출
            realized -= gross + ex.fee

    # 보유 포지션 평가손익(현재가 제공된 종목만).
    # 계좌 스코프(strategy_id=None)에서는 의도적으로 strategy_id 로 좁히지 않는다 —
    # 일일 손실 한도는 계좌 전체를 보호하는 하드 게이트이므로 모든 전략 + 비귀속 포지션을
    # 합산해 판정한다. 전략 스코프에서는 해당 전략 귀속 포지션만 합산한다.
    pos_stmt = select(Position).where(Position.user_id == user_id)
    if strategy_id is not None:
        pos_stmt = pos_stmt.where(Position.strategy_id == strategy_id)
    unrealized = Decimal("0")
    positions = (await db.scalars(pos_stmt)).all()
    for pos in positions:
        cur = current_prices.get(pos.symbol)
        if cur is None or pos.qty <= 0:
            continue
        unrealized += (Decimal(str(cur)) - pos.avg_price) * pos.qty

    return realized + unrealized


async def check_stop_loss(
    db: AsyncSession,
    user_id: int,
    strategy_id: int,
    symbol: str,
    current_price: Decimal,
) -> bool:
    """손절 조건 도달 여부. avg_price 대비 stop_loss_pct 이상 하락 시 True."""
    limit = await _get_limit(db, user_id, strategy_id)
    if not limit or limit.stop_loss_pct is None:
        return False
    pos = await _aggregate_position(db, user_id, symbol)
    if pos is None or pos.qty <= 0 or pos.avg_price <= 0:
        return False
    drop = (pos.avg_price - current_price) / pos.avg_price
    if drop >= limit.stop_loss_pct:
        logger.info(
            "손절 조건 도달: %s 하락률 %.4f >= %.4f", symbol, drop, limit.stop_loss_pct
        )
        return True
    return False


async def _get_limit(db: AsyncSession, user_id: int, strategy_id: int) -> RiskLimit | None:
    """적용할 리스크 한도를 조회한다(전략별 한도 우선, 없으면 사용자 공통 한도).

    evaluate_buy(max_position_size)·check_stop_loss(stop_loss_pct) 에서 쓰인다.
    daily_loss_limit 검사는 계좌/전략 하드 게이트를 분리하기 위해 아래
    `_get_common_limit`·`_get_strategy_limit` 을 대신 사용한다.
    """
    return await db.scalar(
        select(RiskLimit)
        .where(RiskLimit.user_id == user_id)
        .where((RiskLimit.strategy_id == strategy_id) | (RiskLimit.strategy_id.is_(None)))
        .order_by(RiskLimit.strategy_id.desc().nullslast())
    )


async def _get_common_limit(db: AsyncSession, user_id: int) -> RiskLimit | None:
    """사용자 공통(strategy_id=NULL) 한도만 조회한다(전략 한도로 fallback 하지 않음)."""
    return await db.scalar(
        select(RiskLimit).where(
            RiskLimit.user_id == user_id,
            RiskLimit.strategy_id.is_(None),
        )
    )


async def _get_strategy_limit(
    db: AsyncSession, user_id: int, strategy_id: int
) -> RiskLimit | None:
    """이 전략에 **명시적으로** 설정된 한도만 조회한다(공통 한도로 fallback 하지 않음).

    반환값이 None 이면 이 전략 전용 한도가 없다는 뜻이며, 이 경우 전략 하드 게이트는
    작동하지 않는다(기존 동작과 동일).
    """
    return await db.scalar(
        select(RiskLimit).where(
            RiskLimit.user_id == user_id,
            RiskLimit.strategy_id == strategy_id,
        )
    )


@dataclass
class _AggregatePosition:
    """계좌 단위로 합산한 종목 익스포저(수량 합·수량가중 평균단가)."""
    qty: Decimal
    avg_price: Decimal


async def _aggregate_position(
    db: AsyncSession, user_id: int, symbol: str
) -> _AggregatePosition | None:
    """사용자의 특정 종목 포지션을 계좌 단위로 합산해 반환한다(없으면 None).

    같은 종목을 여러 전략이 독립 보유할 수 있으므로(모델의 (user, strategy, symbol)
    유니크) 단일 행 조회는 어느 전략의 행이 잡히는지 비결정적이다. 계좌 단위 리스크
    판정(최대 포지션 한도·손절 게이트)은 전 행을 합산하고 평균단가는 수량가중으로
    구해 결정적으로 판정한다.
    """
    rows = (
        await db.scalars(
            select(Position).where(
                Position.user_id == user_id, Position.symbol == symbol
            )
        )
    ).all()
    total_qty = sum((p.qty for p in rows), Decimal("0"))
    if not rows or total_qty <= 0:
        return None
    weighted = sum((p.qty * p.avg_price for p in rows), Decimal("0"))
    return _AggregatePosition(qty=total_qty, avg_price=weighted / total_qty)
