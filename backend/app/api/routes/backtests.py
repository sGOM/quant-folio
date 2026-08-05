"""백테스트 실행·조회 라우트.

백테스트는 CPU 바운드이므로 run_in_threadpool 로 실행해 이벤트 루프를 막지 않는다.
데이터가 없으면 FinanceDataReader 로 적재 후 price_ticks 를 단일 출처로 사용한다.
"""
import logging
from datetime import date, datetime, time, timedelta
from functools import partial

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.api.routes.strategies import _get_owned
from app.core.database import get_db
from app.models import Backtest, Strategy, StrategyStatus, User
from app.schemas.strategy import BacktestOut, BacktestRequest
from app.services.backtest import (
    analyze_backtest_dsr,
    run_backtest,
    run_rebalance_backtest,
)
from app.services.backtest.signals import requires_ohlc
from app.services.data.loader import (
    ensure_ohlcv_coverage,
    get_close_series,
    get_ohlcv_frame,
    get_volume_series,
)
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

    # 1) price_ticks 커버리지 확인, 부족한 만큼만 외부 소스로 보충(C-1 — 로컬 우선).
    await ensure_ohlcv_coverage(db, symbol, start_dt, end_dt)
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


def _fundamentals_provider(as_of_date, codes, use_ttm: bool = False):
    """score 선정용 as_of 시점 펀더멘털을 조회한다(블로킹, 스레드풀 내 호출).

    - 밸류(PER/PBR/DIV): metrics._fetch_fundamentals(pykrx) — 실거래 점수와 동일 소스.
    - 퀄리티(roe/debt_ratio/fcf): OpenDART 재무데이터(PIT 공시지연 반영). API 키가
      없으면 빈 결과라 quality 컬럼이 붙지 않고, 백테스트 엔진이 중립 처리한다.

    :param use_ttm: True 면 분기 TTM 경로(§8/§3, RebalanceConfig.financial_period=="ttm")로
        퀄리티 재무를 조회한다. False(기본)면 기존 연간 경로(재현성 보존).
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
        qmetrics = opendart.metrics_by_symbol(norm_codes, as_of_date, use_ttm=use_ttm)
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


def _fundamentals_provider_with_neutralize_cols(
    as_of_date, codes, use_ttm: bool = False, neutralize: str = "size",
):
    """_fundamentals_provider 결과에 중립화 축 컬럼(시가총액/업종)을 덧붙인다.

    :param neutralize: "size"(시가총액만) / "sector"(업종명만) / "size_sector"(둘 다).
        krx_index.market_caps·sector_map(둘 다 PIT)를 재사용한다. 미인증/실패로 해당
        축이 비면 그 컬럼만 붙이지 않으므로 스코어러가 그 축의 중립화를 자연히
        생략한다(순수 팩터 그대로, §20).
    """
    from app.services.data import krx_index

    fdf = _fundamentals_provider(as_of_date, codes, use_ttm=use_ttm)
    norm_codes = [str(c).zfill(6) for c in codes]
    cols: dict[str, pd.Series] = {}

    if neutralize in ("size", "size_sector"):
        try:
            caps = krx_index.market_caps(as_of_date)
        except Exception:  # noqa: BLE001
            caps = {}
        if caps:
            cols["market_cap"] = pd.Series(
                {c: caps.get(c) for c in norm_codes}, dtype="float64"
            )

    if neutralize in ("sector", "size_sector"):
        try:
            smap = krx_index.sector_map(as_of_date)
        except Exception:  # noqa: BLE001
            smap = {}
        if smap:
            cols["sector"] = pd.Series(
                {c: smap.get(c) for c in norm_codes}, dtype="object"
            )

    if not cols:
        return fdf
    extra = pd.DataFrame(cols)
    if fdf is None:
        return extra
    fdf = fdf.copy()
    for col, s in cols.items():
        fdf[col] = s.reindex(fdf.index)
    return fdf


def _provider_with_flow(base_provider, window: int, denom: str):
    """base_provider 결과에 수급(flow) 팩터 컬럼(flow_norm)을 덧붙이는 provider 를 만든다.

    factor_weights.flow>0 인 score 전략 백테스트에서만 쓴다. metrics.compute_flow_norm
    (외국인+기관 누적 순매수/정규화 분모)을 as_of 시점으로 조회한다. 조회 실패/부재면
    flow_norm 컬럼을 붙이지 않으므로 스코어러가 자연히 중립(0) 처리한다.
    """
    from app.services.metrics import compute_flow_norm

    def _p(as_of_date, codes):
        norm_codes = [str(c).zfill(6) for c in codes]
        fdf = base_provider(as_of_date, codes) if base_provider is not None else None
        try:
            flow = compute_flow_norm(norm_codes, as_of_date, window=window, denom=denom)
        except Exception:  # noqa: BLE001
            flow = None
        if flow is None or flow.empty:
            return fdf
        s = flow.reindex(norm_codes)
        if fdf is None:
            return pd.DataFrame({"flow_norm": s})
        fdf = fdf.copy()
        fdf["flow_norm"] = s.reindex(fdf.index)
        return fdf

    return _p


def _provider_with_resid_mom(base_provider, panel, market_close, reg_window, mom_window, skip):
    """base_provider 결과에 잔차(베타 조정) 모멘텀 컬럼(resid_mom)을 덧붙이는 provider.

    factor_weights.residual_momentum>0 인 score 전략 백테스트에서만 쓴다. 이미 적재한
    종가 패널(panel)과 벤치마크(KOSPI200) 종가(market_close)를 재사용해
    compute_residual_momentum_panel 로 as_of 시점 잔차 모멘텀을 산출한다(외부 조회 없음).
    시장 지수(market_close)가 없거나 데이터 부족이면 컬럼을 붙이지 않아 스코어러가 중립
    처리한다. 미래참조 없음: 함수 내부가 as_of 이하 종가만 사용한다.
    """
    from app.services.metrics.factors import compute_residual_momentum_panel

    def _p(as_of_date, codes):
        norm_codes = [str(c).zfill(6) for c in codes]
        fdf = base_provider(as_of_date, codes) if base_provider is not None else None
        if market_close is None or panel is None or panel.empty:
            return fdf
        try:
            rm = compute_residual_momentum_panel(
                panel, market_close, as_of_date, codes=norm_codes,
                reg_window=reg_window, mom_window=mom_window, skip=skip,
            )
        except Exception:  # noqa: BLE001
            rm = None
        if rm is None or rm.empty:
            return fdf
        s = rm.reindex(norm_codes)
        if fdf is None:
            return pd.DataFrame({"resid_mom": s})
        fdf = fdf.copy()
        fdf["resid_mom"] = s.reindex(fdf.index)
        return fdf

    return _p


def _provider_with_pead(base_provider, lookback_q: int):
    """base_provider 결과에 PEAD 팩터 컬럼(pead_sue)을 덧붙이는 provider 를 만든다.

    factor_weights.pead>0 인 score 전략 백테스트에서만 쓴다. metrics.compute_pead_sue
    (OpenDART 정기공시 접수일 PIT 로 단일분기 순이익 YoY 서프라이즈 표준화)를 as_of
    시점으로 조회한다. 조회 실패/부재면 pead_sue 컬럼을 붙이지 않으므로 스코어러가
    자연히 중립(0) 처리한다. 미래참조 없음: 함수 내부가 rcept_dt<=as_of 만 사용한다.
    """
    from app.services.metrics import compute_pead_sue

    def _p(as_of_date, codes):
        norm_codes = [str(c).zfill(6) for c in codes]
        fdf = base_provider(as_of_date, codes) if base_provider is not None else None
        try:
            pead = compute_pead_sue(norm_codes, as_of_date, lookback_q=lookback_q)
        except Exception:  # noqa: BLE001
            pead = None
        if pead is None or pead.empty:
            return fdf
        s = pead.reindex(norm_codes)
        if fdf is None:
            return pd.DataFrame({"pead_sue": s})
        fdf = fdf.copy()
        fdf["pead_sue"] = s.reindex(fdf.index)
        return fdf

    return _p


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

    # 인증 없이 돌면 월별 조회가 전부 빈 값을 주고 백테스트가 빈 패널 위에서
    # '성공'한다(§44-1). 19개월치를 다 돌기 전에 막는다.
    krx_index.require_krx_auth()

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
    if not union:
        # 조용히 넘어가면 빈 패널 위에서 백테스트가 '성공'하며 무의미한 수치를 낸다
        # (KRX 로그인 차단 시 실제로 관측됨 — 모든 월이 0종목). 소리내어 남긴다.
        logger.error(
            "PIT 후보풀이 비었다(source=%s, %s~%s, %d개월 전부 0종목) — "
            "KRX 인증 실패 가능성. 이 상태로 만든 백테스트 결과는 신뢰할 수 없다.",
            source, start, end, len(by_month),
        )

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

    # 각 종목 종가 시드 — price_ticks 커버리지 확인 후 부족한 만큼만 외부 소스로 보충
    # (C-1 — 야간 배치로 이미 적재돼 있으면 외부 조회를 건너뛰어 반복 백테스트가 빨라진다).
    columns: dict[str, pd.Series] = {}
    for sym in universe:
        await ensure_ohlcv_coverage(db, sym, warmup_dt, end_dt)
        series = await get_close_series(db, sym, warmup_dt, end_dt)
        if not series.empty:
            columns[sym] = series

    if not columns:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "universe 종목의 시세를 확보하지 못했습니다."
        )

    panel = pd.DataFrame(columns)

    # 체결 정밀도(P2-2 A-2): ADV 참여율 캡이 설정돼 있으면 같은 종목들의 거래량도
    # 적재해 20일 ADV(거래대금) 산출용 패널을 만든다. 미설정이면 조회 자체를 건너뛴다
    # (기존 백테스트 성능·부하에 영향 없음).
    volume_panel = None
    if config.get("adv_participation_cap"):
        vol_columns: dict[str, pd.Series] = {}
        for sym in panel.columns:
            vseries = await get_volume_series(db, sym, warmup_dt, end_dt)
            if not vseries.empty:
                vol_columns[sym] = vseries
        if vol_columns:
            volume_panel = pd.DataFrame(vol_columns)
        else:
            logger.warning("ADV 캡 설정됨이나 거래량 데이터를 확보하지 못했다(미적용).")

    method = config.get("selection", {}).get("method", "momentum")
    use_ttm = config.get("financial_period", "annual") == "ttm"
    provider = partial(_fundamentals_provider, use_ttm=use_ttm) if method == "score" else None
    # 중립화(P1-3 사이즈, §20 섹터): 시가총액·업종(PIT)을 펀더멘털 프레임에 실어
    # 스코어러가 각 팩터를 해당 축에 직교화하게 한다. 중립화가 꺼진 전략(neutralize=
    # "none")에는 추가 조회를 하지 않는다.
    _neutralize = config.get("selection", {}).get("neutralize", "none")
    if provider is not None and _neutralize in ("size", "sector", "size_sector"):
        provider = partial(
            _fundamentals_provider_with_neutralize_cols, use_ttm=use_ttm, neutralize=_neutralize,
        )
    # 수급(flow) 팩터: factor_weights.flow>0 이면 provider 에 flow_norm 컬럼을 덧붙인다.
    if method == "score":
        _sel = config.get("selection", {})
        _flow_w = float((_sel.get("factor_weights") or {}).get("flow", 0.0) or 0.0)
        if _flow_w > 0:
            provider = _provider_with_flow(
                provider,
                int(_sel.get("flow_window", 90)),
                _sel.get("flow_denom", "mcap"),
            )

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

    # 잔차(베타 조정) 모멘텀 팩터: factor_weights.residual_momentum>0 이면 provider 에
    # resid_mom 컬럼을 덧붙인다(적재한 종가 패널 + 벤치마크 재사용, 외부 조회 없음). 벤치마크
    # 적재 이후에 배선해야 시장 회귀 소스를 확보한다.
    if method == "score":
        _sel = config.get("selection", {})
        _rm_w = float((_sel.get("factor_weights") or {}).get("residual_momentum", 0.0) or 0.0)
        if _rm_w > 0:
            provider = _provider_with_resid_mom(
                provider, panel, benchmark_series,
                int(_sel.get("resid_mom_reg_window", 36)),
                int(_sel.get("resid_mom_window", 11)),
                int(_sel.get("resid_mom_skip", 1)),
            )
        # PEAD 팩터: factor_weights.pead>0 이면 provider 에 pead_sue 컬럼을 덧붙인다
        # (OpenDART 정기공시 접수일 PIT 조회).
        _pead_w = float((_sel.get("factor_weights") or {}).get("pead", 0.0) or 0.0)
        if _pead_w > 0:
            provider = _provider_with_pead(provider, int(_sel.get("pead_lookback_q", 8)))

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
            volume_panel,
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

    # 유니버스 지문(§22, DSR 동질 시행 집합 정밀화): 실행 시점의 실제 유니버스(PIT
    # 해소 결과 포함)+universe_rule 파라미터를 해시로 남긴다. 전략 config 는 수정
    # 가능한 가변 객체라 백테스트 시점의 유니버스를 사후에 복원할 수 없으므로,
    # 실행 시점에 저장해두지 않으면 영영 알 수 없다.
    result["universe_fingerprint"] = _universe_fingerprint(
        universe, config.get("selection", {}).get("universe_rule")
    )
    return result


def _universe_fingerprint(universe: list[str], universe_rule: dict | None) -> str:
    """실행 시점 유니버스(정렬된 종목코드 리스트)+universe_rule 파라미터의 해시(§22).

    DSR 동질 시행 집합 필터에서 "같은 유니버스로 실행된 백테스트인지"를 판별하는 데
    쓴다. 종목 구성(동적 유니버스는 PIT 해소 결과가 실행마다 달라질 수 있음)과 규칙
    파라미터 둘 다 동일해야 같은 지문이 나오도록 둘 다 포함한다.
    """
    import hashlib
    import json

    payload = json.dumps(
        {"universe": sorted(universe), "universe_rule": universe_rule or {}},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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


@router.get("/backtests/{backtest_id}/dsr")
async def get_backtest_dsr(
    backtest_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """백테스트 1건의 Deflated Sharpe Ratio(DSR)를 온디맨드 계산해 반환한다.

    과최적화(다중검정 selection bias) 방어 사후 분석(Bailey & López de Prado 2014).
    동질 시행 집합 = 같은 strategy_id + 같은 period_start/period_end 의 Backtest 이력
    (파라미터 탐색 시행들)로 N·V·N_eff 를 추정한다. 계산은 CPU 성 순수함수라 매우
    가볍지만, 일관성을 위해 스레드풀에서 실행한다. 전략 소유자 본인만 접근 가능.
    """
    bt = await db.scalar(
        select(Backtest)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .where(Backtest.id == backtest_id, Strategy.user_id == current.id)
    )
    if bt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "백테스트를 찾을 수 없습니다.")
    if not bt.result:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "백테스트 결과 데이터가 없습니다."
        )

    # 동질 집합: 같은 전략 + 같은 기간(period_start/period_end)의 모든 백테스트 이력을
    # 1차 후보로 잡고, 대상에 유니버스 지문(§22)이 있으면 지문까지 일치하는 행으로
    # 더 좁힌다 — 서로 다른 유니버스/설정의 백테스트가 섞여 N 과대·V 왜곡되는 것을
    # 막는다. 지문이 없는 과거 행(§22 도입 이전 실행)은 대상 자체에 지문이 없을 때만
    # 기간 필터로 폴백한다(하위호환 — 신규 백테스트가 쌓일수록 집합 순도가 개선된다).
    rows = await db.scalars(
        select(Backtest).where(
            Backtest.strategy_id == bt.strategy_id,
            Backtest.period_start == bt.period_start,
            Backtest.period_end == bt.period_end,
        )
    )
    candidates = [r.result for r in rows if r.result]
    target_fp = (bt.result or {}).get("universe_fingerprint")
    if target_fp:
        homogeneous = [r for r in candidates if r.get("universe_fingerprint") == target_fp]
    else:
        homogeneous = candidates

    analysis = await run_in_threadpool(analyze_backtest_dsr, bt.result, homogeneous)
    return {
        "backtest_id": bt.id,
        "strategy_id": bt.strategy_id,
        "period_start": bt.period_start,
        "period_end": bt.period_end,
        **analysis,
    }
