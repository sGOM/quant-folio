"""실거래–백테스트 체결 정합 실측 라우트 (P2-3).

라이브 orders/executions 를 백테스트 체결모형이 가정하는 값과 비교해 3분해(M1 실행
슬리피지·M2 시점 규약 표류·M3 총 정합 괴리)를 bp 로 계량한다. 방법론·부호 규약·등급
기준은 app/services/backtest/fill_quality.py 의 docstring 참조.

엔드포인트:
  GET /api/backtest/fill-quality — 온디맨드 정합 리포트(기간·선택적 전략별).

주의:
  - 대상은 본인(current user)의 전략 리밸런싱 주문만(strategy_id IS NOT NULL),
    체결이 있는 주문(부분/전량)만. REJECTED·미체결은 자연 제외된다.
  - 종가는 백테스트 패널과 동일 소스(load_ohlcv → pykrx/FDR 일봉)로 조회해
    _vol_slippage_map 재호출과 일관되게 맞춘다.
  - 모의투자 초기라 표본이 적으면 등급은 "판정 보류(INSUFFICIENT)"가 정상 경로다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import Order, OrderStatus, Strategy, User
from app.services.backtest.fill_quality import (
    DEFAULT_MIN_SAMPLE,
    DEFAULT_SLIP_BPS,
    compute_fill_quality,
)
from app.services.data.loader import load_ohlcv
from app.services.market import KST

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["fill-quality"])

# 체결로 인정하는 주문 상태(부분·전량). 나머지(PENDING/SUBMITTED/REJECTED/CANCELLED)는 제외.
_FILLED_STATUSES = (OrderStatus.PARTIAL, OrderStatus.FILLED)


def _kst_date(dt: datetime) -> pd.Timestamp:
    """tz-aware(UTC 저장) executed_at 을 KST 날짜(정규화 Timestamp)로 변환한다."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return pd.Timestamp(dt.astimezone(KST).date())


def _normalize_orders(orders: list[Order]) -> list[dict[str, Any]]:
    """Order+Execution → compute_fill_quality 입력 dict 로 정규화.

    P_live = 체결수량가중평균 filled_price, fill_date = 마지막 체결일(KST).
    체결이 없는 주문은 제외한다.
    """
    out: list[dict[str, Any]] = []
    for o in orders:
        execs = list(o.executions)
        if not execs:
            continue
        tot_qty = sum(float(e.filled_qty) for e in execs)
        if tot_qty <= 0:
            continue
        p_live = sum(float(e.filled_price) * float(e.filled_qty) for e in execs) / tot_qty
        fill_date = max(_kst_date(e.executed_at) for e in execs)
        out.append({
            "order_id": o.id,
            "symbol": o.symbol,
            "side": str(o.side),
            "decision_price": float(o.price) if o.price is not None else None,
            "fill_date": fill_date,
            "p_live": p_live,
            "filled_qty": tot_qty,
        })
    return out


def _build_close_panel(orders: list[dict[str, Any]]) -> pd.DataFrame:
    """주문 종목의 일봉 종가 패널을 백테스트와 동일 소스(load_ohlcv)로 조회한다(블로킹).

    윈도우: 최소 체결일−45일(변동성 슬리피지용 20봉 히스토리) ~ 최대 체결일+10일(t+1 종가).
    개별 종목 실패는 건너뛴다(해당 주문은 compute 단계에서 no_price 로 스킵).
    """
    if not orders:
        return pd.DataFrame()
    fdates = [pd.Timestamp(o["fill_date"]) for o in orders]
    start = (min(fdates) - timedelta(days=45)).date()
    end = (max(fdates) + timedelta(days=10)).date()
    cols: dict[str, pd.Series] = {}
    for sym in sorted({o["symbol"] for o in orders}):
        try:
            df = load_ohlcv(sym, start, end)
        except Exception as e:  # noqa: BLE001
            logger.warning("정합 실측 종가 적재 실패 %s: %s", sym, e)
            continue
        if df.empty or "close" not in df.columns:
            continue
        s = df["close"].astype("float64")
        s.index = pd.DatetimeIndex(s.index).normalize()
        cols[sym] = s[~s.index.duplicated(keep="last")]
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).sort_index()


def _estimate_annual_turnover(
    orders: list[dict[str, Any]], capital: float, span_days: int
) -> float | None:
    """편도 연 회전율을 매수 체결금액/자본/기간으로 근사한다(capital 제공 시).

    회전율 = (기간 내 매수 체결금액 합) / capital / 연수. 매수측만으로 편도 회전율을
    근사(전형적 정의)하며, capital 을 평균 순자산의 프록시로 쓴다 — 실제 순자산 변동·
    부분현금 구간은 반영하지 않는 거친 추정이므로 참고용이다.
    """
    if capital <= 0 or span_days <= 0:
        return None
    buy_notional = sum(
        o["p_live"] * o["filled_qty"] for o in orders if o["side"] == "buy"
    )
    if buy_notional <= 0:
        return None
    years = span_days / 365.25
    return buy_notional / capital / years if years > 0 else None


@router.get(
    "/fill-quality",
    summary="실거래–백테스트 체결 정합 실측",
    description=(
        "라이브 체결(orders/executions)을 백테스트 체결모형 가정과 비교해 M1(실행 "
        "슬리피지)·M2(시점 규약 표류)·M3(총 정합 괴리)를 bp 로 계량하고 등급을 판정한다. "
        "본인 전략 리밸런싱 주문(체결분)만 대상이며, 표본 부족 시 '판정 보류'로 표시된다."
    ),
)
async def get_fill_quality(
    days: Annotated[int, Query(ge=1, le=1095, description="조회 기간(오늘 기준 최근 N일)")] = 90,
    date_from: Annotated[date | None, Query(description="시작일(지정 시 days 무시)")] = None,
    date_to: Annotated[date | None, Query(description="종료일(기본 오늘)")] = None,
    strategy_id: Annotated[int | None, Query(description="전략 id(미지정 시 전체)")] = None,
    annual_turnover: Annotated[
        float | None, Query(ge=0, description="편도 연 회전율(예 4.0=400%). 미지정 시 capital 로 추정")
    ] = None,
    capital: Annotated[
        float | None, Query(gt=0, description="연 회전율 추정용 자본(원). annual_turnover 미지정 시 사용")
    ] = None,
    min_sample: Annotated[
        int, Query(ge=1, le=1000, description="방향당 표본 하한(미만이면 판정 보류)")
    ] = DEFAULT_MIN_SAMPLE,
    detail: Annotated[bool, Query(description="주문별 상세·스킵 목록 포함 여부")] = False,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """정합 실측 리포트를 반환한다."""
    d_to = date_to or datetime.now(KST).date()
    d_from = date_from or (d_to - timedelta(days=days))
    if d_from > d_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "시작일이 종료일보다 늦습니다.")

    start_dt = datetime.combine(d_from, time(0, 0, 0), tzinfo=KST)
    end_dt = datetime.combine(d_to, time(23, 59, 59), tzinfo=KST)

    # 슬리피지 가정: 전략 지정 시 그 config 값, 아니면 기본(5bp, 고정).
    slip_bps = DEFAULT_SLIP_BPS
    slip_vol_scale = 0.0
    if strategy_id is not None:
        strat = await db.scalar(
            select(Strategy).where(
                Strategy.id == strategy_id, Strategy.user_id == current.id
            )
        )
        if strat is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")
        cfg = strat.config or {}
        slip_bps = float(cfg.get("slippage_bps", DEFAULT_SLIP_BPS) or DEFAULT_SLIP_BPS)
        slip_vol_scale = float(cfg.get("slippage_vol_scale", 0.0) or 0.0)
    slip_base = max(0.0, slip_bps / 1e4)

    # 대상 주문 조회: 본인·전략 리밸런싱·체결분(부분/전량).
    q = (
        select(Order)
        .options(selectinload(Order.executions))
        .where(
            Order.user_id == current.id,
            Order.strategy_id.is_not(None),
            Order.status.in_(_FILLED_STATUSES),
            Order.created_at >= start_dt,
            Order.created_at <= end_dt,
        )
        .order_by(Order.created_at)
    )
    if strategy_id is not None:
        q = q.where(Order.strategy_id == strategy_id)
    rows = list(await db.scalars(q))
    norm = _normalize_orders(rows)

    # 연 회전율: 명시값 우선, 없으면 capital 로 추정.
    turnover = annual_turnover
    if turnover is None and capital is not None:
        turnover = _estimate_annual_turnover(norm, capital, (d_to - d_from).days)

    # 종가 패널 적재(블로킹 → 스레드풀). 주문이 없으면 빈 패널로 바로 계산(판정 보류).
    panel = await run_in_threadpool(_build_close_panel, norm)

    result = compute_fill_quality(
        norm,
        panel,
        slip_base=slip_base,
        slip_vol_scale=slip_vol_scale,
        backtest_slip_bps=slip_bps,
        min_sample=min_sample,
        annual_turnover=turnover,
    )
    result["window"] = {
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "strategy_id": strategy_id,
        "turnover_estimated": annual_turnover is None and turnover is not None,
    }
    if not detail:
        result.pop("per_order", None)
        result.pop("skipped", None)
    return result
