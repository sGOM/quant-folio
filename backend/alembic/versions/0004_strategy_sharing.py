"""strategy sharing — 닉네임, 공유/정렬/즐겨찾기 컬럼, strategy_likes 테이블

전략을 다른 사용자와 공유·복사하고 좋아요를 누르며, 내 전략 목록을 정렬·즐겨찾기
할 수 있게 한다.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 공유 목록에 표시할 닉네임(없으면 '익명'으로 표기)
    op.add_column("users", sa.Column("display_name", sa.String(50), nullable=True))

    # 전략 공유·정렬·즐겨찾기 메타데이터 (기존 행은 server_default 로 안전 채움)
    op.add_column(
        "strategies",
        sa.Column("is_shared", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "strategies",
        sa.Column("shared_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "strategies",
        sa.Column("is_favorite", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "strategies",
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )
    # 복사 출처 추적(원본 삭제 시 NULL). 자기참조 FK.
    op.add_column(
        "strategies",
        sa.Column(
            "copied_from_id",
            sa.Integer,
            sa.ForeignKey("strategies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # 좋아요 — (strategy_id, user_id) 유니크로 인당 1회를 DB 레벨에서 보장
    op.create_table(
        "strategy_likes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "strategy_id",
            sa.Integer,
            sa.ForeignKey("strategies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("strategy_id", "user_id", name="uq_strategy_likes_user"),
    )
    op.create_index("ix_strategy_likes_strategy", "strategy_likes", ["strategy_id"])


def downgrade() -> None:
    op.drop_index("ix_strategy_likes_strategy", table_name="strategy_likes")
    op.drop_table("strategy_likes")
    op.drop_column("strategies", "copied_from_id")
    op.drop_column("strategies", "sort_order")
    op.drop_column("strategies", "is_favorite")
    op.drop_column("strategies", "shared_at")
    op.drop_column("strategies", "is_shared")
    op.drop_column("users", "display_name")
