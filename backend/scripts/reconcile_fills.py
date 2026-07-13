"""미체결(submitted/partial) 주문을 KIS 체결 조회로 즉시 정합하는 수동 스크립트.

engine/main.py 의 주기적 재조회 루프와 동일한 로직(engine.reconcile.reconcile_open_orders)
을 즉시 1회 이상 실행한다. 초당한도(EGW00201)로 일부 주문 조회가 실패하면 남은 미체결이
없어질 때까지 라운드를 반복한다(라운드 사이 짧게 대기).

사용법 (web 컨테이너에서 실행):
    docker compose run --rm web python scripts/reconcile_fills.py
    docker compose run --rm web python scripts/reconcile_fills.py --user 1 --rounds 5
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import AsyncSessionLocal
from app.core.redis import redis_client
from engine.reconcile import reconcile_open_orders


async def _run(user_id: int | None, rounds: int) -> None:
    for i in range(1, rounds + 1):
        async with AsyncSessionLocal() as db:
            stats = await reconcile_open_orders(db, redis_client, user_id=user_id)
        print(
            f"[라운드 {i}/{rounds}] 조회 {stats['checked']} | "
            f"체결확정 {stats['filled']} | 부분체결 {stats['partial']} | "
            f"조회실패(재시도대상) {stats['errors']}"
        )
        # 더 볼 주문이 없거나(=checked 0) 실패도 없으면 조기 종료.
        if stats["checked"] == 0 or stats["errors"] == 0:
            break
        await asyncio.sleep(1.5)  # 초당한도 완화 후 다음 라운드.


async def _main(user_id: int | None, rounds: int) -> None:
    try:
        await _run(user_id, rounds)
    finally:
        await redis_client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="미체결 주문 KIS 체결 정합")
    parser.add_argument("--user", type=int, default=None, help="특정 사용자만(기본: 전체)")
    parser.add_argument("--rounds", type=int, default=5, help="최대 재시도 라운드(기본 5)")
    args = parser.parse_args()
    asyncio.run(_main(args.user, args.rounds))


if __name__ == "__main__":
    main()
