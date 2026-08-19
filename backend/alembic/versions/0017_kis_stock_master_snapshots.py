"""KIS 종목마스터 스냅샷 테이블 추가 — kis_stock_master_snapshots.

거래정지·관리종목·정리매매·시장경고·불성실공시·우회상장·단기과열·SPAC·액면가·
업종 대/중/소분류 등, KRX MDC/FDR/DART 어디에도 없던 매매 상태 플래그를 KIS
공개 CDN 의 시장 전체 zip(kospi_code.mst/kosdaq_code.mst)에서 매일 받아
시점별로 적재한다. 원본 필드는 시장별로 다르고 향후 소비처의 요구가 아직
불확실해 raw JSONB 로 무손실 보존하고, 조회 빈도가 확실한 name 만 승격
컬럼으로 둔다(docs/superpowers/specs/2026-08-18-kis-stock-master-cache-design.md).

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kis_stock_master_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "symbol", "trade_date", name="uq_kis_stock_master_symbol_date"
        ),
    )
    op.create_index(
        "ix_kis_stock_master_date", "kis_stock_master_snapshots", ["trade_date"]
    )
    op.create_index(
        "ix_kis_stock_master_symbol", "kis_stock_master_snapshots", ["symbol"]
    )


def downgrade() -> None:
    op.drop_index("ix_kis_stock_master_symbol", table_name="kis_stock_master_snapshots")
    op.drop_index("ix_kis_stock_master_date", table_name="kis_stock_master_snapshots")
    op.drop_table("kis_stock_master_snapshots")
