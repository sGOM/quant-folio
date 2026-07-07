"""리밸런싱 전략을 KIS 모의투자(vts)로 점검·실행하는 실행 스크립트.

id=23 처럼 universe_rule.source=KOSPI200(시점별 PIT 구성종목) 전략도 그대로 구동한다.
RebalanceRunner 를 재사용하므로 백테스트와 동일한 선정·목표비중 로직을 탄다.

사용법 (web 컨테이너에서 실행):
    # ① 미리보기 — 주문을 내지 않고 '지금 리밸런싱하면 낼 주문'만 출력(안전).
    docker compose run --rm web python scripts/paper_rebalance.py --strategy 23

    # ② 모의투자 실행 — KIS 모의투자 계좌로 실제 주문 전송.
    docker compose run --rm web python scripts/paper_rebalance.py --strategy 23 --execute

안전장치:
  - --execute 는 settings.is_paper_trading(KIS_ENV=vts) 가 아니면 거부한다(실전 오발주 방지).
  - 기본은 미리보기(--execute 미지정)이며 어떤 주문도 내지 않는다.
  - 장중이 아니어도 강제 1회 리밸런싱을 수행한다(모의 점검 목적). 정기 스케줄 발화가
    아니라 수동 실행이므로 Redis 의 '마지막 실행일'은 소비하지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# `python scripts/paper_rebalance.py` 로 실행하면 sys.path[0] 이 scripts/ 라 app 을 못 찾는다.
# 리포지토리 루트(backend/)를 경로에 추가한다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.core.redis import redis_client
from app.services.market import now_kst
from engine.rebalance_runner import RebalanceRunner


def _fmt_orders(orders: list) -> str:
    if not orders:
        return "  (없음)"
    return "\n".join(f"  {side.upper():4} {sym} x{qty}" for sym, side, qty in orders)


async def _run(strategy_id: int, execute: bool) -> None:
    runner = RebalanceRunner(strategy_id, redis_client)

    # 미리보기: 어떤 주문도 내지 않고 선정·목표·산출 주문을 계산해 출력.
    plan = await runner.preview()
    print(f"\n=== 전략 {strategy_id} 리밸런싱 미리보기 ({plan['as_of']} 기준) ===")
    print(f"모의투자 모드(is_paper_trading): {settings.is_paper_trading} (KIS_ENV={settings.KIS_ENV})")
    print(f"후보풀 크기: {plan['pool_size']}종목 | 레짐 위험회피: {plan['risk_off']}")
    print(f"목표비중({len(plan['targets'])}종목):")
    for sym, w in sorted(plan["targets"].items(), key=lambda x: -x[1]):
        px = plan["prices"].get(sym)
        print(f"  {sym}  {w * 100:5.1f}%" + (f"  @ {px:,.0f}" if px else "  (현재가 미조회)"))
    print(f"현재 보유: {plan['positions'] or '(없음)'}")
    print("산출 주문:")
    print(_fmt_orders(plan["orders"]))

    if not execute:
        print("\n[미리보기] 주문을 실행하지 않았습니다. 실제 모의투자 주문은 --execute 를 붙이세요.")
        return

    if not settings.is_paper_trading:
        print("\n[거부] KIS_ENV 가 모의투자(vts)가 아닙니다. 실전 오발주 방지를 위해 --execute 를 거부합니다.")
        return

    if not plan["orders"]:
        print("\n[실행] 낼 주문이 없습니다(드리프트 밴드 내 또는 선정 없음). 종료.")
        return

    print("\n[실행] KIS 모의투자로 주문을 전송합니다...")
    # 수동 강제 실행: 장중/발화 게이트를 건너뛰고 리밸런싱 1회 수행(스케줄 미소비).
    await runner._rebalance_once(now_kst(), risk_off=plan["risk_off"], bar_tag="manual")
    print("[실행] 완료. 체결/주문 내역은 앱의 체결 로그·모의투자 계좌에서 확인하세요.")


async def _main(strategy_id: int, execute: bool) -> None:
    try:
        await _run(strategy_id, execute)
    finally:
        await redis_client.aclose()  # 같은 이벤트 루프에서 닫아야 한다


def main() -> None:
    parser = argparse.ArgumentParser(description="리밸런싱 전략 모의투자 점검·실행")
    parser.add_argument("--strategy", type=int, default=23, help="전략 ID(기본 23)")
    parser.add_argument(
        "--execute", action="store_true",
        help="실제 모의투자 주문 전송(미지정 시 미리보기만)",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.strategy, args.execute))


if __name__ == "__main__":
    main()
