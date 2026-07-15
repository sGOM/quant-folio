"""백테스트 실행·조회 라우트.

백테스트는 CPU 바운드이므로 run_in_threadpool 로 실행해 이벤트 루프를 막지 않는다.
데이터가 없으면 FinanceDataReader 로 적재 후 price_ticks 를 단일 출처로 사용한다.
"""
import logging
from datetime import date, datetime, time, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.api.routes.strategies import _get_owned
from app.core.database import get_db
from app.models import Backtest, Strategy, StrategyStatus, User
from app.schemas.strategy import BacktestOut, BacktestRequest
from app.services.backtest import run_backtest, run_rebalance_backtest
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


async def _run_single_symbol_backtest(
    db: AsyncSession, config: dict, req: BacktestRequest, start_dt, end_dt
) -> dict:
    """단일 종목 전략 백테스트(기존 vectorbt 경로)."""
    symbol = config["symbol"]
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
        return await run_in_threadpool(run_backtest, series, config)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("백테스트 실행 오류")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"백테스트 실패: {e}")


# pykrx 종합지수 티커(레짐 필터 기준지수).
_REGIME_INDEX_TICKER = {"KOSPI": "1001", "KOSDAQ": "2001"}
# 벤치마크 상대성과용 지수(pykrx 지수코드). 대형주 전략 기본은 KOSPI200.
_BENCHMARK_TICKER = {"KOSPI200": "1028", "KOSPI": "1001", "KOSDAQ": "2001"}


def _load_panic_series(start_d, end_d, market: str):
    """패닉 오버레이(panic_overlay)용 롤링 패닉 지표 시계열을 조회한다(블로킹, 스레드풀 내 호출).

    compute_panic_series 를 재사용한다. 실패/부재 시 None 을 반환하면 백테스트 엔진이
    오버레이를 적용하지 않는다(경고 로그만 남기고 코어 전략은 그대로 동작).
    """
    from app.services.metrics.panic import compute_panic_series

    df = compute_panic_series(market, start_d, end_d)
    return df if df is not None and not df.empty else None


def _load_regime_series(start_d, end_d, ticker: str):
    """레짐 필터용 기준지수 종가 Series 를 조회한다(블로킹, 스레드풀 내 호출).

    metrics._fetch_index_ohlcv 를 재사용한다. 실패/부재 시 None 을 반환하면
    백테스트 엔진이 오버레이를 적용하지 않는다(항상 투자).
    """
    from app.services.metrics import _fetch_index_ohlcv, _ymd

    df = _fetch_index_ohlcv(_ymd(start_d), _ymd(end_d), ticker)
    if df is None or df.empty or "close" not in df.columns:
        return None
    return df["close"]


def _fundamentals_provider(as_of_date, codes):
    """score 선정용 as_of 시점 펀더멘털을 조회한다(블로킹, 스레드풀 내 호출).

    - 밸류(PER/PBR/DIV): metrics._fetch_fundamentals(pykrx) — 실거래 점수와 동일 소스.
    - 퀄리티(roe/debt_ratio/fcf): OpenDART 재무데이터(PIT 공시지연 반영). API 키가
      없으면 빈 결과라 quality 컬럼이 붙지 않고, 백테스트 엔진이 중립 처리한다.

    둘 다 실패/부재면 None 을 반환하면 엔진이 밸류·퀄리티 팩터를 중립 처리한다.
    """
    from app.services.data import opendart
    from app.services.metrics import _fetch_fundamentals, _ymd

    norm_codes = [str(c).zfill(6) for c in codes]
    fdf = None
    try:
        raw = _fetch_fundamentals(_ymd(as_of_date), ["KOSPI", "KOSDAQ"])
        if raw is not None and not raw.empty:
            fdf = raw.copy()
            fdf.index = fdf.index.astype(str).str.zfill(6)
            fdf = fdf.reindex(norm_codes)
    except Exception:  # noqa: BLE001
        fdf = None

    # 퀄리티 팩터(OpenDART). 키 부재/미배선 시 {} → 병합 없음.
    try:
        qmetrics = opendart.metrics_by_symbol(norm_codes, as_of_date)
    except Exception:  # noqa: BLE001
        qmetrics = {}

    if not qmetrics:
        return fdf  # 밸류만(또는 None)

    qdf = pd.DataFrame.from_dict(qmetrics, orient="index")
    qcols = ("roe", "debt_ratio", "fcf", "f_score",
             "op_growth", "net_growth", "turnaround")
    qdf = qdf[[c for c in qcols if c in qdf.columns]]
    qdf = qdf.reindex(norm_codes)
    if fdf is None:
        return qdf
    return fdf.join(qdf, how="left")


def _fundamentals_provider_with_market_cap(as_of_date, codes):
    """_fundamentals_provider 결과에 as_of 시점 시가총액(원) 컬럼을 덧붙인다(사이즈 중립화용).

    krx_index.market_caps(PIT)를 재사용한다. 미인증/실패로 시총이 비면 컬럼을 붙이지
    않으므로 스코어러가 중립화를 자연히 생략한다(순수 팩터 그대로).
    """
    from app.services.data import krx_index

    fdf = _fundamentals_provider(as_of_date, codes)
    norm_codes = [str(c).zfill(6) for c in codes]
    try:
        caps = krx_index.market_caps(as_of_date)
    except Exception:  # noqa: BLE001
        caps = {}
    if not caps:
        return fdf
    cap_series = pd.Series({c: caps.get(c) for c in norm_codes}, dtype="float64")
    if fdf is None:
        return cap_series.to_frame("market_cap")
    fdf = fdf.copy()
    fdf["market_cap"] = cap_series.reindex(fdf.index)
    return fdf


def _build_pit_pool(config: dict, start, end):
    """universe_rule.source 가 지수명이면 (합집합 universe, pool_provider) 를 만든다.

    각 월(month) 의 실제 지수 구성종목을 조회해, 가격 적재용 '합집합'과 리밸런싱일별
    후보풀을 공급하는 pool_provider 를 반환한다(생존편향 제거·PIT). 네트워크 호출을
    백테스트 루프 밖으로 빼기 위해 월별 멤버십을 미리 조회해 dict 로 캐시한다.
    source="fixed" 이면 (None, None) 을 반환해 기존 고정 universe 경로를 쓴다.
    (블로킹 — 호출부가 run_in_threadpool 로 감쌀 것.)
    """
    rule = (config.get("selection") or {}).get("universe_rule") or {}
    source = rule.get("source", "fixed")
    if source == "fixed":
        return None, None

    from app.services.data import krx_index

    # 유동성 필터: 시가총액(억 원) 하한. 각 월 시점 시총 기준으로 소형주를 후보풀에서 제외.
    min_cap = rule.get("min_market_cap")
    min_cap_won = int(min_cap) * 10**8 if min_cap else 0

    # start~end 를 포함하는 각 월의 1일(휴장이면 krx_index 가 직전 영업일로 스냅) 멤버십.
    by_month: dict[tuple[int, int], list[str]] = {}
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        members = krx_index.index_members(date(y, m, 1), source)
        if min_cap_won and members:
            caps = krx_index.market_caps(date(y, m, 1))
            if caps:  # 조회 성공 시에만 필터(실패 시 원본 유지 — 과도한 축소 방지)
                members = [c for c in members if caps.get(c, 0) >= min_cap_won]
        by_month[(y, m)] = members
        m += 1
        if m > 12:
            y, m = y + 1, 1
    union = sorted({c for codes in by_month.values() for c in codes})

    def pool_provider(ts):
        return by_month.get((ts.year, ts.month), [])

    return union, pool_provider


async def _run_rebalance_backtest(
    db: AsyncSession, config: dict, req: BacktestRequest, start_dt, end_dt
) -> dict:
    """리밸런싱(다종목 포트폴리오) 전략 백테스트.

    universe 전 종목의 종가 패널을 워밍업 구간 포함해 적재한 뒤, 실거래와 동일한
    선정·점수 로직으로 일별 시뮬레이션한다. universe_rule.source 가 지수명이면 각
    리밸런싱 시점의 실제 구성종목(PIT)을 후보풀로 써 생존편향을 제거한다.
    """
    # 팩터 워밍업: 52주 고가·MDD(252) + 모멘텀(126)에 필요. 넉넉히 500일 앞부터 적재.
    warmup_start = req.period_start - timedelta(days=500)

    # 시점별 지수 멤버십(PIT) 후보풀 준비. 지수 소스가 아니면 (None, None).
    pit_union, pool_provider = await run_in_threadpool(
        _build_pit_pool, config, warmup_start, req.period_end
    )
    universe = pit_union if pit_union is not None else list(config.get("universe", []))
    if not universe:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "universe 가 비어 있습니다.")

    warmup_dt = _to_dt(warmup_start)

    # 각 종목 종가 시드(없으면 외부 적재 후 재조회)
    columns: dict[str, pd.Series] = {}
    for sym in universe:
        series = await get_close_series(db, sym, warmup_dt, end_dt)
        if series.empty:
            try:
                df = await run_in_threadpool(load_ohlcv, sym, warmup_start, req.period_end)
                await upsert_price_ticks(db, sym, df)
                series = await get_close_series(db, sym, warmup_dt, end_dt)
            except Exception as e:  # noqa: BLE001
                logger.warning("리밸런싱 백테스트 %s 시세 적재 실패: %s", sym, e)
        if not series.empty:
            columns[sym] = series

    if not columns:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "universe 종목의 시세를 확보하지 못했습니다."
        )

    panel = pd.DataFrame(columns)
    method = config.get("selection", {}).get("method", "momentum")
    provider = _fundamentals_provider if method == "score" else None
    # 사이즈 중립화(P1-3): 시가총액(PIT)을 펀더멘털 프레임에 실어 스코어러가 각 팩터를
    # 로그 시총 축에 직교화하게 한다. 중립화가 꺼진 전략에는 추가 조회를 하지 않는다.
    if provider is not None and config.get("selection", {}).get("neutralize") == "size":
        provider = _fundamentals_provider_with_market_cap

    # 현금화 오버레이(레짐 필터): 켜져 있으면 기준지수 종가 시리즈를 적재해 주입한다.
    regime_series = None
    rf = config.get("regime_filter") or {}
    if rf.get("enabled"):
        ticker = _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001")
        try:
            regime_series = await run_in_threadpool(
                _load_regime_series, warmup_start, req.period_end, ticker
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("레짐 필터 기준지수 적재 실패(오버레이 미적용): %s", e)

    # 벤치마크 상대성과: 항상 지수 종가를 적재해 alpha/beta/IR 등을 산출한다(실패 시 None).
    benchmark_series = None
    bench_ticker = _BENCHMARK_TICKER.get(config.get("benchmark_index", "KOSPI200"), "1028")
    try:
        benchmark_series = await run_in_threadpool(
            _load_regime_series, warmup_start, req.period_end, bench_ticker
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("벤치마크 지수 적재 실패(상대지표 미산출): %s", e)

    # 패닉 오버레이(P2): 켜져 있으면 롤링 패닉 지표 시계열을 적재해 주입한다. 브레드스는
    # pykrx 특성상 거래일마다 조회가 필요해 최초 실행이 느릴 수 있다(로컬 파일 캐시로
    # 재실행은 빠르다 — app.services.metrics.panic._BREADTH_CACHE_DIR).
    panic_series = None
    po = config.get("panic_overlay") or {}
    if po.get("enabled"):
        try:
            panic_series = await run_in_threadpool(
                _load_panic_series, warmup_start, req.period_end, po.get("market", "KOSPI")
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("패닉 오버레이 시계열 적재 실패(오버레이 미적용): %s", e)

    try:
        result = await run_in_threadpool(
            run_rebalance_backtest,
            panel,
            config,
            start_dt,
            end_dt,
            provider,
            regime_series,
            pool_provider,
            benchmark_series,
            panic_series,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("리밸런싱 백테스트 실행 오류")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"백테스트 실패: {e}")

    # 팩터 커버리지 경고: quality/growth 가중치>0 인데 OpenDART 재무 커버리지가 낮으면
    # 해당 팩터가 조용히 중립(0) 처리되어 다른 전략이 된다. 결과에 명시적으로 노출한다.
    warnings = await run_in_threadpool(_factor_coverage_warnings, config, universe, req.period_end)
    if warnings:
        result["factor_warnings"] = warnings
    return result


def _factor_coverage_warnings(config: dict, universe: list[str], as_of) -> list[str]:
    """quality/growth 가중치가 있으나 OpenDART 재무 커버리지가 낮은지 점검(블로킹).

    커버리지가 임계 미만이면 "그 팩터가 사실상 미반영"이라는 경고 문자열을 반환한다.
    빈 리스트면 문제 없음. 네트워크 실패 시에는 경고를 생성하지 않는다(오탐 방지).
    """
    fw = (config.get("selection") or {}).get("factor_weights") or {}
    need = [k for k in ("quality", "growth") if float(fw.get(k, 0) or 0) > 0]
    if not need or not universe:
        return []
    sample = universe[:40]
    try:
        from app.services.data import opendart

        metrics = opendart.metrics_by_symbol(sample, as_of)
    except Exception:  # noqa: BLE001
        return []  # 조회 실패는 경고 대상 아님(오탐 방지)
    covered = sum(1 for code in sample if metrics.get(code))
    coverage = covered / len(sample) if sample else 0.0
    if coverage >= 0.3:
        return []
    return [
        f"OpenDART 재무 커버리지 {coverage * 100:.0f}% (표본 {len(sample)}종목) — "
        f"가중치를 준 {'·'.join(need)} 팩터가 대부분 중립(0) 처리되어 점수에 거의 반영되지 "
        f"않습니다. OPENDART API 키·응답을 확인하거나 해당 가중치를 재검토하세요."
    ]


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
    start_dt, end_dt = _to_dt(req.period_start), _to_dt(req.period_end, end=True)

    if config.get("type") == "rebalance":
        result = await _run_rebalance_backtest(db, config, req, start_dt, end_dt)
    else:
        result = await _run_single_symbol_backtest(db, config, req, start_dt, end_dt)

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
