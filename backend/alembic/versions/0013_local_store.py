"""확정 과거 데이터 로컬 영구 저장소 6테이블 추가.

설계: docs/superpowers/specs/2026-08-06-local-persistent-store-design.md

stock_daily_snapshots 는 종목수×거래일 규모(2,800종목 × 250일 × 10년 ≈ 7백만 행)라
price_ticks 와 같이 trade_date 기준 hypertable 로 파티셔닝한다.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- stock_daily_snapshots (거래일 × 종목 격자) ---
    op.create_table(
        "stock_daily_snapshots",
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(20), nullable=True),
        sa.Column("per", sa.Numeric(18, 4), nullable=True),
        sa.Column("pbr", sa.Numeric(18, 4), nullable=True),
        sa.Column("div", sa.Numeric(18, 4), nullable=True),
        sa.Column("market_cap", sa.BigInteger, nullable=True),
        sa.Column("shares", sa.BigInteger, nullable=True),
        sa.Column("open", sa.Numeric(18, 4), nullable=True),
        sa.Column("high", sa.Numeric(18, 4), nullable=True),
        sa.Column("low", sa.Numeric(18, 4), nullable=True),
        sa.Column("close", sa.Numeric(18, 4), nullable=True),
        sa.Column("volume", sa.BigInteger, nullable=True),
        sa.Column("trading_value", sa.BigInteger, nullable=True),
        sa.Column("change_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("trade_date", "symbol"),
    )
    op.create_index(
        "ix_stock_daily_snapshots_symbol_date",
        "stock_daily_snapshots",
        ["symbol", "trade_date"],
    )
    op.execute(
        "SELECT create_hypertable('stock_daily_snapshots', 'trade_date', "
        "if_not_exists => TRUE, migrate_data => TRUE);"
    )

    # --- stock_period_stats (기간키) ---
    op.create_table(
        "stock_period_stats",
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("investors", sa.String(100), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(20), nullable=True),
        sa.Column("change_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column("open", sa.Numeric(18, 4), nullable=True),
        sa.Column("close", sa.Numeric(18, 4), nullable=True),
        sa.Column("volume", sa.BigInteger, nullable=True),
        sa.Column("trading_value", sa.BigInteger, nullable=True),
        sa.Column("net_buy_value", sa.Numeric(24, 2), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("start_date", "end_date", "investors", "symbol"),
    )
    op.create_index("ix_stock_period_stats_symbol", "stock_period_stats", ["symbol"])
    op.create_index(
        "ix_stock_period_stats_range", "stock_period_stats", ["start_date", "end_date"]
    )

    # --- index_ohlcv ---
    op.create_table(
        "index_ohlcv",
        sa.Column("index_code", sa.String(20), nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("index_name", sa.String(100), nullable=True),
        sa.Column("open", sa.Numeric(18, 4), nullable=True),
        sa.Column("high", sa.Numeric(18, 4), nullable=True),
        sa.Column("low", sa.Numeric(18, 4), nullable=True),
        sa.Column("close", sa.Numeric(18, 4), nullable=True),
        sa.Column("volume", sa.BigInteger, nullable=True),
        sa.Column("trading_value", sa.BigInteger, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("index_code", "trade_date"),
    )

    # --- index_constituents (PIT 지수구성) ---
    op.create_table(
        "index_constituents",
        sa.Column("index_code", sa.String(40), nullable=False),
        sa.Column("base_date", sa.Date, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("index_code", "base_date", "symbol"),
    )
    op.create_index(
        "ix_index_constituents_code_date", "index_constituents", ["index_code", "base_date"]
    )

    # --- dart_financials ---
    op.create_table(
        "dart_financials",
        sa.Column("corp_code", sa.String(20), nullable=False),
        sa.Column("bsns_year", sa.Integer, nullable=False),
        sa.Column("reprt_code", sa.String(10), nullable=False),
        sa.Column("fs_div", sa.String(10), nullable=False),
        sa.Column("accounts", postgresql.JSONB, nullable=False),
        sa.Column("rcept_no", sa.String(30), nullable=True),
        sa.Column("rcept_dt", sa.Date, nullable=True),
        sa.Column("confirmed_at", sa.Date, nullable=True),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("corp_code", "bsns_year", "reprt_code", "fs_div"),
    )

    # --- external_fetches (페치 원장) ---
    op.create_table(
        "external_fetches",
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("cache_key", sa.String(200), nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("final", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("source", "cache_key"),
    )


def downgrade() -> None:
    op.drop_table("external_fetches")
    op.drop_table("dart_financials")
    op.drop_index("ix_index_constituents_code_date", table_name="index_constituents")
    op.drop_table("index_constituents")
    op.drop_table("index_ohlcv")
    op.drop_index("ix_stock_period_stats_range", table_name="stock_period_stats")
    op.drop_index("ix_stock_period_stats_symbol", table_name="stock_period_stats")
    op.drop_table("stock_period_stats")
    op.drop_index(
        "ix_stock_daily_snapshots_symbol_date", table_name="stock_daily_snapshots"
    )
    op.drop_table("stock_daily_snapshots")
