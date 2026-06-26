"""initial schema — PRD §4 8 tables + TimescaleDB hypertable

Revision ID: 0001
Revises:
Create Date: 2026-06-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # TimescaleDB 확장 (이미지에 미포함이면 무시되지 않으므로 IF NOT EXISTS)
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("kis_app_key", sa.String(512), nullable=True),
        sa.Column("kis_app_secret", sa.String(512), nullable=True),
        sa.Column("kis_account_no", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # --- strategies ---
    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_strategies_user_id", "strategies", ["user_id"])

    # --- backtests ---
    op.create_table(
        "backtests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("strategy_id", sa.Integer, sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_return", sa.Numeric(12, 6), nullable=True),
        sa.Column("mdd", sa.Numeric(12, 6), nullable=True),
        sa.Column("sharpe", sa.Numeric(12, 6), nullable=True),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_backtests_strategy_id", "backtests", ["strategy_id"])

    # --- orders ---
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_id", sa.Integer, sa.ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("qty", sa.Numeric(18, 4), nullable=False),
        sa.Column("price", sa.Numeric(18, 4), nullable=True),
        sa.Column("order_type", sa.String(20), nullable=False, server_default="limit"),
        sa.Column("kis_order_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
    )
    op.create_index("ix_orders_symbol", "orders", ["symbol"])
    op.create_index("ix_orders_user_created", "orders", ["user_id", "created_at"])

    # --- executions ---
    op.create_table(
        "executions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("filled_qty", sa.Numeric(18, 4), nullable=False),
        sa.Column("filled_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("fee", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("executed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_executions_order_id", "executions", ["order_id"])

    # --- price_ticks (TimescaleDB hypertable) ---
    op.create_table(
        "price_ticks",
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("open", sa.Numeric(18, 4), nullable=False),
        sa.Column("high", sa.Numeric(18, 4), nullable=False),
        sa.Column("low", sa.Numeric(18, 4), nullable=False),
        sa.Column("close", sa.Numeric(18, 4), nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("time", "symbol"),
    )
    op.create_index("ix_price_ticks_symbol_time", "price_ticks", ["symbol", "time"])
    # hypertable 변환 — 대량 시계열을 time 기준 청크로 파티셔닝
    op.execute(
        "SELECT create_hypertable('price_ticks', 'time', "
        "if_not_exists => TRUE, migrate_data => TRUE);"
    )

    # --- positions ---
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("qty", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("avg_price", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "symbol", name="uq_positions_user_symbol"),
    )
    op.create_index("ix_positions_user_id", "positions", ["user_id"])

    # --- risk_limits ---
    op.create_table(
        "risk_limits",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_id", sa.Integer, sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=True),
        sa.Column("max_position_size", sa.Numeric(18, 4), nullable=True),
        sa.Column("daily_loss_limit", sa.Numeric(18, 4), nullable=True),
        sa.Column("stop_loss_pct", sa.Numeric(6, 4), nullable=True),
    )
    op.create_index("ix_risk_limits_user_id", "risk_limits", ["user_id"])


def downgrade() -> None:
    op.drop_table("risk_limits")
    op.drop_table("positions")
    op.drop_table("price_ticks")
    op.drop_table("executions")
    op.drop_table("orders")
    op.drop_table("backtests")
    op.drop_table("strategies")
    op.drop_table("users")
