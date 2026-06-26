"""PRD §4 데이터 모델: users, strategies, backtests, orders, executions,
price_ticks(hypertable), positions, risk_limits.

금액·수량은 부동소수점 오차를 피하기 위해 NUMERIC 을 사용한다.
"""
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class StrategyStatus(StrEnum):
    DRAFT = "draft"
    BACKTESTED = "backtested"
    LIVE = "live"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PENDING = "pending"          # 엔진이 주문 생성, KIS 전송 전
    SUBMITTED = "submitted"      # KIS 접수
    PARTIAL = "partial"          # 일부 체결
    FILLED = "filled"            # 전량 체결
    CANCELLED = "cancelled"
    REJECTED = "rejected"


# ─────────────────────────── users ───────────────────────────
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 사용할 증권사. 'kis'(한국투자) | 'toss'(토스증권). 자격증명 컬럼은 공통 재사용.
    broker: Mapped[str] = mapped_column(
        String(16), default="kis", server_default="kis", nullable=False
    )
    # 주문 브로커(User.broker) 자격증명. 암호화된 값만 저장 (평문 컬럼 금지)
    #  - kis : app_key / app_secret / 계좌번호
    #  - toss: client_id / client_secret / accountSeq
    kis_app_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    kis_app_secret: Mapped[str | None] = mapped_column(String(512), nullable=True)
    kis_account_no: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 통합 시세 전용 토스 자격증명(주문 브로커와 독립). 등록 시 국내·해외 시세를
    # 토스로 통합 조회한다. client_id / client_secret / accountSeq.
    toss_app_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    toss_app_secret: Mapped[str | None] = mapped_column(String(512), nullable=True)
    toss_account_no: Mapped[str | None] = mapped_column(String(32), nullable=True)

    strategies: Mapped[list["Strategy"]] = relationship(back_populates="user")


# ─────────────────────────── strategies ───────────────────────────
class Strategy(Base, TimestampMixin):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[StrategyStatus] = mapped_column(
        String(20), default=StrategyStatus.DRAFT, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="strategies")
    backtests: Mapped[list["Backtest"]] = relationship(
        back_populates="strategy",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ─────────────────────────── backtests ───────────────────────────
class Backtest(Base):
    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), index=True, nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_return: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    mdd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    sharpe: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    strategy: Mapped["Strategy"] = relationship(back_populates="backtests")


# ─────────────────────────── orders ───────────────────────────
class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        # 멱등성 키로 중복 주문을 DB 레벨에서 차단
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
        Index("ix_orders_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    side: Mapped[OrderSide] = mapped_column(String(8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    order_type: Mapped[str] = mapped_column(String(20), default="limit", nullable=False)
    kis_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[OrderStatus] = mapped_column(
        String(16), default=OrderStatus.PENDING, nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    executions: Mapped[list["Execution"]] = relationship(back_populates="order")


# ─────────────────────────── executions ───────────────────────────
class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    filled_qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    filled_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped["Order"] = relationship(back_populates="executions")


# ─────────────────────────── price_ticks (TimescaleDB hypertable) ───────────────────────────
class PriceTick(Base):
    __tablename__ = "price_ticks"
    __table_args__ = (
        # hypertable 파티션 키(time)를 포함한 복합 PK
        Index("ix_price_ticks_symbol_time", "symbol", "time"),
    )

    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


# ─────────────────────────── positions ───────────────────────────
class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_positions_user_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"), nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────── risk_limits ───────────────────────────
class RiskLimit(Base):
    __tablename__ = "risk_limits"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), nullable=True
    )
    max_position_size: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    daily_loss_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
