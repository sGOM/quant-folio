"""주문조직번호(kis_order_org_no) 컬럼 추가 — 체결통보 매칭 정확도 보강용.

KIS 주문 응답의 KRX_FWDG_ORD_ORGNO(주문조직번호)는 지금까지 버려지고 있었다.
실시간 체결통보(engine/fill_notice.py) 매칭 정확도를 높이기 위해 orders 테이블에
보존해 둔다. 매칭 자체는 여전히 kis_order_id(ODNO) 를 1차 키로 쓰고, 이 컬럼은
스키마상 보강 목적(현재 KIS 체결통보 프레임 표준 필드셋에는 ORGNO 가 노출되지
않는 것으로 확인되어, 당장은 매칭에 직접 사용하지 않는다 — fill_notice.py 상단
docstring 참고).

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders", sa.Column("kis_order_org_no", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orders", "kis_order_org_no")
