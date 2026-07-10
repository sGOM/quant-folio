"""pykrx 일괄 조회 헬퍼 — 전 종목 펀더멘털/시총/등락률, 업종지수 OHLCV·목록·이름.

## 데이터 소스
- get_market_fundamental  : 전 종목 PER/PBR/DIV (한 번에)
- get_market_cap           : 전 종목 시가총액/거래대금 (한 번에)
- get_market_price_change  : 기간 등락률·거래대금 (모멘텀 일괄)
- get_index_ticker_list / get_index_ohlcv : 업종지수
- get_index_portfolio_deposit_file       : 업종 구성종목

모든 pykrx 호출은 블로킹이므로 호출자(라우트)가 asyncio.to_thread 로 실행한다.
`from pykrx import stock` 지연 임포트는 스레드 호출 컨텍스트에서만 로딩하기 위함이다.
"""
from __future__ import annotations

import logging
from collections import OrderedDict

import numpy as np
import pandas as pd

from app.services.data.loader import bounded_socket_timeout  # pykrx 무응답 행 방지

logger = logging.getLogger("app.services.metrics")

# 펀더멘털(PER/PBR/DIV) 프로세스 내 LRU 캐시.
# 특정 as_of 일자의 전 종목 펀더멘털은 확정 후 변하지 않으므로 (as_of, 시장) 단위로
# 캐시하면 동일/유사 기간을 반복 백테스트할 때 pykrx 왕복을 크게 줄인다
# (예: score 방식 리밸런싱은 리밸런싱일마다 as_of 스냅샷을 조회한다). 워커 프로세스
# 수명 동안 유지되며, 실패(빈 결과)는 캐시하지 않아 다음 호출 때 재시도된다.
_FUND_CACHE: "OrderedDict[tuple, pd.DataFrame]" = OrderedDict()
_FUND_CACHE_MAX = 64  # 스냅샷 상한(초과 시 가장 오래된 항목 축출)


def _fetch_fundamentals(as_of_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """전 종목 펀더멘털(PER/PBR/DIV)을 시장별로 조회해 합산한다.

    컬럼: PER, PBR, DIV + market(추가). 티커 인덱스.
    PER=0은 적자(undefined)로 간주해 음수로 취급한다.
    (as_of, 시장) 키로 프로세스 내 LRU 캐시하며, 호출자가 결과를 변형해도 캐시가
    오염되지 않도록 항상 사본을 반환한다.
    """
    key = (as_of_ymd, tuple(sorted(mkts)))
    cached = _FUND_CACHE.get(key)
    if cached is not None:
        _FUND_CACHE.move_to_end(key)  # 최근 사용으로 갱신
        return cached.copy()

    from pykrx import stock  # 블로킹 임포트 지연(스레드 호출 컨텍스트)

    frames: list[pd.DataFrame] = []
    for mkt in mkts:
        try:
            df = stock.get_market_fundamental(as_of_ymd, market=mkt)
            if df is None or df.empty:
                continue
            df = df[["PER", "PBR", "DIV"]].copy()
            df["market"] = mkt
            # PER=0 → NaN 처리 (pykrx 는 적자 종목에 0 반환)
            df.loc[df["PER"] <= 0, "PER"] = np.nan
            frames.append(df)
        except Exception:
            logger.warning("펀더멘털 조회 실패 (%s %s)", mkt, as_of_ymd, exc_info=True)

    if not frames:
        # 빈 결과는 캐시하지 않는다(일시적 조회 실패를 영구화하지 않기 위함).
        return pd.DataFrame(columns=["PER", "PBR", "DIV", "market"])

    result = pd.concat(frames)
    _FUND_CACHE[key] = result.copy()
    _FUND_CACHE.move_to_end(key)
    if len(_FUND_CACHE) > _FUND_CACHE_MAX:
        _FUND_CACHE.popitem(last=False)  # 가장 오래된 항목 축출
    return result


def _fetch_market_cap(as_of_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """전 종목 시가총액·상장주식수·거래대금을 조회한다.

    컬럼: 시가총액, 상장주식수, 거래대금. 티커 인덱스.
    """
    from pykrx import stock

    frames: list[pd.DataFrame] = []
    for mkt in mkts:
        try:
            df = stock.get_market_cap(as_of_ymd, market=mkt)
            if df is None or df.empty:
                continue
            # 필요 컬럼만 선택 (버전 차이 대비)
            cols = [c for c in ["시가총액", "거래량", "거래대금", "상장주식수"] if c in df.columns]
            frames.append(df[cols])
        except Exception:
            logger.warning("시가총액 조회 실패 (%s %s)", mkt, as_of_ymd, exc_info=True)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames)


def _fetch_price_change(start_ymd: str, end_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """기간 등락률·거래대금을 전 종목 일괄 조회한다.

    컬럼: 시가, 종가, 변동폭, 등락률, 거래량, 거래대금. 티커 인덱스.
    등락률은 pykrx 원값 그대로(%) — 호출자가 /100으로 변환한다.
    """
    from pykrx import stock

    frames: list[pd.DataFrame] = []
    for mkt in mkts:
        try:
            df = stock.get_market_price_change(start_ymd, end_ymd, market=mkt)
            if df is None or df.empty:
                continue
            frames.append(df)
        except Exception:
            logger.warning("가격변동 조회 실패 (%s~%s %s)", start_ymd, end_ymd, mkt, exc_info=True)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames)


def _fetch_index_ohlcv(start_ymd: str, end_ymd: str, ticker: str) -> pd.DataFrame | None:
    """업종지수 OHLCV를 조회한다. 실패 시 None 반환.

    pykrx 한글 컬럼 → 영문 변환:
      시가→open, 고가→high, 저가→low, 종가→close,
      거래량→volume, 거래대금→trading_value
    """
    from pykrx import stock

    try:
        with bounded_socket_timeout(20):
            df = stock.get_index_ohlcv(start_ymd, end_ymd, ticker)
        if df is None or df.empty:
            return None
        df = df.rename(columns={
            "시가": "open", "고가": "high", "저가": "low", "종가": "close",
            "거래량": "volume", "거래대금": "trading_value",
        })
        return df
    except Exception:
        logger.debug("업종지수 OHLCV 조회 실패 (%s)", ticker, exc_info=True)
        return None


def _fetch_index_tickers(date_ymd: str, mkt: str) -> list[str]:
    """업종지수 코드 목록을 반환한다."""
    from pykrx import stock

    try:
        result = stock.get_index_ticker_list(date=date_ymd, market=mkt)
        if result is None:
            return []
        return list(result)
    except Exception:
        logger.warning("업종지수 목록 조회 실패 (%s %s)", mkt, date_ymd, exc_info=True)
        return []


def _get_index_name(date_ymd: str, ticker: str) -> str:
    """업종지수 이름을 반환한다. 실패 시 ticker 코드를 반환."""
    from pykrx import stock

    try:
        # pykrx 버전에 따라 인자 형식이 다를 수 있으므로 두 가지 시도
        try:
            name = stock.get_index_ticker_name(ticker)
        except TypeError:
            name = stock.get_index_ticker_name(date_ymd, ticker)
        return str(name).strip() if name else ticker
    except Exception:
        return ticker
