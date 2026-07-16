"""다중 전략 포지션 분리 — positions·executions 에 strategy_id 추가

같은 종목을 두 전략(러너)이 동시에 보유할 때 포지션이 섞이던 문제를 해소한다.
- positions.strategy_id: 포지션 소유 전략. UNIQUE 를 (user_id, symbol) →
  (user_id, strategy_id, symbol) 로 교체해 전략별로 독립 포지션을 유지한다.
- executions.strategy_id: 비정규화 컬럼(전략별 체결 직접 조회용). orders.strategy_id 에서 채운다.

백필: positions 는 해당 (user_id, symbol) 로 나간 주문의 strategy_id 가 단일값으로
유일하게 특정될 때만 그 값으로 채우고, 여러 전략이 같은 종목에 주문한 이력이 있어
애매하면 NULL 로 남긴다(안전). executions 는 orders.strategy_id 로 조인해 그대로 채운다.

주의: downgrade 시 백필로 채워진 strategy_id 값은 컬럼과 함께 유실된다(복구 불가).
strategy_id=NULL 비귀속 포지션은 신규 3-컬럼 UNIQUE 에서도 종목당 여러 행이 가능하다
(Postgres 는 NULL 을 서로 다른 값으로 취급). 의도된 동작이다.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 컬럼 추가(nullable) + FK(SET NULL) + 인덱스.
    op.add_column("positions", sa.Column("strategy_id", sa.Integer(), nullable=True))
    op.add_column("executions", sa.Column("strategy_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "positions_strategy_id_fkey", "positions", "strategies",
        ["strategy_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "executions_strategy_id_fkey", "executions", "strategies",
        ["strategy_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_positions_strategy_id", "positions", ["strategy_id"])
    op.create_index("ix_executions_strategy_id", "executions", ["strategy_id"])

    # 2) 백필.
    # positions: (user_id, symbol) 에 대해 주문 이력의 strategy_id 가 단일값으로
    # 유일할 때만 채운다(여러 전략이면 애매 → NULL 유지).
    op.execute(
        """
        UPDATE positions p
        SET strategy_id = sub.sid
        FROM (
            SELECT user_id, symbol, MIN(strategy_id) AS sid
            FROM orders
            WHERE strategy_id IS NOT NULL
            GROUP BY user_id, symbol
            HAVING COUNT(DISTINCT strategy_id) = 1
        ) sub
        WHERE p.user_id = sub.user_id
          AND p.symbol = sub.symbol
          AND p.strategy_id IS NULL
        """
    )
    # executions: order 당 strategy_id 는 이미 확정값이므로 애매함 없이 조인해 채운다.
    op.execute(
        """
        UPDATE executions e
        SET strategy_id = o.strategy_id
        FROM orders o
        WHERE e.order_id = o.id
          AND o.strategy_id IS NOT NULL
        """
    )

    # 3) UNIQUE 제약 교체: (user_id, symbol) → (user_id, strategy_id, symbol).
    op.drop_constraint("uq_positions_user_symbol", "positions", type_="unique")
    op.create_unique_constraint(
        "uq_positions_user_strategy_symbol",
        "positions",
        ["user_id", "strategy_id", "symbol"],
    )


def downgrade() -> None:
    # UNIQUE 제약 원복: (user_id, strategy_id, symbol) → (user_id, symbol).
    # 주의: 신규 스키마에서 동일 (user_id, symbol) 에 전략별로 여러 포지션이 존재하면
    # 이 원복은 중복으로 실패할 수 있다(다운그레이드 전 정합성 정리 필요).
    op.drop_constraint("uq_positions_user_strategy_symbol", "positions", type_="unique")
    op.create_unique_constraint(
        "uq_positions_user_symbol", "positions", ["user_id", "symbol"]
    )

    op.drop_index("ix_executions_strategy_id", table_name="executions")
    op.drop_index("ix_positions_strategy_id", table_name="positions")
    op.drop_constraint("executions_strategy_id_fkey", "executions", type_="foreignkey")
    op.drop_constraint("positions_strategy_id_fkey", "positions", type_="foreignkey")
    # 백필로 채워진 strategy_id 값은 컬럼과 함께 유실된다(복구 불가).
    op.drop_column("executions", "strategy_id")
    op.drop_column("positions", "strategy_id")
