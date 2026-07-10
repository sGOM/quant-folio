"""러너 공통 베이스 — 전략 적재·실행 루프·주문 래핑의 공통 골격.

StrategyRunner(단일종목 신호)와 RebalanceRunner(리밸런싱)가 공유하는 부분:
  - 전략/사용자 적재 및 자격 검증(`_load` → `_on_load` 훅)
  - stop_event 기반 주기 실행 루프(틱 타임아웃 방어 포함, `run` → `_tick_once` 훅)
  - 보유 수량 조회·주문 실행 래핑(`_holding_qty`, `_place`)
  - (user, symbol) 분산 락(`_position_lock`)

서브클래스는 `_tick_once`(틱 본문)와 `_log_start`(시작 로그)를 구현하고, 필요하면
`_on_load`(추가 적재)를 오버라이드한다. 로그 라벨/주기는 클래스 속성으로 조정한다.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select

from app.core.channels import position_lock_key
from app.core.database import AsyncSessionLocal
from app.models import Position, Strategy, User
from app.services.broker import BrokerClient, make_broker_for_user, user_has_credentials
from engine.executor import execute_signal, make_idempotency_key

_POSITION_LOCK_TTL = 30  # 초


@contextlib.asynccontextmanager
async def _position_lock(redis: Redis, user_id: int, symbol: str):
    """(user, symbol) 단위 분산 락. 획득 실패 시 acquired=False 로 진입."""
    key = position_lock_key(user_id, symbol)
    acquired = bool(await redis.set(key, "1", nx=True, ex=_POSITION_LOCK_TTL))
    try:
        yield acquired
    finally:
        if acquired:
            await redis.delete(key)


class BaseRunner:
    """전략 러너 공통 골격.

    :param strategy_id: 구동할 전략 ID
    :param redis: 가격 캐시·분산 락·이벤트 발행에 쓰는 Redis 클라이언트
    """

    # 서브클래스가 조정하는 실행/로그 파라미터.
    _logger: logging.Logger = logging.getLogger("engine.base_runner")
    _label: str = "전략"          # 로그 라벨(예: "전략", "리밸런싱 전략")
    _tick_word: str = "tick"       # 틱 표현(예: "tick", "점검")
    _poll_interval: int = 30       # 초 — 틱 주기
    _tick_timeout: int = 120       # 초 — 틱 1회 최대 허용 시간(외부 I/O 무응답 방어)

    def __init__(self, strategy_id: int, redis: Redis):
        self.strategy_id = strategy_id
        self.redis = redis
        self._cfg: dict = {}
        self._user_id: int | None = None
        self._broker: BrokerClient | None = None

    async def _load(self) -> bool:
        """전략·사용자를 적재하고 자격을 검증한다. 성공 시 True.

        공통 필드(cfg/user_id/broker) 설정 후 서브클래스별 추가 적재를 `_on_load` 훅에
        위임한다(같은 DB 세션 재사용).
        """
        async with AsyncSessionLocal() as db:
            s = await db.scalar(select(Strategy).where(Strategy.id == self.strategy_id))
            if s is None:
                self._logger.warning("전략 %d 없음 — 실행 취소", self.strategy_id)
                return False
            user = await db.scalar(select(User).where(User.id == s.user_id))
            if user is None or not user_has_credentials(user):
                self._logger.warning(
                    "전략 %d 사용자 증권사 미등록 — 실행 취소", self.strategy_id
                )
                return False

            self._cfg = dict(s.config)
            self._user_id = s.user_id
            self._broker = make_broker_for_user(user)
            await self._on_load(db)
        return True

    async def _on_load(self, db) -> None:
        """서브클래스 추가 적재 훅(기본 no-op). `_load` 의 DB 세션을 그대로 받는다."""

    def _log_start(self) -> None:
        """실행 시작 로그(서브클래스가 구현)."""
        raise NotImplementedError

    async def _tick_once(self) -> None:
        """틱 1회 본문(서브클래스가 구현)."""
        raise NotImplementedError

    async def run(self, stop_event: asyncio.Event) -> None:
        """전략 적재 후 stop_event 가 설정될 때까지 주기적으로 틱을 실행한다.

        각 틱은 `_tick_timeout` 으로 감싸 외부 조회가 무응답으로 멈춰도 러너 전체가
        영구 정지하지 않게 방어한다.
        """
        if not await self._load():
            return
        self._log_start()

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(self._tick_once(), timeout=self._tick_timeout)
            except asyncio.TimeoutError:
                self._logger.warning(
                    "%s %d %s 타임아웃(%d초) — 외부 조회 지연으로 판단, "
                    "이번 틱 중단하고 다음 주기 재시도",
                    self._label, self.strategy_id, self._tick_word, self._tick_timeout,
                )
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "%s %d %s 오류", self._label, self.strategy_id, self._tick_word
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

        self._logger.info("%s %d 실행 종료", self._label, self.strategy_id)

    async def _holding_qty(self, db, symbol: str) -> Decimal:
        """현재 보유 수량을 반환한다(포지션 없으면 0)."""
        pos = await db.scalar(
            select(Position).where(
                Position.user_id == self._user_id, Position.symbol == symbol
            )
        )
        return pos.qty if pos else Decimal("0")

    async def _place(
        self, db, symbol: str, side: str, qty: int, price: Decimal, bar_ts: str
    ) -> None:
        """멱등성 키를 구성해 주문을 실행한다(executor 재사용)."""
        await execute_signal(
            db, self.redis, self._broker,
            user_id=self._user_id, strategy_id=self.strategy_id,
            symbol=symbol, side=side, qty=qty, price=price,
            idempotency_key=make_idempotency_key(self.strategy_id, symbol, side, bar_ts),
        )
