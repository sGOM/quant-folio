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
from datetime import date, datetime
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.services.data.errors import (
    DataSourceError,
    SourceUnavailableError,
    note_failure,
    representative,
    stop_aggregate,
)
from app.services.data.loader import bounded_socket_timeout  # pykrx 무응답 행 방지
from app.services.data.store import daily as _daily
from app.services.data.store import indexes as _indexes
from app.services.data.store import periods as _periods
from app.services.data.store.frame import cached_frame, is_final_date, make_cache_key
from app.services.data.store.ledger import default_ledger

logger = logging.getLogger("app.services.metrics")


def _pykrx_stock():
    """pykrx.stock 모듈을 지연 로딩한다.

    pykrx 임포트는 블로킹이므로 (모듈 import 시점이 아니라) 실제 조회 호출
    컨텍스트에서만 로딩되도록 지연시킨다. 모든 조회 헬퍼가 이 한 곳을 경유한다.
    """
    from pykrx import stock

    return stock


# 스토어 접근을 모듈 수준 얇은 래퍼로 감싼다 — 테스트가 실제 DB 없이 갈아끼울 수 있게.
def _store_ledger():
    return default_ledger()


def _store_write_daily(day, df, columns):
    _daily.write_daily(day, df, columns=columns)


def _store_read_daily(day, cols, out_columns, markets=None):
    return _daily.read_daily(day, cols, out_columns=out_columns, markets=markets)


def _store_write_periods(start, end, investors, df, columns):
    _periods.write_periods(start, end, investors, df, columns=columns)


def _store_read_periods(start, end, investors, cols, out_columns, markets=None):
    return _periods.read_periods(
        start, end, investors, cols, out_columns=out_columns, markets=markets
    )


def _store_write_index_ohlcv(code, df, name):
    _indexes.write_index_ohlcv(code, df, index_name=name)


def _store_read_index_ohlcv(code, start, end):
    return _indexes.read_index_ohlcv(code, start, end)


def _ymd_to_date(ymd: str) -> date:
    """'20190312' → date(2019, 3, 12)."""
    return datetime.strptime(ymd, "%Y%m%d").date()


def _fetch_per_market(
    fetch_one: Callable[[Any, str], pd.DataFrame | None],
    mkts: list[str],
    *,
    what: str,
    when: str,
    source: str,
    empty_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, bool]:
    """시장별 조회를 "루프 → 실패 수집 → concat" 골격으로 일반화한다.

    이전 판은 개별 시장 실패를 경고만 남기고 삼켜, **전 시장이 실패해도 빈 프레임을
    반환**했다. §44-1(KRX 차단)·§47(폐기된 검증)에서 백테스트가 빈 패널 위에서
    '성공'하며 무의미한 수치를 낸 원인이 이것이다.

    이제 전량 실패는 `representative()` 가 고른 대표 예외로 raise 한다. 일부만
    실패하면 성공분을 돌려주되 `complete=False` 로 알려, 호출자가 그 결과를 확정으로
    굳히지 않게 한다(다음 호출에서 빠진 시장을 보완할 수 있어야 한다).

    :param source: 쿨다운·집계 단락 판정에 쓰는 소스 식별자
    :returns: (합친 프레임, 전 시장 성공 여부)
    """
    stock = _pykrx_stock()
    frames: list[pd.DataFrame] = []
    errors: list[DataSourceError] = []
    ok = 0

    for mkt in mkts:
        if stop_aggregate(source, errors, ok):
            logger.warning("%s 집계 단락 (%s %s) — 남은 시장 건너뜀", what, mkt, when)
            break
        try:
            df = fetch_one(stock, mkt)
            ok += 1
            if df is None or df.empty:
                continue
            frames.append(df)
        except DataSourceError as e:
            logger.warning("%s 조회 실패 (%s %s): %s", what, mkt, when, e)
            note_failure(e)
            errors.append(e)
        except Exception as e:  # noqa: BLE001 - pykrx 는 임의 예외를 던진다
            wrapped = SourceUnavailableError(source, f"{what} 조회 실패({mkt} {when}): {e}")
            logger.warning("%s 조회 실패 (%s %s)", what, mkt, when, exc_info=True)
            note_failure(wrapped)
            errors.append(wrapped)

    if errors and ok == 0:
        raise representative(errors)

    complete = not errors and ok == len(mkts)
    if not frames:
        empty = pd.DataFrame(columns=empty_columns) if empty_columns else pd.DataFrame()
        return empty, complete
    return pd.concat(frames), complete

# 펀더멘털(PER/PBR/DIV) 프로세스 내 LRU 캐시.
# 특정 as_of 일자의 전 종목 펀더멘털은 확정 후 변하지 않으므로 (as_of, 시장) 단위로
# 캐시하면 동일/유사 기간을 반복 백테스트할 때 pykrx 왕복을 크게 줄인다
# (예: score 방식 리밸런싱은 리밸런싱일마다 as_of 스냅샷을 조회한다). 워커 프로세스
# 수명 동안 유지되며, 실패(빈 결과)는 캐시하지 않아 다음 호출 때 재시도된다.
_FUND_CACHE: "OrderedDict[tuple, pd.DataFrame]" = OrderedDict()
_FUND_CACHE_MAX = 64  # 스냅샷 상한(초과 시 가장 오래된 항목 축출)


def _fetch_fundamentals(as_of_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """전 종목 펀더멘털(PER/PBR/DIV)을 조회한다 — 로컬 우선.

    컬럼: PER, PBR, DIV + market. 티커 인덱스.
    PER=0 은 적자(undefined)로 간주해 NaN 으로 바꾼다.

    2단 캐시다. 1차는 프로세스 내 LRU(_FUND_CACHE, DB 왕복도 아낀다), 2차는
    stock_daily_snapshots. 확정된 과거 일자는 최초 1회만 pykrx 를 탄다.

    :raises DataSourceError: 전 시장 조회가 실패했을 때(빈 프레임을 돌려주지 않는다).
    """
    key = (as_of_ymd, tuple(sorted(mkts)))
    cached = _FUND_CACHE.get(key)
    if cached is not None:
        _FUND_CACHE.move_to_end(key)
        return cached.copy()

    day = _ymd_to_date(as_of_ymd)
    cols = {"PER": "per", "PBR": "pbr", "DIV": "div", "market": "market"}
    out_cols = {"per": "PER", "pbr": "PBR", "div": "DIV", "market": "market"}
    complete = True

    def _one(stock, mkt: str) -> pd.DataFrame | None:
        df = stock.get_market_fundamental(as_of_ymd, market=mkt)
        if df is None or df.empty:
            return None
        df = df[["PER", "PBR", "DIV"]].copy()
        df["market"] = mkt
        df.loc[df["PER"] <= 0, "PER"] = np.nan
        return df

    def _remote() -> pd.DataFrame:
        nonlocal complete
        df, complete = _fetch_per_market(
            _one, mkts, what="펀더멘털", when=as_of_ymd, source="krx",
            empty_columns=["PER", "PBR", "DIV", "market"],
        )
        return df

    result = cached_frame(
        "fundamentals",
        make_cache_key(as_of_ymd, mkts),
        read_local=lambda: _store_read_daily(day, list(out_cols), out_cols, markets=mkts),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_daily(day, df, cols),
        # 부분 실패는 확정으로 굳히지 않는다 — 다음 호출에서 빠진 시장을 보완해야 한다.
        # 콜러블로 넘기는 이유: complete 는 _remote() 가 돈 뒤에야 정해진다.
        is_final=lambda: is_final_date(day) and complete,
        ledger=_store_ledger(),
    )

    if result.empty:
        return result

    _FUND_CACHE[key] = result.copy()
    _FUND_CACHE.move_to_end(key)
    if len(_FUND_CACHE) > _FUND_CACHE_MAX:
        _FUND_CACHE.popitem(last=False)
    return result


def _fetch_market_cap(as_of_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """전 종목 시가총액·상장주식수·거래대금을 조회한다 — 로컬 우선.

    컬럼: 시가총액, 거래량, 거래대금, 상장주식수. 티커 인덱스.

    :raises DataSourceError: 전 시장 조회가 실패했을 때.
    """
    day = _ymd_to_date(as_of_ymd)
    cols = {
        "시가총액": "market_cap", "거래량": "volume",
        "거래대금": "trading_value", "상장주식수": "shares",
        "market": "market",
    }
    out_cols = {
        "market_cap": "시가총액", "volume": "거래량",
        "trading_value": "거래대금", "shares": "상장주식수",
        "market": "market",
    }
    complete = True

    def _one(stock, mkt: str) -> pd.DataFrame | None:
        df = stock.get_market_cap(as_of_ymd, market=mkt)
        if df is None or df.empty:
            return None
        # 필요 컬럼만 선택 (pykrx 버전 차이 대비)
        keep = [c for c in ["시가총액", "거래량", "거래대금", "상장주식수"] if c in df.columns]
        df = df[keep].copy()
        # KOSPI/KOSDAQ 이 (trade_date, symbol) 같은 키공간에 함께 저장되므로 태깅
        # 없이는 단일시장 로컬 재조회가 전 시장 결과를 돌려준다(B1).
        df["market"] = mkt
        return df

    def _remote() -> pd.DataFrame:
        nonlocal complete
        df, complete = _fetch_per_market(
            _one, mkts, what="시가총액", when=as_of_ymd, source="krx",
        )
        return df

    return cached_frame(
        "market_cap",
        make_cache_key(as_of_ymd, mkts),
        read_local=lambda: _store_read_daily(day, list(out_cols), out_cols, markets=mkts),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_daily(day, df, cols),
        is_final=lambda: is_final_date(day) and complete,
        ledger=_store_ledger(),
    )


def _fetch_price_change(start_ymd: str, end_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """기간 등락률·거래대금을 전 종목 일괄 조회한다 — 로컬 우선.

    컬럼: 시가, 종가, 등락률, 거래량, 거래대금. 티커 인덱스.
    등락률은 pykrx 원값 그대로(%) — 호출자가 /100 으로 변환한다.

    :raises DataSourceError: 전 시장 조회가 실패했을 때.
    """
    start_d, end_d = _ymd_to_date(start_ymd), _ymd_to_date(end_ymd)
    cols = {
        "시가": "open", "종가": "close", "등락률": "change_pct",
        "거래량": "volume", "거래대금": "trading_value",
        "종목명": "name", "market": "market",
    }
    out_cols = {v: k for k, v in cols.items()}
    complete = True

    def _one(stock, mkt: str) -> pd.DataFrame | None:
        df = stock.get_market_price_change(start_ymd, end_ymd, market=mkt)
        if df is None or df.empty:
            return None
        df = df.copy()
        # KOSPI/KOSDAQ 이 (start,end,investors,symbol) 같은 키공간에 함께 저장되므로
        # 태깅 없이는 단일시장 로컬 재조회가 전 시장 결과를 돌려준다(B1).
        df["market"] = mkt
        return df

    def _remote() -> pd.DataFrame:
        nonlocal complete
        df, complete = _fetch_per_market(
            _one, mkts, what="가격변동", when=f"{start_ymd}~{end_ymd}", source="krx",
        )
        return df

    return cached_frame(
        "price_change",
        make_cache_key(start_ymd, end_ymd, mkts),
        read_local=lambda: _store_read_periods(
            start_d, end_d, "", list(out_cols), out_cols, markets=mkts
        ),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_periods(start_d, end_d, "", df, cols),
        is_final=lambda: is_final_date(end_d) and complete,
        ledger=_store_ledger(),
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
    처리), 전량(모든 시장×투자자) 조회가 실패하면 예외를 던진다(아래 :raises: 참고).
    빈 프레임이 반환되는 경우는 조회 자체는 성공했지만 순매수 실적이 없는(0행 확정)
    경우뿐이다.

    미래참조 방지: 호출자가 end_ymd 를 as_of(직전 확정 영업일) 이하로 넘겨야 한다.

    :raises DataSourceError: 전량(모든 시장×투자자) 조회가 실패했을 때.
    """
    investors_key = ",".join(sorted(investors))
    start_d, end_d = _ymd_to_date(start_ymd), _ymd_to_date(end_ymd)
    complete = True

    def _remote() -> pd.DataFrame:
        nonlocal complete
        stock = _pykrx_stock()
        accum: dict[str, float] = {}
        # 티커 → 시장. 한 티커는 한 시장에만 속하므로 mkt 루프를 도는 동안 계속
        # 덮어써도 충돌이 없다. accum 이 시장 구분 없이 합산되므로(B1) 결과
        # 프레임에 market 컬럼으로 별도로 붙여야 저장소가 KOSPI/KOSDAQ 을
        # 구분할 수 있다.
        owner: dict[str, str] = {}
        errors: list[DataSourceError] = []
        ok = 0
        for mkt in mkts:
            for investor in investors:
                if stop_aggregate("krx", errors, ok):
                    logger.warning("순매수 집계 단락 (%s %s) — 남은 조회 건너뜀", mkt, investor)
                    break
                try:
                    df = stock.get_market_net_purchases_of_equities(
                        start_ymd, end_ymd, mkt, investor
                    )
                    ok += 1
                    if df is None or df.empty or "순매수거래대금" not in df.columns:
                        continue
                    vals = pd.to_numeric(df["순매수거래대금"], errors="coerce")
                    for ticker, v in vals.items():
                        if pd.isna(v):
                            continue
                        k = str(ticker).zfill(6)
                        accum[k] = accum.get(k, 0.0) + float(v)
                        owner[k] = mkt
                except DataSourceError as e:
                    logger.warning("투자자별 순매수 조회 실패 (%s %s): %s", mkt, investor, e)
                    note_failure(e)
                    errors.append(e)
                except Exception as e:  # noqa: BLE001 - pykrx 는 임의 예외를 던진다
                    wrapped = SourceUnavailableError(
                        "krx", f"순매수 조회 실패({mkt} {investor} {start_ymd}~{end_ymd}): {e}"
                    )
                    logger.warning(
                        "투자자별 순매수 조회 실패 (%s %s %s~%s)",
                        mkt, investor, start_ymd, end_ymd, exc_info=True,
                    )
                    note_failure(wrapped)
                    errors.append(wrapped)

        if errors and ok == 0:
            raise representative(errors)
        complete = not errors and ok == len(mkts) * len(investors)

        if not accum:
            out = pd.DataFrame(columns=["net_buy_value"])
            out.index.name = "티커"
            return out
        out = pd.DataFrame.from_dict(accum, orient="index", columns=["net_buy_value"])
        out.index.name = "티커"
        out["market"] = out.index.map(owner)
        return out

    return cached_frame(
        "net_purchases",
        make_cache_key(start_ymd, end_ymd, mkts, investors),
        read_local=lambda: _store_read_periods(
            start_d, end_d, investors_key, ["net_buy_value"],
            out_columns={"net_buy_value": "net_buy_value"},
            markets=mkts,
        ),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_periods(
            start_d, end_d, investors_key, df,
            {"net_buy_value": "net_buy_value", "market": "market"},
        ),
        is_final=lambda: is_final_date(end_d) and complete,
        ledger=_store_ledger(),
    )


def _fetch_market_ohlcv_snapshot(date_ymd: str, mkt: str) -> pd.DataFrame | None:
    """단일 거래일의 전 종목 OHLCV 스냅샷을 조회한다 — 로컬 우선.

    컬럼: 시가/고가/저가/종가/거래량/거래대금/등락률. 티커 인덱스.
    패닉셀 S9(신저가 브레드스)가 날짜를 훑으며 부르므로 로컬 적재 효과가 가장 크다.

    :raises DataSourceError: 조회가 실패했을 때(이전 판은 None 을 돌려줬다).
    """
    day = _ymd_to_date(date_ymd)
    cols = {
        "시가": "open", "고가": "high", "저가": "low", "종가": "close",
        "거래량": "volume", "거래대금": "trading_value", "등락률": "change_pct",
        "market": "market",
    }
    out_cols = {v: k for k, v in cols.items()}

    def _remote() -> pd.DataFrame:
        stock = _pykrx_stock()
        try:
            with bounded_socket_timeout(20):
                df = stock.get_market_ohlcv(date_ymd, market=mkt)
        except DataSourceError as e:
            note_failure(e)
            raise
        except Exception as e:  # noqa: BLE001 - pykrx 는 임의 예외를 던진다
            wrapped = SourceUnavailableError(
                "krx", f"전종목 OHLCV 스냅샷 조회 실패({mkt} {date_ymd}): {e}"
            )
            note_failure(wrapped)
            raise wrapped from e
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        # 이 테이블은 펀더멘털·시총과 (trade_date, symbol) 키공간을 공유한다.
        # 태깅 없이는 단일시장 로컬 재조회가 전 시장 결과를 돌려준다(B1).
        df["market"] = mkt
        return df

    out = cached_frame(
        "market_ohlcv",
        make_cache_key(date_ymd, mkt),
        read_local=lambda: _store_read_daily(day, list(out_cols), out_cols, markets=[mkt]),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_daily(day, df, cols),
        is_final=is_final_date(day),
        ledger=_store_ledger(),
    )
    return out if not out.empty else None


def _fetch_index_ohlcv(start_ymd: str, end_ymd: str, ticker: str) -> pd.DataFrame | None:
    """지수 OHLCV 를 조회한다 — 로컬 우선. 데이터가 없으면 None.

    pykrx 한글 컬럼 → 영문 변환:
      시가→open, 고가→high, 저가→low, 종가→close,
      거래량→volume, 거래대금→trading_value

    :raises DataSourceError: 조회가 실패했을 때(이전 판은 None 을 돌려줬다).
    """
    start_d, end_d = _ymd_to_date(start_ymd), _ymd_to_date(end_ymd)

    def _remote() -> pd.DataFrame:
        stock = _pykrx_stock()
        try:
            with bounded_socket_timeout(20):
                df = stock.get_index_ohlcv(start_ymd, end_ymd, ticker)
        except DataSourceError as e:
            note_failure(e)
            raise
        except Exception as e:  # noqa: BLE001 - pykrx 는 임의 예외를 던진다
            wrapped = SourceUnavailableError(
                "krx", f"지수 OHLCV 조회 실패({ticker} {start_ymd}~{end_ymd}): {e}"
            )
            # 패닉·섹터 지표의 핵심 입력이라 원인 스택을 운영 로그에 남긴다.
            logger.warning("지수 OHLCV 조회 실패 (%s)", ticker, exc_info=True)
            note_failure(wrapped)
            raise wrapped from e
        if df is None or df.empty:
            return pd.DataFrame(columns=_indexes.OHLCV_COLUMNS)
        return df.rename(columns={
            "시가": "open", "고가": "high", "저가": "low", "종가": "close",
            "거래량": "volume", "거래대금": "trading_value",
        })

    out = cached_frame(
        "index_ohlcv",
        make_cache_key(ticker, start_ymd, end_ymd),
        read_local=lambda: _store_read_index_ohlcv(ticker, start_d, end_d),
        fetch_remote=_remote,
        write_local=lambda df: _store_write_index_ohlcv(ticker, df, None),
        is_final=is_final_date(end_d),
        ledger=_store_ledger(),
    )
    return out if not out.empty else None


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
