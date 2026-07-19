"""PRD §4 데이터 모델: users, strategies, backtests, orders, executions,
price_ticks(hypertable), positions, risk_limits.

금액·수량은 부동소수점 오차를 피하기 위해 NUMERIC 을 사용한다.
"""
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
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
    # 공유 전략 목록에 표시할 닉네임(없으면 화면에서 '익명'으로 표기)
    display_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
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
    # 전략 설명(자유 텍스트). 공유 시 함께 노출.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[StrategyStatus] = mapped_column(
        String(20), default=StrategyStatus.DRAFT, nullable=False
    )
    # 대표 백테스트(공유 시 성과 표시용). 원본 삭제 시 SET NULL.
    featured_backtest_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtests.id", ondelete="SET NULL"), nullable=True
    )
    # 공유: 켜면 다른 모든 사용자가 공유 목록에서 열람·복사·좋아요 가능
    is_shared: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 소유자 내 표시: 즐겨찾기(상단 고정)·정렬 순서(작을수록 위)
    is_favorite: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # 복사 출처(원본 삭제 시 NULL). 자기참조 FK.
    copied_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="strategies")
    backtests: Mapped[list["Backtest"]] = relationship(
        back_populates="strategy",
        # featured_backtest_id 로 strategies↔backtests 사이 FK 가 둘이 되어 모호하므로
        # 이 관계는 backtests.strategy_id 를 명시한다.
        foreign_keys="Backtest.strategy_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    likes: Mapped[list["StrategyLike"]] = relationship(
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

    strategy: Mapped["Strategy"] = relationship(
        back_populates="backtests", foreign_keys="Backtest.strategy_id"
    )


# ─────────────────────────── strategy_likes ───────────────────────────
class StrategyLike(Base):
    """공유 전략 좋아요. (strategy_id, user_id) 유니크로 인당 1회를 강제한다."""
    __tablename__ = "strategy_likes"
    __table_args__ = (
        UniqueConstraint("strategy_id", "user_id", name="uq_strategy_likes_user"),
        Index("ix_strategy_likes_strategy", "strategy_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    strategy: Mapped["Strategy"] = relationship(back_populates="likes")


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
        ForeignKey("strategies.id", ondelete="SET NULL"), index=True, nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    side: Mapped[OrderSide] = mapped_column(String(8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    order_type: Mapped[str] = mapped_column(String(20), default="limit", nullable=False)
    kis_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # KIS 주문조직번호(KRX_FWDG_ORD_ORGNO). 체결통보 매칭 정확도 보강용(선택적).
    kis_order_org_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[OrderStatus] = mapped_column(
        String(16), default=OrderStatus.PENDING, nullable=False
    )
    # 감사 로그용 주문 사유 — 어떤 신호·공식·리스크·리밸런싱 기준으로 이 주문이
    # 나갔는지 사람이 읽을 수 있는 한국어 설명(예: "골든크로스: SMA5 51,200 > SMA20 50,800",
    # "리밸런싱 편입 3/20위, 목표비중 6.7%"). 과거 주문·수동 주문은 NULL.
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # 계정 삭제(users→orders CASCADE) 시 체결이력도 DB 레벨에서 함께 삭제되도록 한다
    # (executions.order_id ondelete=CASCADE). passive_deletes=True 로 ORM 이 별도 UPDATE/
    # SELECT 없이 DB 캐스케이드에 위임한다.
    executions: Mapped[list["Execution"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )


# ─────────────────────────── executions ───────────────────────────
class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 비정규화 컬럼 — 전략별 체결 이력을 order 조인 없이 직접 조회하기 위한 것.
    # 쓰기 시 order.strategy_id 에서 그대로 채운다(전략 삭제 시 SET NULL).
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), index=True, nullable=True
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
        # 다중 전략 포지션 분리: 같은 종목을 두 전략이 동시에 보유해도 포지션이 섞이지
        # 않도록 (user_id, strategy_id, symbol) 단위로 유일성을 강제한다.
        # 주의: Postgres 는 nullable 컬럼을 포함한 UNIQUE 제약에서 NULL 을 서로 다른
        # 값으로 취급한다(NULL != NULL). 즉 strategy_id=NULL 인 비귀속(레거시·수동)
        # 포지션은 동일 (user_id, symbol) 에 대해 여러 행이 존재할 수 있으며, 이는
        # 의도된 동작이다 — 자동매매 엔진이 소유하지 않는 포지션은 사람이 직접 관리한다.
        UniqueConstraint(
            "user_id", "strategy_id", "symbol", name="uq_positions_user_strategy_symbol"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 이 포지션을 소유한 전략(자동매매 러너). 수동·레거시 포지션은 NULL(비귀속).
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), index=True, nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"), nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────── sector_map_snapshots ───────────────────────────
class SectorMapSnapshot(Base):
    """업종분류 PIT 스냅샷(docs/improvements.md C-2 해소).

    KRX MDC(app.services.data.krx_index.sector_map)는 '현재' 업종분류만 제공하고 과거
    임의 시점을 조회하는 API 가 없다. 이를 우회하기 위해 지금부터 주기(분기 1회,
    worker.snapshot_sector_map 태스크)로 현재 시점 분류를 이 테이블에 적재해 시점별
    이력을 쌓는다. 스냅샷 도입 이후의 백테스트 구간부터는 point-in-time 매핑을 쓸 수
    있다(krx_index.sector_map(as_of=...) 참고). 스냅샷 도입 이전 과거 구간은 데이터
    소스 부재로 소급 적용이 여전히 불가능해 현재 분류로 폴백한다(문서화된 잔존 한계).
    """
    __tablename__ = "sector_map_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol", "snapshot_date", name="uq_sector_map_snapshot_symbol_date"),
        Index("ix_sector_map_snapshots_symbol_date", "symbol", "snapshot_date"),
        Index("ix_sector_map_snapshots_date", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    sector: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─────────────────────────── news_articles ───────────────────────────
class NewsArticle(Base):
    """경제 뉴스 기사(docs/ROADMAP.md M3 — 수집·요약·종목 연계).

    언론사 RSS(worker.ingest_news, 주기 배치)에서 수집한 기사 메타데이터를 적재한다.
    본문 전재가 아닌 **요약+원문 링크** 방식으로만 표시한다(출처 약관 준수). url 을
    유니크 키로 삼아 재수집 시 중복 삽입을 막는다. 요약은 현재 RSS description 기반
    추출 요약이며, 생성 수단은 교체 가능하도록 서비스(app/services/news.py)에 격리한다.
    """
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("url", name="uq_news_articles_url"),
        Index("ix_news_articles_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NewsArticleSymbol(Base):
    """기사 ↔ 종목 매핑(제목·요약의 종목명 언급 기반, app/services/news.py::match_symbols).

    종목 상세/스크리너에서 "이 종목의 관련 기사"를 조회하는 역인덱스. (symbol,
    article_id) 복합 인덱스가 그 조회 경로를 담당한다.
    """
    __tablename__ = "news_article_symbols"
    __table_args__ = (
        UniqueConstraint("article_id", "symbol", name="uq_news_article_symbols"),
        Index("ix_news_article_symbols_symbol_article", "symbol", "article_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("news_articles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)


# ─────────────────────────── risk_limits ───────────────────────────
class RiskLimit(Base):
    __tablename__ = "risk_limits"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), index=True, nullable=True
    )
    max_position_size: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    daily_loss_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)


# ─────────────────────────── alerts ───────────────────────────
class Alert(Base):
    """engine/alerts.py::publish_alert 의 영속화 사본(docs/improvements.md §17 해소).

    WS·텔레그램은 순간 전송이라 미접속 중이거나(WS) warning 심각도(텔레그램)면 알림이
    유실된다. 이 테이블은 발행되는 모든 알림을 그대로 적재해 `GET /api/alerts` 로
    나중에 조회·확인 처리할 수 있게 한다. user_id NULL 은 특정 사용자가 아닌 전역/운영
    알림(배치 작업 실패 등)을 의미하며, `GET /api/alerts` 는 요청자 알림 + 전역 알림을
    함께 반환한다.
    """
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_user_read_created", "user_id", "is_read", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    strategy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
