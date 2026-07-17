"""전략별 실측(모의투자) 자산가치 ↔ 백테스트 기대곡선 괴리 추적 라우트 (improvements.md §6).

라이브/모의투자 체결(orders/executions)로 일별 실측 NAV 곡선을 재구성하고, 동일 전략의
백테스트 기대 곡선(equity_curve)과 나란히 비교해 트래킹 에러·누적 괴리를 노출한다.
방법론·정규화·부호 규약·한계는 app/services/backtest/tracking.py 의 docstring 참조.

엔드포인트:
  GET /api/strategies/{strategy_id}/tracking — 실측/백테스트 오버레이 + 요약 지표.

주의:
  - 본인(current user) 소유 전략만. 체결이 하나도 없으면 404(비교할 실측 곡선 부재).
  - 비교 백테스트는 backtest_id 지정 시 그 건, 미지정 시 해당 전략의 최신 백테스트를 자동
    매칭한다. 백테스트가 없으면 200 으로 실측 곡선만 돌려주고(backtest_curve=[]) 지표는
    null·warning 을 채운다.
  - 종가는 price_ticks(단일 출처)에서 조회한다. 백테스트 곡선은 저장된 result.equity_curve
    를 그대로 쓴다(재계산하지 않음).
  - date_from 은 '표시 창'만 좁힌다(§5). NAV 재구성 자체는 언제나 이 전략의 진짜 첫 체결
    부터 전체 재생하고, date_from 이후 구간만 잘라 반환한다 — date_from 이전부터 보유하던
    포지션이 있어도 NAV 레벨이 어긋나지 않는다(tracking.reconstruct_realized_curve 참고).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.api.routes.strategies import _get_owned
from app.core.database import get_db
from app.models import Backtest, Order, OrderStatus, PriceTick, User
from app.services.backtest.tracking import (
    compute_tracking,
    reconstruct_realized_curve,
)
from app.services.market import KST

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["tracking"])

# 체결로 인정하는 주문 상태(부분·전량). 나머지는 미체결이라 NAV 재구성에서 제외.
_FILLED_STATUSES = (OrderStatus.PARTIAL, OrderStatus.FILLED)
# 기본 초기자본(원). 전략 config.cash 도 없을 때의 폴백(engine.py 기본과 일치).
_DEFAULT_CAPITAL = 10_000_000.0


# ─────────────────────────── 응답 스키마 ───────────────────────────
class CurvePoint(BaseModel):
    """시계열 점 — 프론트 LineChart 의 {t, v} 포맷."""

    t: str
    v: float


class TrackingMetrics(BaseModel):
    """실측 vs 백테스트 요약 지표(공통 거래일 기준)."""

    tracking_error: float | None = None            # 일수익률 차이의 연율화 표준편차(소수)
    cumulative_divergence_pct: float | None = None  # 누적 상대 성과 격차(%)
    realized_return_pct: float | None = None        # 공통구간 실측 수익률(%)
    backtest_return_pct: float | None = None        # 공통구간 백테스트 수익률(%)
    correlation: float | None = None                # 일수익률 상관
    n_common_days: int = 0                          # 비교에 쓰인 공통 거래일 수


class TrackingSample(BaseModel):
    """표본·데이터 규모."""

    n_executions: int
    n_symbols: int
    n_realized_days: int
    n_backtest_days: int


class TrackingWindow(BaseModel):
    """조회 창(실측 데이터 기준)."""

    date_from: str
    date_to: str


class TrackingResponse(BaseModel):
    """전략 실측↔백테스트 괴리 추적 응답."""

    strategy_id: int
    backtest_id: int | None
    window: TrackingWindow
    initial_capital: float
    realized_curve: list[CurvePoint]     # 실측 일별 NAV(원)
    backtest_curve: list[CurvePoint]     # 백테스트 equity_curve(원)
    realized_index: list[CurvePoint]     # 실측 100 기준 정규화
    backtest_index: list[CurvePoint]     # 백테스트 100 기준 정규화
    metrics: TrackingMetrics
    sample: TrackingSample
    warning: str | None = None
    notes: list[str] = []


def _kst_date(dt: datetime) -> pd.Timestamp:
    """tz-aware(UTC 저장) executed_at 을 KST 날짜(정규화 Timestamp)로 변환한다."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return pd.Timestamp(dt.astimezone(KST).date())


def _normalize_executions(
    orders: list[Order], d_from: date, d_to: date
) -> list[dict[str, Any]]:
    """Order+Execution → reconstruct_realized_curve 입력 dict 로 정규화.

    side 는 주문(Order)에서, 수량·체결가·수수료·체결일은 Execution 에서 취한다. 창
    [d_from, d_to] 밖 체결·수량 0 이하는 제외한다.
    """
    out: list[dict[str, Any]] = []
    for o in orders:
        side = str(o.side)
        for e in o.executions:
            qty = float(e.filled_qty)
            price = float(e.filled_price)
            if qty <= 0 or price <= 0:
                continue
            d = _kst_date(e.executed_at)
            if d.date() < d_from or d.date() > d_to:
                continue
            out.append({
                "symbol": o.symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "fee": float(e.fee) if e.fee is not None else 0.0,
                "date": d,
            })
    return out


async def _build_close_panel(
    db: AsyncSession, symbols: list[str], start: datetime, end: datetime
) -> pd.DataFrame:
    """대상 종목의 price_ticks 종가 패널을 조회한다(columns=종목, index=time).

    종목별로 조회해 결합하며, 한 종목이라도 데이터가 없으면 그 컬럼은 빠진다(재구성
    단계에서 0 평가·경고 처리). 시세가 하나도 없으면 빈 DataFrame.
    """
    cols: dict[str, pd.Series] = {}
    for sym in symbols:
        rows = (
            await db.execute(
                select(PriceTick.time, PriceTick.close)
                .where(PriceTick.symbol == sym)
                .where(PriceTick.time >= start)
                .where(PriceTick.time <= end)
                .order_by(PriceTick.time)
            )
        ).all()
        if not rows:
            continue
        idx = pd.DatetimeIndex([r[0] for r in rows]).normalize()
        s = pd.Series([float(r[1]) for r in rows], index=idx)
        cols[sym] = s[~s.index.duplicated(keep="last")]
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).sort_index()


@router.get(
    "/strategies/{strategy_id}/tracking",
    response_model=TrackingResponse,
    summary="실측(모의투자) 성과 ↔ 백테스트 기대곡선 괴리 추적",
    description=(
        "전략의 체결(executions)로 일별 실측 NAV 곡선을 재구성해 동일 전략의 백테스트 기대 "
        "곡선과 비교한다. 두 곡선(원화·100 정규화)과 트래킹 에러·누적 괴리 지표를 반환한다. "
        "체결이 없으면 404, 백테스트가 없으면 실측 곡선만(지표 null·warning) 돌려준다."
    ),
)
async def get_strategy_tracking(
    strategy_id: int,
    date_from: Annotated[date | None, Query(description="시작일(미지정 시 첫 체결일)")] = None,
    date_to: Annotated[date | None, Query(description="종료일(미지정 시 오늘)")] = None,
    backtest_id: Annotated[
        int | None, Query(description="비교 백테스트 id(미지정 시 최신 백테스트 자동 매칭)")
    ] = None,
    initial_capital: Annotated[
        float | None,
        Query(gt=0, description="실측 NAV 초기자본(원). 미지정 시 전략 config.cash 또는 기본값"),
    ] = None,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrackingResponse:
    """전략 실측↔백테스트 괴리 추적 리포트를 반환한다(본인 소유만)."""
    strategy = await _get_owned(db, current, strategy_id)

    d_to = date_to or datetime.now(KST).date()
    d_from = date_from or date(1970, 1, 1)  # 미지정 시 첫 체결일까지 하한 개방
    if date_from is not None and date_from > d_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "시작일이 종료일보다 늦습니다."
        )

    # 1) 대상 주문·체결 조회(본인·전략·체결분) → 정규화. 재생(replay)은 항상 진짜 첫 체결
    # 부터 전체 이력으로 하고(§5), date_from 은 아래 window_execs 로 '표시'만 좁힌다.
    orders = list(
        await db.scalars(
            select(Order)
            .options(selectinload(Order.executions))
            .where(
                Order.user_id == current.id,
                Order.strategy_id == strategy_id,
                Order.status.in_(_FILLED_STATUSES),
            )
            .order_by(Order.created_at)
        )
    )
    all_execs = _normalize_executions(orders, date(1970, 1, 1), d_to)
    if not all_execs:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "해당 기간 실측 체결이 없습니다(모의투자/실거래 체결 후 이용 가능).",
        )
    window_execs = [e for e in all_execs if e["date"].date() >= d_from]
    if not window_execs:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "해당 기간 실측 체결이 없습니다(모의투자/실거래 체결 후 이용 가능).",
        )

    window_dates = [e["date"].date() for e in window_execs]
    eff_from = min(window_dates)
    eff_to = max(max(window_dates), d_to)
    all_dates = [e["date"].date() for e in all_execs]
    replay_from = min(all_dates)

    # 2) 종가 패널 조회(price_ticks). 재생 시작일(진짜 첫 체결일)부터 종료일까지 —
    # display_from 이전 보유 종목의 시세도 필요하다(§5).
    symbols = sorted({e["symbol"] for e in all_execs})
    start_dt = datetime.combine(replay_from, time(0, 0, 0), tzinfo=KST)
    end_dt = datetime.combine(eff_to, time(23, 59, 59), tzinfo=KST)
    panel = await _build_close_panel(db, symbols, start_dt, end_dt)

    # 3) 실측 NAV 재구성 — 전체 이력을 재생하되 display_from(=d_from) 이후만 반환(§5).
    cfg = strategy.config or {}
    capital = float(
        initial_capital
        if initial_capital is not None
        else (cfg.get("cash") or _DEFAULT_CAPITAL)
    )
    realized_curve, notes = reconstruct_realized_curve(
        all_execs, panel, initial_capital=capital, display_from=d_from,
    )
    if replay_from < eff_from:
        n_prior = sum(1 for d in all_dates if d < eff_from)
        notes.append(
            f"조회 구간 이전 체결 {n_prior}건을 반영해 표시 구간 시작 시점의 초기 보유·현금을"
            " 산출했습니다(표시 구간 자체는 그대로 유지)."
        )

    # 4) 비교 백테스트 매칭.
    bt_query = select(Backtest).where(Backtest.strategy_id == strategy_id)
    if backtest_id is not None:
        bt_query = bt_query.where(Backtest.id == backtest_id)
    else:
        bt_query = bt_query.order_by(Backtest.id.desc())
    bt = await db.scalar(bt_query)
    if backtest_id is not None and bt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "백테스트를 찾을 수 없습니다.")

    backtest_curve: list[dict[str, Any]] = []
    warning: str | None = None
    if bt is not None and isinstance(bt.result, dict):
        raw = bt.result.get("equity_curve") or []
        backtest_curve = [
            {"t": p["t"], "v": float(p["v"])}
            for p in raw
            if isinstance(p, dict) and p.get("t") is not None and p.get("v") is not None
        ]
    if not backtest_curve:
        warning = "비교할 백테스트 곡선이 없습니다 — 실측 곡선만 표시합니다."

    # 5) 지표 산출.
    tracking = compute_tracking(realized_curve, backtest_curve)

    return TrackingResponse(
        strategy_id=strategy_id,
        backtest_id=bt.id if bt is not None else None,
        window=TrackingWindow(
            date_from=eff_from.isoformat(),
            date_to=eff_to.isoformat(),
        ),
        initial_capital=capital,
        realized_curve=[CurvePoint(**p) for p in realized_curve],
        backtest_curve=[CurvePoint(**p) for p in backtest_curve],
        realized_index=[CurvePoint(**p) for p in tracking["realized_index"]],
        backtest_index=[CurvePoint(**p) for p in tracking["backtest_index"]],
        metrics=TrackingMetrics(**tracking["metrics"]),
        sample=TrackingSample(
            n_executions=len(window_execs),
            n_symbols=len({e["symbol"] for e in window_execs}),
            n_realized_days=len(realized_curve),
            n_backtest_days=len(backtest_curve),
        ),
        warning=warning,
        notes=notes,
    )
