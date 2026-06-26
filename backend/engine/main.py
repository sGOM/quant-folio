"""매매 엔진 진입점 (독립 프로세스).

- web 과 분리되어 24시간 동작. 웹 재배포와 무관하게 매매를 지속한다.
- Redis ENGINE_CONTROL_CHANNEL 로 전략 start/stop 제어를 수신한다.
- 기동 시 ACTIVE_STRATEGIES_KEY 로 운용 중 전략을 복구한다.
- 각 live 전략을 StrategyRunner 태스크로 구동하고, PriceFeed(WS)로 시세를 공급한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal

from sqlalchemy import select

from app.core.channels import (
    ACTIVE_STRATEGIES_KEY,
    ENGINE_CONTROL_CHANNEL,
    ENGINE_HEARTBEAT_KEY,
)
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis import redis_client
from app.models import Strategy, StrategyStatus, User
from app.services.broker import resolve_credentials
from engine.price_feed import PriceFeedManager
from engine.rebalance_runner import RebalanceRunner
from engine.runner import StrategyRunner

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("engine")

_shutdown = asyncio.Event()           # 그레이스풀 셧다운 신호
_runners: dict[int, dict] = {}        # strategy_id -> {task, stop} 실행 중 전략 러너
_feed_mgr = PriceFeedManager(redis_client)  # 사용자별 실시간 시세 피드 관리자


def _handle_signal(*_args) -> None:
    """SIGINT/SIGTERM 핸들러 — 종료 이벤트를 설정해 그레이스풀 셧다운을 시작한다."""
    logger.info("종료 신호 수신")
    _shutdown.set()


async def _make_runner(strategy_id: int):
    """전략 config.type 에 따라 알맞은 러너를 생성한다.

    'rebalance' 면 주기적 리밸런싱 러너, 그 외(단일종목 신호 전략)는 StrategyRunner.
    조회 실패 시 기존 동작(StrategyRunner)으로 안전하게 폴백한다.
    """
    async with AsyncSessionLocal() as db:
        cfg = await db.scalar(select(Strategy.config).where(Strategy.id == strategy_id))
    if cfg and cfg.get("type") == "rebalance":
        return RebalanceRunner(strategy_id, redis_client)
    return StrategyRunner(strategy_id, redis_client)


async def _start_strategy(strategy_id: int) -> None:
    """전략 러너 태스크를 시작하고 활성 집합·PriceFeed 를 갱신한다(이미 실행 중이면 무시)."""
    if strategy_id in _runners:
        return
    stop = asyncio.Event()
    runner = await _make_runner(strategy_id)
    task = asyncio.create_task(runner.run(stop))
    _runners[strategy_id] = {"task": task, "stop": stop}
    await redis_client.sadd(ACTIVE_STRATEGIES_KEY, strategy_id)
    logger.info("전략 %d start", strategy_id)
    await _sync_feeds()


async def _stop_strategy(strategy_id: int) -> None:
    """전략 러너를 정지(최대 10초 대기 후 취소)하고 활성 집합·PriceFeed 를 갱신한다."""
    entry = _runners.pop(strategy_id, None)
    if entry:
        entry["stop"].set()
        try:
            await asyncio.wait_for(entry["task"], timeout=10)
        except asyncio.TimeoutError:
            entry["task"].cancel()
        except Exception:  # noqa: BLE001
            logger.exception("전략 %d 종료 중 오류", strategy_id)
            entry["task"].cancel()
    await redis_client.srem(ACTIVE_STRATEGIES_KEY, strategy_id)
    logger.info("전략 %d stop", strategy_id)
    await _sync_feeds()


async def _sync_feeds() -> None:
    """활성 전략들의 (사용자 → 종목 집합)을 계산해 PriceFeed 를 동기화."""
    if not _runners:
        await _feed_mgr.shutdown()
        return
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(Strategy, User)
            .join(User, User.id == Strategy.user_id)
            .where(Strategy.id.in_(list(_runners.keys())))
        )
        per_user: dict[int, dict] = {}
        for strategy, user in rows.all():
            resolved = resolve_credentials(user)  # DB 우선, .env 폴백
            if resolved is None:
                continue
            broker, app_key, app_secret, _account = resolved
            # 토스 등 WS 미지원 브로커는 실시간 피드를 띄우지 않는다.
            # runner 가 REST 폴링(get_quote)으로 현재가를 얻는다.
            if broker != "kis":
                continue
            # 리밸런싱 전략은 다종목·저빈도라 WS 피드를 두지 않고 REST quote 를 쓴다.
            symbol = strategy.config.get("symbol")
            if symbol is None:
                continue
            entry = per_user.setdefault(
                user.id,
                {"app_key": app_key, "app_secret": app_secret, "symbols": set()},
            )
            entry["symbols"].add(symbol)

    for uid, info in per_user.items():
        try:
            await _feed_mgr.ensure(uid, info["app_key"], info["app_secret"], info["symbols"])
        except Exception:  # noqa: BLE001
            logger.exception("PriceFeed ensure 실패 user=%d", uid)


async def _control_loop() -> None:
    """Redis pub/sub 제어 메시지 수신."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(ENGINE_CONTROL_CHANNEL)
    logger.info("제어 채널 구독: %s", ENGINE_CONTROL_CHANNEL)
    try:
        while not _shutdown.is_set():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                continue
            try:
                data = json.loads(msg["data"])
                action, sid = data["action"], int(data["strategy_id"])
            except (json.JSONDecodeError, KeyError, ValueError):
                logger.warning("잘못된 제어 메시지: %s", msg.get("data"))
                continue
            if action == "start":
                await _start_strategy(sid)
            elif action == "stop":
                await _stop_strategy(sid)
    finally:
        await pubsub.unsubscribe(ENGINE_CONTROL_CHANNEL)
        await pubsub.aclose()


async def _recover() -> None:
    """기동 시 운용 중(live) 전략 복구 — DB 기준."""
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(
            select(Strategy.id).where(Strategy.status == StrategyStatus.LIVE)
        )
        ids = list(rows)
    for sid in ids:
        await _start_strategy(sid)
    if ids:
        logger.info("운용 중 전략 복구: %s", ids)


async def _heartbeat_loop() -> None:
    """5초마다 생존 신호(TTL 15초)를 갱신해 web 의 엔진 상태 조회에 응답한다."""
    while not _shutdown.is_set():
        await redis_client.set(ENGINE_HEARTBEAT_KEY, "alive", ex=15)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    """엔진 메인 루프 — 신호 핸들러 등록, live 전략 복구, 제어/하트비트 태스크 구동 후
    종료 신호를 받으면 전략·피드를 정리하고 종료한다."""
    logger.info(
        "매매 엔진 시작 — KIS_ENV=%s (모의투자=%s)",
        settings.KIS_ENV, settings.is_paper_trading,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            signal.signal(sig, _handle_signal)

    await _recover()
    tasks = [
        asyncio.create_task(_control_loop()),
        asyncio.create_task(_heartbeat_loop()),
    ]
    await _shutdown.wait()

    logger.info("정리 중 — 전략 %d개 종료", len(_runners))
    for sid in list(_runners.keys()):
        await _stop_strategy(sid)
    await _feed_mgr.shutdown()
    for t in tasks:
        t.cancel()
    await redis_client.aclose()
    logger.info("매매 엔진 종료")


if __name__ == "__main__":
    asyncio.run(main())
