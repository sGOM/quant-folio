"""시장 태그(market)가 없는 로컬 스토어 행과 그 페치 원장을 정리한다.

§49 B1: `stock_daily_snapshots`·`stock_period_stats` 는 PK 에 시장 구분이 없어
KOSPI 행과 KOSDAQ 행이 같은 키공간에 섞인다. 초기 구현은 쓰기 시점에 `market` 을
채우는 곳이 `_fetch_fundamentals` 뿐이었고 읽기에 시장 필터도 없었다. 지금은 5개
쓰기 경로가 모두 태깅하고 읽기가 `market IN (...)` 으로 거른다.

그래서 **그 이전 스키마로 적재된 행(`market IS NULL`)은 이제 읽기 필터에 전부
걸러진다.** 원장이 그 캐시키를 `final=True` 로 기록해 뒀다면 `cached_frame` 은
로컬을 읽어 빈 결과를 그대로 반환하고 **재조회하지 않는다** — 수동 삭제 전까지
영구히 "데이터 없음"이 된다. 이 브랜치가 없애려는 조용한 실패 그 자체다.

`cached_frame` 의 "빈 결과는 확정하지 않는다" 가드(§49 I3)는 **쓰기 시점**에만
작동하므로 이미 기록된 원장 행을 되돌리지 못한다. 그래서 산문 규약("저장소는 빈
상태에서 시작해야 한다")에 기대는 대신 여기서 한 번 정리한다.

스토어는 재조회 가능한 캐시이므로 삭제는 무손실이다 — 다음 조회가 외부에서 다시
받아 태그와 함께 적재한다. 태그된 행은 건드리지 않으므로 이미 정상인 환경에서는
아무것도 지워지지 않는다(멱등).

되돌림은 no-op 다. 지운 것은 파생 캐시라 복원 대상이 아니고, downgrade 가 행을
되살릴 방법도 없다.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-08

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 위 두 테이블에 적재하는 `cached_frame` 의 source 값(app/services/metrics/fetch.py).
#: 원장 행을 남겨두면 태그 없는 행을 지워도 재조회가 일어나지 않는다.
_STORE_SOURCES = (
    "fundamentals",
    "market_cap",
    "market_ohlcv",
    "price_change",
    "net_purchases",
)


def upgrade() -> None:
    for table in ("stock_daily_snapshots", "stock_period_stats"):
        op.execute(f"DELETE FROM {table} WHERE market IS NULL")

    sources = ", ".join(f"'{s}'" for s in _STORE_SOURCES)
    op.execute(f"DELETE FROM external_fetches WHERE source IN ({sources})")


def downgrade() -> None:
    """no-op — 지운 것은 외부에서 재조회 가능한 파생 캐시다."""
