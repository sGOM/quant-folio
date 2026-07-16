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
import json
import logging
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select

from app.core.channels import (
    FAILURE_ALERT_THRESHOLD,
    engine_health_key,
    position_lock_key,
)
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import Position, Strategy, User
from app.services.broker import BrokerClient, make_broker_for_user, user_has_credentials
from app.services.live_gate import evaluate_live_gate
from app.services.market import now_kst
from engine.alerts import publish_alert
from engine.executor import execute_signal, make_idempotency_key

_POSITION_LOCK_TTL = 30  # 초
# 러너 헬스 상태 TTL — 러너가 정지하면(틱 미갱신) 이 시간 뒤 자연 소멸해 stale 상태가
# 조회 결과에 남지 않게 한다. 정상 운용 중이면 매 틱마다 갱신되므로 만료되지 않는다.
_HEALTH_TTL = 60 * 60 * 24  # 24시간


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
    # 연속 실패가 이 횟수에 도달하면 알림을 1회 발행한다(임계 초과 알림). 공용 상수를
    # 기본값으로 두되 서브클래스가 필요 시 오버라이드할 수 있다.
    _failure_alert_threshold: int = FAILURE_ALERT_THRESHOLD

    def __init__(self, strategy_id: int, redis: Redis):
        self.strategy_id = strategy_id
        self.redis = redis
        self._cfg: dict = {}
        self._user_id: int | None = None
        self._broker: BrokerClient | None = None
        # 러너 헬스 상태(마지막 성공 틱 시각·연속 실패 횟수)를 인메모리로도 들고 있어
        # 임계 초과 알림의 dedup(같은 실패 구간 재발송 방지)에 쓴다. Redis 는 web 의
        # 조회 응답용 영속 사본이다.
        self._consecutive_failures: int = 0
        self._last_success_ts: str | None = None

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
                await self._record_tick_failure(
                    f"{self._tick_word} 타임아웃({self._tick_timeout}초)"
                )
            except Exception as e:  # noqa: BLE001
                self._logger.exception(
                    "%s %d %s 오류", self._label, self.strategy_id, self._tick_word
                )
                await self._record_tick_failure(f"{self._tick_word} 오류: {e}")
            else:
                await self._record_tick_success()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

        self._logger.info("%s %d 실행 종료", self._label, self.strategy_id)

    # ───────────────────── 러너 헬스 상태(마지막 성공/연속 실패) ─────────────────────
    #
    # 프로세스 전역 heartbeat(ENGINE_HEARTBEAT_KEY)는 엔진 데몬 전체의 생존만 알린다.
    # 개별 전략 러너가 조용히 실패(타임아웃·예외 반복)해도 heartbeat 는 alive 라, 러너
    # 단위 헬스를 Redis(engine:health:{id})에 별도로 남겨 web 이 조회하고 임계 초과 시
    # 알림을 발행한다. 상태는 인메모리(_consecutive_failures)를 소스로 갱신하며, Redis 는
    # 조회 응답용 영속 사본(TTL 로 stale 자연 소멸)이다.
    def _health_key(self) -> str:
        return engine_health_key(self.strategy_id)

    async def _write_health(self, last_error: str | None) -> None:
        """현재 헬스 상태를 Redis 에 기록한다(조회용 사본). I/O 실패는 무시(방어)."""
        state = {
            "strategy_id": self.strategy_id,
            "user_id": self._user_id,
            "last_success_ts": self._last_success_ts,
            "consecutive_failures": self._consecutive_failures,
            "last_error": last_error,
            "updated_at": now_kst().isoformat(),
        }
        try:
            await self.redis.set(
                self._health_key(), json.dumps(state, default=str), ex=_HEALTH_TTL
            )
        except Exception:  # noqa: BLE001
            self._logger.debug("헬스 상태 기록 실패(무시) — 전략 %d", self.strategy_id)

    async def _record_tick_success(self) -> None:
        """틱 성공 — 마지막 성공 시각을 갱신하고 연속 실패 카운트를 리셋한다."""
        self._consecutive_failures = 0
        self._last_success_ts = now_kst().isoformat()
        await self._write_health(last_error=None)

    async def _record_tick_failure(self, reason: str) -> None:
        """틱 실패(타임아웃/예외) — 연속 실패 카운트를 올리고 임계 도달 시 1회 알림.

        알림 dedup: 카운트가 임계를 '처음 넘는 시점(== 임계)'에만 발행한다. 계속 실패해도
        재발송하지 않으며, 성공으로 0 리셋된 뒤 다시 임계에 도달하면 재발송한다.
        """
        self._consecutive_failures += 1
        await self._write_health(last_error=reason)
        if self._consecutive_failures == self._failure_alert_threshold:
            await publish_alert(
                self.redis,
                user_id=self._user_id,
                strategy_id=self.strategy_id,
                severity="critical",
                code="runner_failures",
                message=(
                    f"{self._label} {self.strategy_id} 연속 {self._consecutive_failures}회 "
                    f"실패 — 마지막 원인: {reason}"
                ),
            )

    async def _holding_qty(self, db, symbol: str) -> Decimal:
        """현재 보유 수량을 반환한다(포지션 없으면 0)."""
        pos = await db.scalar(
            select(Position).where(
                Position.user_id == self._user_id,
                Position.strategy_id == self.strategy_id,
                Position.symbol == symbol,
            )
        )
        return pos.qty if pos else Decimal("0")

    async def _place(
        self, db, symbol: str, side: str, qty: int, price: Decimal, bar_ts: str,
        reason: str | None = None,
    ) -> None:
        """멱등성 키를 구성해 주문을 실행한다(executor 재사용).

        :param reason: 감사 로그용 주문 사유(Order.reason 에 기록).
        """
        # 실전(prod) 하드 게이트 — 실제 자금이 나가기 직전 마지막 방어. prod 일 때만
        # 강제하고, 실패하면 주문을 막고 critical 알림을 보낸다(vts 는 게이트가 로깅만).
        if not settings.is_paper_trading:
            gate = await evaluate_live_gate(
                db, self._user_id, self.strategy_id,
                order_notional=Decimal(str(qty)) * price,
            )
            if not gate.passed:
                self._logger.error(
                    "실전 게이트 차단 — 주문 거부 %s %s %d주: %s",
                    symbol, side, qty, "; ".join(gate.reasons),
                )
                await publish_alert(
                    self.redis,
                    user_id=self._user_id,
                    strategy_id=self.strategy_id,
                    severity="critical",
                    code="live_gate_blocked",
                    message=(
                        f"실전 게이트 차단 — {symbol} {side} {qty:,}주 주문 거부. "
                        f"사유: {'; '.join(gate.reasons)}"
                    ),
                )
                return

        await execute_signal(
            db, self.redis, self._broker,
            user_id=self._user_id, strategy_id=self.strategy_id,
            symbol=symbol, side=side, qty=qty, price=price,
            idempotency_key=make_idempotency_key(self.strategy_id, symbol, side, bar_ts),
            reason=reason,
        )
