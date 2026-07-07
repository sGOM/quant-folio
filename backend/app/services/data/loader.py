"""KRX 시세 데이터 적재 — FinanceDataReader 우선, 실패 시 pykrx 폴백.

조회한 일봉 OHLCV 를 price_ticks(hypertable) 에 upsert 한다.
백테스트·차트는 price_ticks 를 단일 출처로 사용한다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PriceTick

logger = logging.getLogger(__name__)


def load_ohlcv(symbol: str, start: str | date, end: str | date) -> pd.DataFrame:
    """일봉 OHLCV 를 DataFrame 으로 반환.

    컬럼: open, high, low, close, volume / index: tz-naive DatetimeIndex.
    동기(blocking) 함수이므로 호출자는 스레드풀에서 실행할 것.
    """
    df = _load_fdr(symbol, start, end)
    if df is None or df.empty:
        df = _load_pykrx(symbol, start, end)
    if df is None or df.empty:
        raise ValueError(f"{symbol} 시세를 가져오지 못했습니다 ({start}~{end}).")

    df = df.rename(columns=str.lower)
    cols = ["open", "high", "low", "close", "volume"]
    df = df[[c for c in cols if c in df.columns]].dropna()
    return df


def _load_fdr(symbol: str, start, end) -> pd.DataFrame | None:
    """FinanceDataReader 로 일봉을 조회한다(실패 시 None 반환해 폴백 유도)."""
    try:
        import FinanceDataReader as fdr

        return fdr.DataReader(symbol, start, end)
    except Exception as e:  # noqa: BLE001
        logger.warning("FinanceDataReader 실패(%s): %s", symbol, e)
        return None


def _load_pykrx(symbol: str, start, end) -> pd.DataFrame | None:
    """pykrx 로 일봉을 조회하고 한글 컬럼을 영문으로 변환한다(실패 시 None)."""
    try:
        from pykrx import stock

        s = pd.Timestamp(start).strftime("%Y%m%d")
        e = pd.Timestamp(end).strftime("%Y%m%d")
        df = stock.get_market_ohlcv(s, e, symbol)
        # pykrx 한글 컬럼 → 영문
        return df.rename(
            columns={"시가": "open", "고가": "high", "저가": "low",
                     "종가": "close", "거래량": "volume"}
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("pykrx 실패(%s): %s", symbol, e)
        return None


async def upsert_price_ticks(
    db: AsyncSession, symbol: str, df: pd.DataFrame
) -> int:
    """DataFrame 을 price_ticks 에 upsert. 적재 행 수 반환."""
    rows = []
    for ts, r in df.iterrows():
        t = pd.Timestamp(ts).to_pydatetime()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        rows.append(
            {
                "time": t,
                "symbol": symbol,
                "open": Decimal(str(r["open"])),
                "high": Decimal(str(r["high"])),
                "low": Decimal(str(r["low"])),
                "close": Decimal(str(r["close"])),
                "volume": int(r.get("volume", 0) or 0),
            }
        )
    if not rows:
        return 0

    stmt = pg_insert(PriceTick).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["time", "symbol"],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
        },
    )
    await db.execute(stmt)
    await db.commit()
    logger.info("price_ticks upsert: %s rows=%d", symbol, len(rows))
    return len(rows)


async def get_close_series(
    db: AsyncSession, symbol: str, start: datetime, end: datetime
) -> pd.Series:
    """price_ticks 에서 종가 시계열을 Series 로 반환 (index=time)."""
    result = await db.execute(
        select(PriceTick.time, PriceTick.close)
        .where(PriceTick.symbol == symbol)
        .where(PriceTick.time >= start)
        .where(PriceTick.time <= end)
        .order_by(PriceTick.time)
    )
    rows = result.all()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.Series([float(r[1]) for r in rows], index=idx, name=symbol)


async def get_ohlcv_frame(
    db: AsyncSession, symbol: str, start: datetime, end: datetime
) -> pd.DataFrame:
    """price_ticks 에서 OHLCV 전체를 DataFrame 으로 반환.

    컬럼: open, high, low, close, volume / index: time(DatetimeIndex).
    OHLC 기반 전략(ATR 트레일링·변동성 돌파·켈트너·스토캐스틱)의 신호 입력에 사용.
    데이터가 없으면 해당 컬럼만 가진 빈 DataFrame 을 반환한다.
    """
    cols = ["open", "high", "low", "close", "volume"]
    result = await db.execute(
        select(
            PriceTick.time,
            PriceTick.open,
            PriceTick.high,
            PriceTick.low,
            PriceTick.close,
            PriceTick.volume,
        )
        .where(PriceTick.symbol == symbol)
        .where(PriceTick.time >= start)
        .where(PriceTick.time <= end)
        .order_by(PriceTick.time)
    )
    rows = result.all()
    if not rows:
        return pd.DataFrame(columns=cols)
    idx = pd.DatetimeIndex([r[0] for r in rows])
    data = {
        "open": [float(r[1]) for r in rows],
        "high": [float(r[2]) for r in rows],
        "low": [float(r[3]) for r in rows],
        "close": [float(r[4]) for r in rows],
        "volume": [float(r[5]) if r[5] is not None else 0.0 for r in rows],
    }
    return pd.DataFrame(data, index=idx)
