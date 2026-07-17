"""업종분류 PIT 스냅샷 테이블 추가 — sector_map_snapshots (docs/improvements.md C-2 해소).

KRX MDC(app.services.data.krx_index.sector_map)는 '현재' 업종분류만 제공하고 과거 임의
시점을 조회하는 API 가 없다. 이를 우회하기 위해 지금부터 주기(분기 1회, worker
"snapshot-sector-map-quarterly" beat)로 현재 시점 분류를 이 테이블에 적재해 시점별 이력을
쌓는다. 스냅샷 도입 이후의 백테스트 구간부터는 point-in-time 매핑을 쓸 수 있다. 스냅샷
도입 이전 과거 구간은 데이터 소스 부재로 소급 적용이 여전히 불가능하다(문서화된 한계).

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sector_map_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("sector", sa.String(100), nullable=False),
        sa.Column("snapshot_date", sa.Date, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "symbol", "snapshot_date", name="uq_sector_map_snapshot_symbol_date"
        ),
    )
    # (symbol, snapshot_date) — 종목별 시점 조회 성능.
    op.create_index(
        "ix_sector_map_snapshots_symbol_date",
        "sector_map_snapshots",
        ["symbol", "snapshot_date"],
    )
    # snapshot_date 단독 — "as_of 이전 가장 최근 배치일" 탐색(MAX(snapshot_date) WHERE ...) 성능.
    op.create_index(
        "ix_sector_map_snapshots_date", "sector_map_snapshots", ["snapshot_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_sector_map_snapshots_date", table_name="sector_map_snapshots")
    op.drop_index("ix_sector_map_snapshots_symbol_date", table_name="sector_map_snapshots")
    op.drop_table("sector_map_snapshots")
