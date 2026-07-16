"""KRX 시세 데이터 적재 — FinanceDataReader 우선, 실패 시 pykrx 폴백.

조회한 일봉 OHLCV 를 price_ticks(hypertable) 에 upsert 한다.
백테스트·차트는 price_ticks 를 단일 출처로 사용한다.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PriceTick

logger = logging.getLogger(__name__)


# socket.setdefaulttimeout 은 프로세스 전역값이라, 여러 러너 스레드가 bounded_socket_timeout
# 을 동시·중첩 실행하면 각자의 prev 캡처/원복이 서로를 덮어써 전역값이 엉킨다(최악: None 이
# 잔류해 무한 대기 — 방어하려던 무한 대기를 동시성으로 되살림). 락으로 원복 부기를 직렬화하고,
# '현재 활성 요청들의 최소 타임아웃'을 전역에 유지하되, 원래 baseline 은 활성 요청이 하나도
# 없어질 때 정확히 한 번만 복원한다(중첩·동시성 안전).
_timeout_lock = threading.RLock()
_active_timeouts: list[float] = []
_timeout_baseline: float | None = None


@contextlib.contextmanager
def bounded_socket_timeout(seconds: float):
    """블로킹 네트워크 호출에 임시 소켓 타임아웃을 건다(프로세스 전역, 중첩·스레드 안전).

    FinanceDataReader/pykrx 는 내부 요청에 timeout 을 지정하지 않는 경로가 있어, 응답이
    없으면 스레드가 무한 대기한다. 이 호출은 asyncio.to_thread 로 별도 스레드에서
    실행되므로, 이벤트 루프 쪽의 asyncio.wait_for 타임아웃으로는 이미 실행 중인 스레드를
    끊을 수 없다(실행 중인 스레드로 넘어간 뒤엔 Future.cancel() 이 아무 효과가 없음) —
    실질적인 방어는 이 소켓 레벨 타임아웃뿐이다. socket.setdefaulttimeout 은 새로 생성되는
    소켓에만 적용되고, 활성 요청이 모두 끝나면 baseline 으로 원복한다.
    """
    global _timeout_baseline
    with _timeout_lock:
        if not _active_timeouts:
            _timeout_baseline = socket.getdefaulttimeout()
        _active_timeouts.append(seconds)
        socket.setdefaulttimeout(min(_active_timeouts))
    try:
        yield
    finally:
        with _timeout_lock:
            # 자기 요청 하나만 제거(같은 값이 여러 개여도 하나만).
            try:
                _active_timeouts.remove(seconds)
            except ValueError:
                pass
            if _active_timeouts:
                socket.setdefaulttimeout(min(_active_timeouts))
            else:
                socket.setdefaulttimeout(_timeout_baseline)


def load_ohlcv(symbol: str, start: str | date, end: str | date) -> pd.DataFrame:
    """일봉 OHLCV 를 DataFrame 으로 반환.

    컬럼: open, high, low, close, volume / index: tz-naive DatetimeIndex.
    동기(blocking) 함수이므로 호출자는 스레드풀에서 실행할 것.
    """
    with bounded_socket_timeout(20):
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


async def get_volume_series(
    db: AsyncSession, symbol: str, start: datetime, end: datetime
) -> pd.Series:
    """price_ticks 에서 거래량(주) 시계열을 Series 로 반환 (index=time).

    ADV(일평균거래대금) 캡(P2-2 A-2)용 — 종가 패널과 곱해 거래대금을 산출한다.
    """
    result = await db.execute(
        select(PriceTick.time, PriceTick.volume)
        .where(PriceTick.symbol == symbol)
        .where(PriceTick.time >= start)
        .where(PriceTick.time <= end)
        .order_by(PriceTick.time)
    )
    rows = result.all()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.Series(
        [float(r[1]) if r[1] is not None else 0.0 for r in rows], index=idx, name=symbol
    )


# 주말·공휴일 등으로 인접 거래일이 며칠 비어도 '진짜 결측'으로 오판하지 않기 위한 여유.
# 최장 연휴(설·추석 등)를 감안해 5일로 잡는다.
_COVERAGE_GRACE_DAYS = 5


async def ensure_ohlcv_coverage(
    db: AsyncSession, symbol: str, start: datetime, end: datetime
) -> None:
    """price_ticks 가 [start,end] 구간을 커버하도록 부족분만 외부 소스로 보충한다(C-1).

    데이터가 아예 없으면 전체 구간을, 있으면 머리(더 과거로 확장된 백테스트 구간)·꼬리
    (야간 배치 이후 새로 생긴 최근 거래일)만 골라 채운다 — 매번 전체 구간을 재조회하던
    기존 방식보다 훨씬 가볍고, 반복 백테스트가 이미 적재된 로컬 데이터를 그대로 재사용하게
    한다. 외부 조회 실패는 로그만 남기고 조용히 넘어간다(호출자는 이후 조회에서 있는 만큼만
    쓰게 되며, 완전 실패 시 기존 '해당 기간 시세가 없습니다' 422 로 자연스럽게 이어진다).
    """
    result = await db.execute(
        select(func.min(PriceTick.time), func.max(PriceTick.time))
        .where(PriceTick.symbol == symbol)
        .where(PriceTick.time >= start)
        .where(PriceTick.time <= end)
    )
    mn, mx = result.one()

    grace = timedelta(days=_COVERAGE_GRACE_DAYS)
    gaps: list[tuple[date, date]] = []
    if mn is None:
        gaps.append((start.date(), end.date()))
    else:
        if mn.date() > start.date() + grace:
            gaps.append((start.date(), mn.date()))
        if mx.date() < end.date() - grace:
            gaps.append((mx.date(), end.date()))

    for gap_start, gap_end in gaps:
        try:
            df = await asyncio.to_thread(load_ohlcv, symbol, gap_start, gap_end)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 구간 보충 실패(%s~%s): %s", symbol, gap_start, gap_end, e)
            continue
        await upsert_price_ticks(db, symbol, df)
