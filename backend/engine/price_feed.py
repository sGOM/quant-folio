"""실시간 가격 공급 — 사용자별 KIS WS 를 구독해 Redis 가격 캐시를 채운다.

여러 전략이 같은 종목을 봐도 사용자당 WS 1개로 묶는다. 종목 집합이 바뀌면
재구독한다. 연결이 끊기면 백오프 후 재연결한다(감독 루프).
runner 는 이 캐시(price:{symbol})를 우선 사용하고 없으면 REST 로 폴백한다.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from redis.asyncio import Redis

from engine.kis_ws import KisWebSocketClient

logger = logging.getLogger("engine.price_feed")

_PRICE_TTL = 120  # 가격 캐시 TTL(초). 피드가 끊겨도 일정 시간 마지막 가격을 유지.


class PriceFeedManager:
    """사용자별 KIS 시세 WS 피드를 관리한다(사용자당 1개, 종목 집합 변경 시 재구독)."""

    def __init__(self, redis: Redis):
        self.redis = redis
        self._feeds: dict[int, dict] = {}  # user_id -> {task, stop, symbols}

    async def ensure(
        self, user_id: int, app_key: str, app_secret: str, symbols: set[str]
    ) -> None:
        """사용자의 시세 피드를 원하는 종목 집합으로 맞춘다.

        이미 같은 종목 집합이면 아무것도 하지 않고, 다르면 기존 피드를 정리한 뒤
        새 피드를 시작한다. 빈 집합이면 피드를 두지 않는다.
        """
        cur = self._feeds.get(user_id)
        if cur and cur["symbols"] == symbols:
            return
        await self.remove(user_id)
        if not symbols:
            return

        stop = asyncio.Event()

        async def on_price(sym: str, price: Decimal) -> None:
            await self.redis.set(f"price:{sym}", str(price), ex=_PRICE_TTL)

        client = KisWebSocketClient(app_key, app_secret, on_price)
        task = asyncio.create_task(self._supervise(client, list(symbols), stop))
        self._feeds[user_id] = {"task": task, "stop": stop, "symbols": set(symbols)}
        logger.info("PriceFeed 시작 user=%d symbols=%s", user_id, sorted(symbols))

    async def _supervise(
        self, client: KisWebSocketClient, symbols: list[str], stop: asyncio.Event
    ) -> None:
        """WS 연결을 감독하며 끊기면 재연결한다(오류 시 지수 백오프, 취소는 전파)."""
        backoff = 5        # 현재 대기 시간(초)
        max_backoff = 120  # 백오프 상한(초)
        while not stop.is_set():
            try:
                await client.run(symbols, stop)
                # 정상 반환(연결 종료) — busy-retry 방지를 위해 짧게 대기 후 재연결.
                wait = backoff if stop.is_set() else 3
            except asyncio.CancelledError:
                raise  # 취소는 그대로 전파(정리 경로 보존)
            except Exception as e:  # noqa: BLE001
                logger.warning("PriceFeed WS 오류, %ds 후 재연결: %s", backoff, e)
                wait = backoff
                backoff = min(backoff * 2, max_backoff)  # 지수 백오프
            else:
                backoff = 5  # 정상 종료면 백오프 리셋
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def remove(self, user_id: int) -> None:
        """사용자의 시세 피드를 중지·정리한다(없으면 무시)."""
        feed = self._feeds.pop(user_id, None)
        if not feed:
            return
        feed["stop"].set()
        feed["task"].cancel()
        try:
            await feed["task"]
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("PriceFeed 종료 중 오류 user 정리")

    async def shutdown(self) -> None:
        """모든 사용자 피드를 정리한다(엔진 종료·비활성 시)."""
        for uid in list(self._feeds):
            await self.remove(uid)
