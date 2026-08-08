"""stock_period_stats 에 종목명(name) 컬럼 추가.

I1: `_fetch_price_change` 의 로컬 히트 결과가 '종목명' 컬럼을 잃어(테이블에 컬럼
자체가 없었다), 로컬 캐시 2회차부터 `_build_krx_name_map` 이 빈 맵을 반환하고
내장 카탈로그에 없는 종목의 이름이 조용히 비어버렸다.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stock_period_stats", sa.Column("name", sa.String(100), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("stock_period_stats", "name")
