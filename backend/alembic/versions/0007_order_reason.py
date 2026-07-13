"""orders.reason 추가 — 감사 로그 주문 사유

매수·매도 주문이 어떤 신호·공식·리스크·리밸런싱 기준으로 나갔는지 사람이 읽을 수 있는
한국어 설명을 저장한다(감사 로그 상세화). 기존 주문·수동 주문은 NULL.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("reason", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "reason")
