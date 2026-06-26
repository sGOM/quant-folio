"""add users.broker — 증권사 선택(kis|toss)

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-22

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 기존 사용자는 모두 기본 'kis' 로 채운다(server_default).
    op.add_column(
        "users",
        sa.Column(
            "broker",
            sa.String(16),
            nullable=False,
            server_default="kis",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "broker")
