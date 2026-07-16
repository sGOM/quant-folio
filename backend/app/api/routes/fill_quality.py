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
from pydantic import BaseModel, Field
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
from app.services.backtest.slippage_calibration import propose_slippage_calibration
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

    if strategy_id is not None:
        strat = await db.scalar(
            select(Strategy).where(
                Strategy.id == strategy_id, Strategy.user_id == current.id
            )
        )
        if strat is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")

    return await compute_fill_quality_report(
        db, current.id,
        d_from=d_from, d_to=d_to, strategy_id=strategy_id,
        annual_turnover=annual_turnover, capital=capital,
        min_sample=min_sample, detail=detail,
    )


# 캘리브레이션 제안 산출 시 되돌아보는 창(일). B-2 배치·GET 리포트 기본과 맞춘다.
_CALIB_WINDOW_DAYS = 90
# 요청 제안값과 서버 재계산 제안값의 허용 오차(bp). 반올림(2자리) 여유.
_CALIB_MATCH_TOL_BPS = 0.5


class SlippageCalibrationApply(BaseModel):
    """슬리피지 캘리브레이션 승인 요청 바디."""

    proposed_bps: float = Field(
        ..., ge=0, le=1000, description="반영할 slippage_bps(서버가 재계산한 제안값과 대조 검증)"
    )


@router.post(
    "/slippage-calibration/{strategy_id}/apply",
    summary="슬리피지 캘리브레이션 제안 승인·반영",
    description=(
        "체결 정합 실측(M1)을 근거로 산출한 slippage_bps 제안을 사람이 승인해 전략 config 에 "
        "반영한다. 자동 반영은 없다. 서버가 최신 리포트로 제안값을 재계산해 요청값과 대조하며, "
        "제안이 없거나(표본 부족·유의 변화 없음) 값이 불일치하면 409 로 거절한다."
    ),
)
async def apply_slippage_calibration_route(
    strategy_id: int,
    payload: SlippageCalibrationApply,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """제안을 승인해 전략 config 의 slippage_bps 를 갱신한다(본인 소유만)."""
    return await apply_slippage_calibration(
        db, current.id,
        strategy_id=strategy_id,
        proposed_bps=payload.proposed_bps,
        actor=current.email,
    )


async def apply_slippage_calibration(
    db: AsyncSession,
    user_id: int,
    *,
    strategy_id: int,
    proposed_bps: float,
    actor: str,
    window_days: int = _CALIB_WINDOW_DAYS,
) -> dict[str, Any]:
    """슬리피지 캘리브레이션 제안을 검증·반영한다(HTTP 비의존, 라우트가 위임).

    최신 정합 리포트로 제안을 재계산해(사람이 오래된/조작된 값을 밀어 넣지 못하도록) 요청
    값과 대조한 뒤, 일치하면 Strategy.config 의 slippage_bps 를 갱신하고 감사 로그를 남긴다.
    """
    strat = await db.scalar(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user_id)
    )
    if strat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")

    d_to = datetime.now(KST).date()
    d_from = d_to - timedelta(days=window_days)
    report = await compute_fill_quality_report(
        db, user_id, d_from=d_from, d_to=d_to, strategy_id=strategy_id,
    )
    proposal = propose_slippage_calibration(report)
    if proposal is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "현재 반영할 슬리피지 캘리브레이션 제안이 없습니다(표본 부족 또는 유의 변화 없음).",
        )
    if abs(proposal.proposed_bps - float(proposed_bps)) > _CALIB_MATCH_TOL_BPS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"제안값 불일치: 최신 제안은 {proposal.proposed_bps}bp 입니다"
            f"(요청 {proposed_bps}bp). 다시 확인 후 승인하세요.",
        )

    cfg = dict(strat.config or {})
    old_bps = float(cfg.get("slippage_bps", DEFAULT_SLIP_BPS) or DEFAULT_SLIP_BPS)
    # JSONB 는 새 dict 재할당으로 변경을 추적하게 한다(전략 수정 API 와 동일 패턴).
    cfg["slippage_bps"] = proposal.proposed_bps
    strat.config = cfg
    await db.commit()
    await db.refresh(strat)

    # 감사 로그: 이전값→신규값·승인자·근거(실측 중앙값·표본)를 남긴다.
    logger.info(
        "슬리피지 캘리브레이션 반영: 전략 %s slippage_bps %.2f→%.2f "
        "(승인자=%s, 실측 중앙값 %.2fbp, 표본 %d, 클램프=%s)",
        strategy_id, old_bps, proposal.proposed_bps, actor,
        proposal.observed_median_bps, proposal.sample_size, proposal.clamped,
    )
    return {
        "strategy_id": strategy_id,
        "previous_bps": old_bps,
        "applied_bps": proposal.proposed_bps,
        "observed_median_bps": proposal.observed_median_bps,
        "sample_size": proposal.sample_size,
        "clamped": proposal.clamped,
        "reason": proposal.reason,
    }


async def compute_fill_quality_report(
    db: AsyncSession,
    user_id: int,
    *,
    d_from: date,
    d_to: date,
    strategy_id: int | None = None,
    annual_turnover: float | None = None,
    capital: float | None = None,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    detail: bool = False,
) -> dict[str, Any]:
    """정합 실측 리포트 산출(HTTP 비의존) — 라우트와 Celery 주간 배치(B-2)가 공유한다.

    호출자가 전략 소유권을 이미 확인했다고 가정한다(라우트는 404 처리를, 배치는
    Order.user_id 로 조회하므로 자연히 본인 소유만 잡힌다).
    """
    start_dt = datetime.combine(d_from, time(0, 0, 0), tzinfo=KST)
    end_dt = datetime.combine(d_to, time(23, 59, 59), tzinfo=KST)

    # 슬리피지 가정: 전략 지정 시 그 config 값, 아니면 기본(5bp, 고정).
    slip_bps = DEFAULT_SLIP_BPS
    slip_vol_scale = 0.0
    if strategy_id is not None:
        strat = await db.scalar(
            select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user_id)
        )
        if strat is not None:
            cfg = strat.config or {}
            slip_bps = float(cfg.get("slippage_bps", DEFAULT_SLIP_BPS) or DEFAULT_SLIP_BPS)
            slip_vol_scale = float(cfg.get("slippage_vol_scale", 0.0) or 0.0)
    slip_base = max(0.0, slip_bps / 1e4)

    # 대상 주문 조회: 본인·전략 리밸런싱·체결분(부분/전량).
    q = (
        select(Order)
        .options(selectinload(Order.executions))
        .where(
            Order.user_id == user_id,
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
