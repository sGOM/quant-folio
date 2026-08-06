"""확정 과거 데이터의 로컬 영구 저장소 모델.

설계: docs/superpowers/specs/2026-08-06-local-persistent-store-design.md

조회키가 같은 데이터끼리 접었다. 펀더멘털·시가총액·전종목 OHLCV 는 셋 다
(거래일 × 종목) 격자라 stock_daily_snapshots 한 장에 들어간다. 기간 등락률·순매수는
기간키(start~end)라 별도다 — pykrx 기간 등락률은 수정주가 기준이라 price_ticks
종가로 재계산하면 액면분할·유상증자 구간에서 값이 갈리므로 원본을 그대로 보관한다.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StockDailySnapshot(Base):
    """거래일 × 종목 격자 — 펀더멘털 + 시가총액 + 전종목 OHLCV.

    세 소스가 서로 다른 시점에 채우므로 전 컬럼 nullable 이고, upsert 는 들어온 값이
    NULL 이면 기존값을 보존한다(시총만 적재된 행을 펀더멘털 적재가 지우면 안 된다).
    """

    __tablename__ = "stock_daily_snapshots"
    __table_args__ = (Index("ix_stock_daily_snapshots_symbol_date", "symbol", "trade_date"),)

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)
    market: Mapped[str | None] = mapped_column(String(20), nullable=True)

    per: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    div: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)

    market_cap: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trading_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockPeriodStat(Base):
    """기간키(start~end) 종목 통계 — 기간 등락률과 투자자 순매수.

    investors 는 투자자군 조합을 정렬 후 ',' 로 이은 문자열(기본 "기관합계,외국인").
    조합이 다르면 다른 행이다. 등락률만 조회한 행은 investors=''.
    """

    __tablename__ = "stock_period_stats"
    __table_args__ = (
        Index("ix_stock_period_stats_symbol", "symbol"),
        Index("ix_stock_period_stats_range", "start_date", "end_date"),
    )

    start_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    investors: Mapped[str] = mapped_column(String(100), primary_key=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)

    market: Mapped[str | None] = mapped_column(String(20), nullable=True)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trading_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    net_buy_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IndexOhlcv(Base):
    """지수 일봉 — 업종지수·KOSPI/KOSDAQ 대표지수."""

    __tablename__ = "index_ohlcv"

    index_code: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)

    index_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    trading_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IndexConstituent(Base):
    """PIT 지수구성 — base_date 시점의 지수 편입 종목."""

    __tablename__ = "index_constituents"
    __table_args__ = (Index("ix_index_constituents_code_date", "index_code", "base_date"),)

    index_code: Mapped[str] = mapped_column(String(40), primary_key=True, nullable=False)
    base_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DartFinancial(Base):
    """OpenDART 재무제표 원계정.

    파생지표(derive_metrics·piotroski_f_score)가 아니라 원계정 리스트를 그대로 담는다.
    파생 코드가 바뀌면 저장된 파생값은 낡지만 원계정은 안 낡는다.

    confirmed_at 이후로는 불변으로 취급한다(정정공시 반영 유예 = 접수일 + 90일).
    """

    __tablename__ = "dart_financials"

    corp_code: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)
    bsns_year: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    reprt_code: Mapped[str] = mapped_column(String(10), primary_key=True, nullable=False)
    fs_div: Mapped[str] = mapped_column(String(10), primary_key=True, nullable=False)

    accounts: Mapped[list] = mapped_column(JSONB, nullable=False)
    rcept_no: Mapped[str | None] = mapped_column(String(30), nullable=True)
    rcept_dt: Mapped[date | None] = mapped_column(Date, nullable=True)
    confirmed_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExternalFetch(Base):
    """페치 원장 — "이 조회를 실제로 해봤는가"의 유일한 기록.

    정규화 테이블만으로는 "휴장일이라 0행"과 "아직 적재 안 됨"이 똑같이 0행이라
    구분이 불가능하다. 구분하지 못하면 §48 이 닫으려던 조용한 실패 모드를 이 저장소가
    그대로 재현한다. 조회 사실 자체를 여기 남겨 둘을 가른다.

    final=False 는 "저장은 했지만 아직 확정 아님"(당일 시세·미확정 DART)이라 다음
    호출에서 재조회된다.
    """

    __tablename__ = "external_fetches"

    source: Mapped[str] = mapped_column(String(40), primary_key=True, nullable=False)
    cache_key: Mapped[str] = mapped_column(String(200), primary_key=True, nullable=False)

    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    final: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
