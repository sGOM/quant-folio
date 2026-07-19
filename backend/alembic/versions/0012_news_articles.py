"""경제 뉴스 테이블 추가 — news_articles·news_article_symbols (docs/ROADMAP.md M3).

언론사 RSS 에서 수집한 기사(요약+원문 링크)와 기사↔종목 매핑을 적재한다.
url 유니크 제약으로 재수집 시 중복 삽입을 막고, (symbol, article_id) 인덱스가
"이 종목의 관련 기사" 조회를 담당한다.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("url", name="uq_news_articles_url"),
    )
    op.create_index("ix_news_articles_published", "news_articles", ["published_at"])

    op.create_table(
        "news_article_symbols",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "article_id",
            sa.Integer,
            sa.ForeignKey("news_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.UniqueConstraint("article_id", "symbol", name="uq_news_article_symbols"),
    )
    op.create_index("ix_news_article_symbols_article_id", "news_article_symbols", ["article_id"])
    op.create_index(
        "ix_news_article_symbols_symbol_article",
        "news_article_symbols",
        ["symbol", "article_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_news_article_symbols_symbol_article", table_name="news_article_symbols")
    op.drop_index("ix_news_article_symbols_article_id", table_name="news_article_symbols")
    op.drop_table("news_article_symbols")
    op.drop_index("ix_news_articles_published", table_name="news_articles")
    op.drop_table("news_articles")
