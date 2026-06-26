"""백테스트 실행·조회 라우트.

백테스트는 CPU 바운드이므로 run_in_threadpool 로 실행해 이벤트 루프를 막지 않는다.
데이터가 없으면 FinanceDataReader 로 적재 후 price_ticks 를 단일 출처로 사용한다.
"""
import logging
from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.api.routes.strategies import _get_owned
from app.core.database import get_db
from app.models import Backtest, Strategy, StrategyStatus, User
from app.schemas.strategy import BacktestOut, BacktestRequest
from app.services.backtest import run_backtest
from app.services.backtest.signals import requires_ohlc
from app.services.data import load_ohlcv, upsert_price_ticks
from app.services.data.loader import get_close_series, get_ohlcv_frame
from app.services.market import KST
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["backtests"])


def _to_dt(d, end: bool = False) -> datetime:
    """날짜를 KST 경계 datetime 으로 변환한다.

    KRX 시세는 KST 기준이므로 경계도 KST 자정/마감으로 잡는다(UTC 변환 시 9시간 오차).
    :param d: 변환할 date
    :param end: True 면 그날 23:59:59(종료 경계), False 면 00:00:00(시작 경계)
    """
    t = time(23, 59, 59) if end else time(0, 0, 0)
    return datetime.combine(d, t, tzinfo=KST)


@router.post(
    "/strategies/{strategy_id}/backtest",
    response_model=BacktestOut,
    status_code=status.HTTP_201_CREATED,
)
async def run_strategy_backtest(
    strategy_id: int,
    req: BacktestRequest,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략을 지정 기간으로 백테스트한다.

    시세(price_ticks)가 없으면 외부 소스에서 적재 후 단일 출처로 사용하며,
    CPU 바운드 계산은 스레드풀에서 실행해 이벤트 루프를 막지 않는다. 결과는 저장된다.
    """
    strategy: Strategy = await _get_owned(db, current, strategy_id)
    config = strategy.config
    if config.get("type") == "rebalance":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "리밸런싱(포트폴리오) 전략의 백테스트는 아직 지원되지 않습니다.",
        )
    symbol = config["symbol"]
    start_dt, end_dt = _to_dt(req.period_start), _to_dt(req.period_end, end=True)

    # OHLC 전략이면 OHLCV 프레임을, close-only 전략이면 종가만 신호 입력으로 사용.
    use_ohlc = requires_ohlc(config)

    async def _fetch():
        if use_ohlc:
            return await get_ohlcv_frame(db, symbol, start_dt, end_dt)
        return await get_close_series(db, symbol, start_dt, end_dt)

    # 1) price_ticks 확인, 없으면 적재
    series = await _fetch()
    if series.empty:
        try:
            df = await run_in_threadpool(
                load_ohlcv, symbol, req.period_start, req.period_end
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"데이터 적재 실패: {e}")
        await upsert_price_ticks(db, symbol, df)
        series = await _fetch()

    if series.empty:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "해당 기간 시세가 없습니다.")

    # 2) 백테스트 실행 (CPU 바운드 → 스레드풀)
    try:
        result = await run_in_threadpool(run_backtest, series, config)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("백테스트 실행 오류")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"백테스트 실패: {e}")

    # 3) 결과 저장 + 전략 상태 갱신
    bt = Backtest(
        strategy_id=strategy.id,
        period_start=start_dt,
        period_end=end_dt,
        total_return=result.get("total_return"),
        mdd=result.get("mdd"),
        sharpe=result.get("sharpe"),
        result=result,
    )
    db.add(bt)
    if strategy.status == StrategyStatus.DRAFT:
        strategy.status = StrategyStatus.BACKTESTED
    await db.commit()
    await db.refresh(bt)
    return bt


@router.get("/strategies/{strategy_id}/backtests", response_model=list[BacktestOut])
async def list_backtests(
    strategy_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """전략의 백테스트 실행 이력을 최신순으로 반환한다."""
    await _get_owned(db, current, strategy_id)
    rows = await db.scalars(
        select(Backtest)
        .where(Backtest.strategy_id == strategy_id)
        .order_by(Backtest.id.desc())
    )
    return list(rows)


@router.get("/backtests/{backtest_id}", response_model=BacktestOut)
async def get_backtest(
    backtest_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """백테스트 단건을 조회한다(전략 소유자 본인만). 없으면 404."""
    bt = await db.scalar(
        select(Backtest)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .where(Backtest.id == backtest_id, Strategy.user_id == current.id)
    )
    if bt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "백테스트를 찾을 수 없습니다.")
    return bt
