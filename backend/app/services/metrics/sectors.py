"""섹터(업종지수) 지표 계산 — 모멘텀·변동성·RS_126·거래대금 추세.

pykrx KRX 업종지수를 이용해 모멘텀·변동성·상대강도(RS)를 산출한다. 업종지수 RS_126 은
동일 시장(KOSPI/KOSDAQ) 종합지수를 분모로 계산한다.
"""
from __future__ import annotations

import logging
from datetime import date

from app.schemas.metrics import SectorMetric, SectorsOut
from app.services.data.errors import DataSourceError, representative
from app.services.metrics.common import (
    _approx_start,
    _compute_mdd,
    _compute_vol_ann,
    _mkts,
    _safe_float,
    _ymd,
)
from app.services.metrics.fetch import (
    _fetch_index_ohlcv,
    _fetch_index_tickers,
    _get_index_name,
)

logger = logging.getLogger("app.services.metrics")

# Redis 캐시 TTL (6시간 — EOD 스냅샷은 자주 변하지 않음)
SECTORS_CACHE_TTL = 6 * 3600

# 기준 업종지수: RS_126 분모 (pykrx 업종지수 코드)
_KOSPI_REF_TICKER = "1001"   # KOSPI 종합지수
_KOSDAQ_REF_TICKER = "2001"  # KOSDAQ 종합지수


def compute_sectors(market: str, as_of: date) -> SectorsOut:
    """섹터(업종지수) 지표를 계산해 반환한다.

    pykrx KRX 업종지수를 이용해 모멘텀·변동성·RS·밸류(구성종목 중앙값)를 산출한다.
    동기(블로킹) 함수이므로 호출자는 asyncio.to_thread로 실행해야 한다.
    """
    from pykrx import stock as _stock  # noqa: F401 — 임포트 확인용

    as_of_ymd = _ymd(as_of)
    mkts_to_fetch = _mkts(market)

    # 필요한 과거 기간: 252영업일 + 여유 → 약 400 캘린더일
    hist_start = _approx_start(as_of, 252, buffer=30)
    hist_start_ymd = _ymd(hist_start)

    # ── 기준 시장지수 6M 수익률 (RS_126 분모) ──
    ref_returns: dict[str, float | None] = {}
    for ref_mkt, ref_ticker in [("KOSPI", _KOSPI_REF_TICKER), ("KOSDAQ", _KOSDAQ_REF_TICKER)]:
        df_ref = _fetch_index_ohlcv(hist_start_ymd, as_of_ymd, ref_ticker)
        if df_ref is not None and "close" in df_ref.columns and len(df_ref) >= 126:
            c = df_ref["close"]
            ref_ret = _safe_float((c.iloc[-1] / c.iloc[-126] - 1))
        else:
            ref_ret = None
        ref_returns[ref_mkt] = ref_ret

    # ── 업종지수별 지표 계산 ──
    items: list[SectorMetric] = []
    ticker_list_errors: list[DataSourceError] = []

    for mkt in mkts_to_fetch:
        try:
            tickers = _fetch_index_tickers(as_of_ymd, mkt)
        except DataSourceError as e:
            logger.warning("업종지수 목록 조회 실패로 시장 건너뜀 (%s): %s", mkt, e)
            ticker_list_errors.append(e)
            continue
        ref_mkt = mkt  # KOSPI 업종 → KOSPI 기준, KOSDAQ 업종 → KOSDAQ 기준

        for ticker in tickers:
            # 기준 시장지수 자체는 섹터 목록에서 제외
            if ticker in (_KOSPI_REF_TICKER, _KOSDAQ_REF_TICKER):
                continue

            try:
                metric = _compute_one_sector(
                    ticker=ticker,
                    mkt=mkt,
                    date_ymd=as_of_ymd,
                    hist_start_ymd=hist_start_ymd,
                    ref_return=ref_returns.get(ref_mkt),
                )
                if metric is not None:
                    items.append(metric)
            except Exception:
                logger.warning("섹터 계산 오류 (ticker=%s %s)", ticker, mkt, exc_info=True)

    if ticker_list_errors and not items:
        raise representative(ticker_list_errors)

    return SectorsOut(as_of=as_of, items=items)


def _compute_one_sector(
    ticker: str,
    mkt: str,
    date_ymd: str,
    hist_start_ymd: str,
    ref_return: float | None,
) -> SectorMetric | None:
    """업종지수 1개의 지표를 계산한다."""
    try:
        df = _fetch_index_ohlcv(hist_start_ymd, date_ymd, ticker)
    except DataSourceError as e:
        logger.warning("업종지수 건너뜀 (%s): %s", ticker, e)
        return None
    if df is None or "close" not in df.columns or len(df) < 21:
        return None

    name = _get_index_name(date_ymd, ticker)
    c = df["close"]

    def _mom(n: int) -> float | None:
        if len(c) < n:
            return None
        return _safe_float(c.iloc[-1] / c.iloc[-n] - 1)

    mom_1m = _mom(21)
    mom_3m = _mom(63)
    mom_6m = _mom(126)

    # RS_126: 섹터 6M ÷ 기준 시장 6M
    rs_126: float | None = None
    if mom_6m is not None and ref_return is not None and abs(ref_return) > 1e-9:
        rs_126 = _safe_float(mom_6m / ref_return)

    # 연율 변동성
    vol_ann = _compute_vol_ann(c.tail(253))

    # 위험 조정 모멘텀
    risk_adj_mom: float | None = None
    if mom_6m is not None and vol_ann and vol_ann > 1e-9:
        risk_adj_mom = _safe_float(mom_6m / vol_ann)

    # 거래대금 추세 (MA20 ÷ MA60)
    value_trend: float | None = None
    if "trading_value" in df.columns and len(df) >= 60:
        tv = df["trading_value"]
        ma20 = tv.rolling(20).mean().iloc[-1]
        ma60 = tv.rolling(60).mean().iloc[-1]
        if ma60 > 0:
            value_trend = _safe_float(ma20 / ma60)

    # 52주 고가 대비
    high_52w_ratio: float | None = None
    if len(c) >= 252:
        hi = c.tail(252).max()
        if hi > 0:
            high_52w_ratio = _safe_float(c.iloc[-1] / hi)

    # MDD_252
    mdd_252 = _compute_mdd(c.tail(252)) if len(c) >= 2 else None

    # 섹터 밸류(PER/PBR/배당) 중앙값은 업종 구성종목 API 가 현재 pykrx 버전에서
    # 빈 값을 반환해 산출 불가하므로 계산하지 않는다(응답 스키마에서도 제외).

    return SectorMetric(
        name=name,
        market=mkt,
        mom_1m=mom_1m,
        mom_3m=mom_3m,
        mom_6m=mom_6m,
        rs_126=rs_126,
        vol_ann=vol_ann,
        risk_adj_mom=risk_adj_mom,
        value_trend=value_trend,
        high_52w_ratio=high_52w_ratio,
        mdd_252=mdd_252,
    )
