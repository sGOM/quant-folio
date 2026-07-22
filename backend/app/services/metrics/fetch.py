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
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.services.data.loader import bounded_socket_timeout  # pykrx 무응답 행 방지

logger = logging.getLogger("app.services.metrics")


def _pykrx_stock():
    """pykrx.stock 모듈을 지연 로딩한다.

    pykrx 임포트는 블로킹이므로 (모듈 import 시점이 아니라) 실제 조회 호출
    컨텍스트에서만 로딩되도록 지연시킨다. 모든 조회 헬퍼가 이 한 곳을 경유한다.
    """
    from pykrx import stock

    return stock


def _fetch_per_market(
    fetch_one: Callable[[Any, str], pd.DataFrame | None],
    mkts: list[str],
    *,
    what: str,
    when: str,
    empty_columns: list[str] | None = None,
) -> pd.DataFrame:
    """시장별 조회를 "루프 → 실패 경고 → concat" 골격으로 일반화한다.

    `fetch_one(stock, mkt)` 이 시장별 DataFrame(또는 None/빈 프레임=건너뜀)을 반환한다.
    개별 시장 실패는 경고 로그 후 건너뛰고, 전부 실패하면 빈 프레임을 반환한다
    (`empty_columns` 지정 시 해당 컬럼 스켈레톤).

    :param what: 실패 로그용 데이터 명칭(예: "펀더멘털")
    :param when: 실패 로그용 기간/일자 표기(예: as_of, "start~end")
    """
    stock = _pykrx_stock()
    frames: list[pd.DataFrame] = []
    for mkt in mkts:
        try:
            df = fetch_one(stock, mkt)
            if df is None or df.empty:
                continue
            frames.append(df)
        except Exception:
            logger.warning("%s 조회 실패 (%s %s)", what, mkt, when, exc_info=True)

    if not frames:
        return pd.DataFrame(columns=empty_columns) if empty_columns else pd.DataFrame()
    return pd.concat(frames)

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

    def _one(stock, mkt: str) -> pd.DataFrame | None:
        df = stock.get_market_fundamental(as_of_ymd, market=mkt)
        if df is None or df.empty:
            return None
        df = df[["PER", "PBR", "DIV"]].copy()
        df["market"] = mkt
        # PER=0 → NaN 처리 (pykrx 는 적자 종목에 0 반환)
        df.loc[df["PER"] <= 0, "PER"] = np.nan
        return df

    result = _fetch_per_market(
        _one, mkts, what="펀더멘털", when=as_of_ymd,
        empty_columns=["PER", "PBR", "DIV", "market"],
    )
    if result.empty:
        # 빈 결과는 캐시하지 않는다(일시적 조회 실패를 영구화하지 않기 위함).
        return result

    _FUND_CACHE[key] = result.copy()
    _FUND_CACHE.move_to_end(key)
    if len(_FUND_CACHE) > _FUND_CACHE_MAX:
        _FUND_CACHE.popitem(last=False)  # 가장 오래된 항목 축출
    return result


def _fetch_market_cap(as_of_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """전 종목 시가총액·상장주식수·거래대금을 조회한다.

    컬럼: 시가총액, 상장주식수, 거래대금. 티커 인덱스.
    """
    def _one(stock, mkt: str) -> pd.DataFrame | None:
        df = stock.get_market_cap(as_of_ymd, market=mkt)
        if df is None or df.empty:
            return None
        # 필요 컬럼만 선택 (버전 차이 대비)
        cols = [c for c in ["시가총액", "거래량", "거래대금", "상장주식수"] if c in df.columns]
        return df[cols]

    return _fetch_per_market(_one, mkts, what="시가총액", when=as_of_ymd)


def _fetch_price_change(start_ymd: str, end_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """기간 등락률·거래대금을 전 종목 일괄 조회한다.

    컬럼: 시가, 종가, 변동폭, 등락률, 거래량, 거래대금. 티커 인덱스.
    등락률은 pykrx 원값 그대로(%) — 호출자가 /100으로 변환한다.
    """
    def _one(stock, mkt: str) -> pd.DataFrame | None:
        return stock.get_market_price_change(start_ymd, end_ymd, market=mkt)

    return _fetch_per_market(
        _one, mkts, what="가격변동", when=f"{start_ymd}~{end_ymd}",
    )


#: 수급(flow) 팩터 기본 투자자군 — 외국인 + 기관합계 순매수. KRX 투자자 분류 라벨.
_FLOW_INVESTORS: tuple[str, ...] = ("외국인", "기관합계")


def _fetch_net_purchases(
    start_ymd: str,
    end_ymd: str,
    mkts: list[str],
    investors: tuple[str, ...] = _FLOW_INVESTORS,
) -> pd.DataFrame:
    """기간 [start_ymd, end_ymd] 누적 투자자별 순매수 대금을 전 종목 일괄 조회한다(수급 팩터).

    pykrx get_market_net_purchases_of_equities(fromdate, todate, market, investor) 는
    한 번 호출로 그 시장 전 종목의 기간 누적 순매수(거래량·거래대금)를 투자자군별로
    반환한다(get_market_price_change 와 같은 '시장당 1회' 급 비용). 외국인+기관합계 두
    투자자군을 합산해 종목별 순매수 '대금'(원)만 남긴다.

    반환: 티커 인덱스, 컬럼 ["net_buy_value"](외국인+기관 순매수거래대금 합, 원).
    개별 (시장, 투자자) 조회 실패는 경고 후 건너뛰고(그 부분만 결측 → 호출자가 중립
    처리), 전량 실패면 빈 프레임을 반환한다(호출자가 리밸런싱 스킵 판단).

    미래참조 방지: 호출자가 end_ymd 를 as_of(직전 확정 영업일) 이하로 넘겨야 한다.
    """
    stock = _pykrx_stock()
    # 종목별 순매수거래대금을 (시장, 투자자)에 걸쳐 누적 합산한다.
    accum: dict[str, float] = {}
    any_ok = False
    for mkt in mkts:
        for investor in investors:
            try:
                df = stock.get_market_net_purchases_of_equities(
                    start_ymd, end_ymd, mkt, investor
                )
                if df is None or df.empty or "순매수거래대금" not in df.columns:
                    continue
                any_ok = True
                vals = pd.to_numeric(df["순매수거래대금"], errors="coerce")
                for ticker, v in vals.items():
                    if pd.isna(v):
                        continue
                    key = str(ticker).zfill(6)
                    accum[key] = accum.get(key, 0.0) + float(v)
            except Exception:
                logger.warning(
                    "투자자별 순매수 조회 실패 (%s %s %s~%s)",
                    mkt, investor, start_ymd, end_ymd, exc_info=True,
                )

    if not any_ok or not accum:
        return pd.DataFrame(columns=["net_buy_value"])
    out = pd.DataFrame.from_dict(accum, orient="index", columns=["net_buy_value"])
    out.index.name = "티커"
    return out


def _fetch_market_ohlcv_snapshot(date_ymd: str, mkt: str) -> pd.DataFrame | None:
    """단일 거래일의 전 종목 OHLCV 스냅샷을 조회한다(패닉셀 S9 신저가 브레드스 등).

    컬럼: 시가/고가/저가/종가/거래량/거래대금/등락률/시가총액. 티커 인덱스.
    기간 조회(_fetch_price_change)와 달리 "그 날짜 하루"만 반환하므로, 여러 날짜를
    누적하면(캐시) 종목별 종가 시계열을 재구성할 수 있다. 1회 호출로 시장 전체를
    받아오므로 브레드스 계열 호출과 비용이 같은 급(시장당 1회)이다.
    """
    stock = _pykrx_stock()
    try:
        with bounded_socket_timeout(20):
            df = stock.get_market_ohlcv(date_ymd, market=mkt)
        if df is None or df.empty:
            return None
        return df
    except Exception:
        logger.warning("전종목 OHLCV 스냅샷 조회 실패 (%s %s)", mkt, date_ymd, exc_info=True)
        return None


def _fetch_index_ohlcv(start_ymd: str, end_ymd: str, ticker: str) -> pd.DataFrame | None:
    """업종지수 OHLCV를 조회한다. 실패 시 None 반환.

    pykrx 한글 컬럼 → 영문 변환:
      시가→open, 고가→high, 저가→low, 종가→close,
      거래량→volume, 거래대금→trading_value
    """
    stock = _pykrx_stock()

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
        # 패닉·섹터 지표의 핵심 입력이므로 원인 스택을 운영 로그(warning)에 남긴다.
        logger.warning("업종지수 OHLCV 조회 실패 (%s)", ticker, exc_info=True)
        return None


def _fetch_index_tickers(date_ymd: str, mkt: str) -> list[str]:
    """업종지수 코드 목록을 반환한다."""
    stock = _pykrx_stock()

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
    stock = _pykrx_stock()

    try:
        # pykrx 버전에 따라 인자 형식이 다를 수 있으므로 두 가지 시도
        try:
            name = stock.get_index_ticker_name(ticker)
        except TypeError:
            name = stock.get_index_ticker_name(date_ymd, ticker)
        return str(name).strip() if name else ticker
    except Exception:
        return ticker
