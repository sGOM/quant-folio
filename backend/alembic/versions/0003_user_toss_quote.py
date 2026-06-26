"""add users.toss_* — 통합 시세 전용 토스 자격증명(주문 브로커와 독립)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 통합 시세(국내+해외)용 토스 자격증명. 주문 브로커 자격증명(kis_*)과 별개.
    op.add_column("users", sa.Column("toss_app_key", sa.String(512), nullable=True))
    op.add_column("users", sa.Column("toss_app_secret", sa.String(512), nullable=True))
    op.add_column("users", sa.Column("toss_account_no", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "toss_account_no")
    op.drop_column("users", "toss_app_secret")
    op.drop_column("users", "toss_app_key")
