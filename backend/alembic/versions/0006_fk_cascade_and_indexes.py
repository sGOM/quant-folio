"""executions FK CASCADE + FK 인덱스 보강 (코드리뷰 D1·D2)

D1: executions.order_id 의 ON DELETE 를 RESTRICT→CASCADE 로 바꾼다. 기존 RESTRICT 는
    users→orders CASCADE 삭제(계정 파기·GDPR) 시, 체결이력이 있는 order 에서 걸려
    트랜잭션 전체가 롤백되어 '한 번이라도 체결된 유저는 영구 삭제 불가' 상태였다.
D2: orders.strategy_id(SET NULL)·risk_limits.strategy_id(CASCADE) 에 인덱스가 없어
    전략 삭제 시 참조무결성 확인이 풀스캔이 된다. 인덱스를 추가한다.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # D1: executions.order_id FK 를 CASCADE 로 재생성(인라인 생성이라 Postgres 기본명 사용).
    op.drop_constraint("executions_order_id_fkey", "executions", type_="foreignkey")
    op.create_foreign_key(
        "executions_order_id_fkey", "executions", "orders",
        ["order_id"], ["id"], ondelete="CASCADE",
    )

    # D2: FK 인덱스 보강.
    op.create_index("ix_orders_strategy_id", "orders", ["strategy_id"])
    op.create_index("ix_risk_limits_strategy_id", "risk_limits", ["strategy_id"])


def downgrade() -> None:
    op.drop_index("ix_risk_limits_strategy_id", table_name="risk_limits")
    op.drop_index("ix_orders_strategy_id", table_name="orders")

    op.drop_constraint("executions_order_id_fkey", "executions", type_="foreignkey")
    op.create_foreign_key(
        "executions_order_id_fkey", "executions", "orders",
        ["order_id"], ["id"], ondelete="RESTRICT",
    )
