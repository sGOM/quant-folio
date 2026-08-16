"""지수 OHLCV 확보 구간 테이블(index_ohlcv_coverage)을 추가한다.

범위 키 소스는 요청 범위가 정확히 일치할 때만 로컬 히트했다(§49 의 남은 한계).
확보 구간을 따로 기록해 "요청이 그 안에 들어오면 히트"로 바꾼다.

index_ohlcv 는 이제 페치 원장(external_fetches)을 쓰지 않는다 — 범위형에서 원장이
하던 일("미적재 vs 데이터 없음")을 커버리지 테이블이 더 정확히 하므로, 둘을
병행하면 진실이 두 곳이 된다. 기존 원장 행을 함께 지운다.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "index_ohlcv_coverage",
        sa.Column("index_code", sa.String(length=20), nullable=False),
        sa.Column("covered_from", sa.Date(), nullable=False),
        sa.Column("covered_to", sa.Date(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("index_code", "covered_from"),
    )
    op.execute("DELETE FROM external_fetches WHERE source = 'index_ohlcv'")


def downgrade() -> None:
    op.drop_table("index_ohlcv_coverage")
