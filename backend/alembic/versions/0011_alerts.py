"""알림 영속화 테이블 추가 — alerts (docs/improvements.md §17 해소).

engine/alerts.py::publish_alert 는 WS(접속 중인 사용자)·텔레그램(critical 만)으로만
알림을 전송해, user_id=None 인 배치 warning 이나 미접속 중 발행된 warning 은 유실됐다.
이 테이블은 발행되는 모든 알림을 그대로 적재해 GET /api/alerts 로 나중에 조회·확인
처리할 수 있게 한다. user_id NULL 은 전역/운영 알림을 의미한다.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("strategy_id", sa.Integer, nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("code", sa.String(50), nullable=True),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("is_read", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_alerts_user_id", "alerts", ["user_id"])
    # (user_id, is_read, created_at) — "내 미확인 알림 최신순" 조회 성능.
    op.create_index(
        "ix_alerts_user_read_created", "alerts", ["user_id", "is_read", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_alerts_user_read_created", table_name="alerts")
    op.drop_index("ix_alerts_user_id", table_name="alerts")
    op.drop_table("alerts")
