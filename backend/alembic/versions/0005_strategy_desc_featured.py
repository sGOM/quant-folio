"""strategy description + featured backtest — 설명·대표 백테스트 첨부

전략에 자유 텍스트 설명을 달고, 백테스트 이력 중 하나를 '대표 결과'로 지정해
공유 시 함께 표시할 수 있게 한다.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("strategies", sa.Column("description", sa.Text(), nullable=True))
    # 대표 백테스트(공유 시 성과 표시용). 원본 백테스트 삭제 시 자동 해제.
    op.add_column(
        "strategies",
        sa.Column(
            "featured_backtest_id",
            sa.Integer(),
            sa.ForeignKey("backtests.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("strategies", "featured_backtest_id")
    op.drop_column("strategies", "description")
