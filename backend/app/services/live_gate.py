"""실전(prod) 전환 게이트 — 실거래 주문이 나가기 전 마지막 하드 게이트.

자동매매 엔진은 ``KIS_ENV``(vts=모의투자 / prod=실전)로 브로커를 전환한다. 실전은
실제 자금이 즉시 나가므로, 전략 버그·설정 실수가 계좌를 비우는 사고를 구조적으로
차단하기 위해 다음 세 조건을 순차 평가한다(하나라도 실패하면 주문·기동 거부).

  1. 체결 정합 실측(fill quality) 등급 — 백테스트 가정과 실거래 체결의 괴리.
     최근 리밸런스 표본의 M1(실행 슬리피지)/M3(총 정합 괴리) 등급이 RED 이면 차단.
     표본이 ``DEFAULT_MIN_SAMPLE`` 미만이면 "판단 불가"로 보수적으로 차단.
  2. 주문 금액 상한 — 1회 주문(RiskLimit.max_position_size)·일일 누적
     (``KIS_DAILY_ORDER_NOTIONAL_CAP``, 당일 체결 합계) 초과 차단.
  3. prod 2단계 승인 — ``KIS_PROD_APPROVED`` 플래그가 꺼져 있으면(기본값=False, 안전측)
     실전 주문 자체를 거부.

vts(모의투자)에서는 세 체크를 수행하되 로깅만 하고 항상 통과시킨다(차단하지 않음).
호출 측(engine)은 prod 일 때만 강제 호출하고 실패 시 주문을 막고 critical 알림을 보낸다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.fill_quality import compute_fill_quality_report
from app.core.config import settings
from app.models import Execution, Order, RiskLimit
from app.services.backtest.fill_quality import DEFAULT_MIN_SAMPLE
from app.services.market import KST

logger = logging.getLogger("live_gate")

# 체결 정합 실측 조회 윈도우(일). 최근 리밸런스 표본을 넉넉히 포함하도록 잡는다.
_FILL_QUALITY_LOOKBACK_DAYS = 90

# 실전 차단으로 취급하는 정합 등급(판단 불가·명백한 미달).
_BLOCKING_GRADES = {"RED"}


@dataclass
class GateResult:
    """실전 게이트 평가 결과.

    :param passed: 통과 여부(False 면 주문·기동을 거부해야 한다).
    :param reasons: 실패 사유 목록(passed=False 일 때 채워진다). vts 에서는 항상 비어 있다.
    :param details: 진단용 부가 정보(등급·표본수·모의투자 여부 등).
    """
    passed: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


async def _get_limit(
    db: AsyncSession, user_id: int, strategy_id: int
) -> RiskLimit | None:
    """적용할 리스크 한도를 조회한다(전략별 한도 우선, 없으면 사용자 공통 한도).

    engine.risk._get_limit 과 동일 규칙(app 레이어가 engine 을 역참조하지 않도록 재구성).
    """
    return await db.scalar(
        select(RiskLimit)
        .where(RiskLimit.user_id == user_id)
        .where((RiskLimit.strategy_id == strategy_id) | (RiskLimit.strategy_id.is_(None)))
        .order_by(RiskLimit.strategy_id.desc().nullslast())
    )


def _today_start_utc() -> datetime:
    """당일(KST 거래일)의 시작 시각을 UTC 로 반환한다(체결 조회 경계용)."""
    kst_midnight = datetime.combine(datetime.now(KST).date(), time.min, tzinfo=KST)
    return kst_midnight.astimezone(timezone.utc)


async def _daily_executed_notional(db: AsyncSession, user_id: int) -> Decimal:
    """당일 체결 주문의 총 체결금액(매수+매도 절대 명목가) 합계를 계산한다.

    engine.risk._daily_pnl 과 동일한 Execution⋈Order 조인 패턴을 따르되, 손익이 아닌
    '거래 규모'(filled_qty × filled_price)를 방향 무관하게 합산해 일일 누적 상한과 비교한다.
    """
    start = _today_start_utc()
    rows = (
        await db.execute(
            select(Execution)
            .join(Order, Execution.order_id == Order.id)
            .where(Order.user_id == user_id, Execution.executed_at >= start)
        )
    ).scalars().all()
    total = Decimal("0")
    for ex in rows:
        total += ex.filled_qty * ex.filled_price
    return total


async def _check_fill_quality(
    db: AsyncSession, user_id: int, strategy_id: int, failures: list[str],
    details: dict[str, Any],
) -> None:
    """체결 정합 실측 등급을 확인해 미달·표본부족이면 failures 에 사유를 추가한다."""
    d_to = datetime.now(KST).date()
    d_from = d_to - timedelta(days=_FILL_QUALITY_LOOKBACK_DAYS)
    report = await compute_fill_quality_report(
        db, user_id,
        d_from=d_from, d_to=d_to, strategy_id=strategy_id,
        min_sample=DEFAULT_MIN_SAMPLE,
    )
    sample = report.get("sample", {}) or {}
    grades = report.get("grades", {}) or {}
    n = int(sample.get("n_orders", 0) or 0)
    min_sample = int(sample.get("min_sample", DEFAULT_MIN_SAMPLE) or DEFAULT_MIN_SAMPLE)
    m1 = grades.get("m1_exec")
    m3 = grades.get("m3_total")
    details["fill_quality"] = {"n_orders": n, "min_sample": min_sample, "m1": m1, "m3": m3}

    # 표본 부족 → 판단 불가로 보수적 차단(정합이 좋은지 나쁜지 알 수 없다).
    if n < min_sample:
        failures.append(
            f"체결 정합 표본 부족(n={n} < {min_sample}) — 판단 불가로 실전 차단"
        )
        return
    # 표본이 충분한데 등급이 RED(명백한 괴리)면 차단.
    if m1 in _BLOCKING_GRADES or m3 in _BLOCKING_GRADES:
        failures.append(
            f"체결 정합 등급 미달(M1={m1}, M3={m3}) — 실전 차단"
        )


async def _check_order_notional(
    db: AsyncSession, user_id: int, strategy_id: int, order_notional: Decimal,
    failures: list[str], details: dict[str, Any],
) -> None:
    """1회 주문·일일 누적 주문 금액 상한을 확인해 초과 시 failures 에 사유를 추가한다."""
    # 1회 주문 상한 — RiskLimit.max_position_size 재사용.
    limit = await _get_limit(db, user_id, strategy_id)
    max_pos = limit.max_position_size if limit else None
    if max_pos is not None and order_notional > max_pos:
        failures.append(
            f"1회 주문 금액 {order_notional:,.0f}원이 최대 포지션 한도 {max_pos:,.0f}원 초과"
        )

    # 일일 누적 상한 — config 값. None/0 이면 미적용.
    cap = settings.KIS_DAILY_ORDER_NOTIONAL_CAP
    used = await _daily_executed_notional(db, user_id)
    details["order_notional"] = {
        "this_order": float(order_notional),
        "daily_used": float(used),
        "daily_cap": float(cap) if cap is not None else None,
        "max_position_size": float(max_pos) if max_pos is not None else None,
    }
    if cap is not None and Decimal(str(cap)) > 0 and used + order_notional > Decimal(str(cap)):
        failures.append(
            f"일일 누적 주문 금액 {used:,.0f}+{order_notional:,.0f}원이 "
            f"일일 상한 {Decimal(str(cap)):,.0f}원 초과"
        )


async def evaluate_live_gate(
    db: AsyncSession,
    user_id: int,
    strategy_id: int,
    *,
    order_notional: Decimal | None = None,
) -> GateResult:
    """실전 전환 게이트를 평가한다(세 조건 순차).

    :param order_notional: 이번 주문의 명목금액(qty×price). 주문 단위 호출(_place)에서
        전달한다. None 이면 금액 상한 체크를 건너뛴다(기동 단계 승인·정합 게이트만).
    :return: GateResult. prod 에서 passed=False 면 호출 측이 주문·기동을 거부해야 한다.
        vts(모의투자)에서는 체크를 수행해 로깅만 하고 항상 passed=True 로 통과시킨다.
    """
    failures: list[str] = []
    details: dict[str, Any] = {"paper": settings.is_paper_trading, "kis_env": settings.KIS_ENV}

    # 1) prod 2단계 승인 — 승인 플래그가 꺼져 있으면(기본 False) 실전 차단.
    if not settings.is_paper_trading and not settings.KIS_PROD_APPROVED:
        failures.append(
            "prod 승인 플래그(KIS_PROD_APPROVED)가 꺼져 있음 — 실전 전환 미승인"
        )

    # 2) 체결 정합 실측 등급(백테스트 가정 대비 실거래 괴리).
    await _check_fill_quality(db, user_id, strategy_id, failures, details)

    # 3) 주문 금액 상한(1회·일일 누적). 주문 단위 호출에서만.
    if order_notional is not None:
        await _check_order_notional(
            db, user_id, strategy_id, Decimal(str(order_notional)), failures, details
        )

    if settings.is_paper_trading:
        # vts(모의투자): 체크는 수행하되 차단하지 않는다(로깅만) — 실거래 이전 종단 관찰용.
        if failures:
            logger.info(
                "[live_gate] vts(모의투자) 전략 %d — 통과(로깅만): %s",
                strategy_id, "; ".join(failures),
            )
        else:
            logger.info("[live_gate] vts(모의투자) 전략 %d — 통과", strategy_id)
        return GateResult(passed=True, reasons=[], details={**details, "logged_failures": failures})

    passed = not failures
    if not passed:
        logger.warning(
            "[live_gate] 실전(prod) 게이트 실패 전략 %d: %s",
            strategy_id, "; ".join(failures),
        )
    else:
        logger.info("[live_gate] 실전(prod) 게이트 통과 전략 %d", strategy_id)
    return GateResult(passed=passed, reasons=failures, details=details)
