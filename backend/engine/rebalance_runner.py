"""리밸런싱 실행기 — 단일 리밸런싱 전략을 구동.

흐름: 주기적으로(기본 60초) 발화 조건 확인 → due 시 universe 종가 시드 → 목표비중
산정 → 현재가·보유 조회 → 드리프트 밴드 초과분 주문(매도 우선 → 매수) → 마지막
실행일 기록. 주문 실행·멱등성·리스크는 기존 executor/risk 를 그대로 재사용한다.

상태(마지막 실행일)는 Redis 에 보관하고, 멱등성 키에 실행일을 넣어 같은 거래일
중복 발화 시에도 주문이 이중 생성되지 않게 한다(orders.idempotency_key UNIQUE).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

import pandas as pd
from redis.asyncio import Redis
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models import Position, Strategy, User
from app.services.broker import BrokerClient, make_broker_for_user, user_has_credentials
from app.services.data.loader import (
    get_close_series,
    load_ohlcv,
    upsert_price_ticks,
)
from app.services.market import now_kst
from engine import risk
from engine.executor import execute_signal, make_idempotency_key
from engine.rebalance import (
    compute_rebalance_orders,
    compute_target_weights,
    is_rebalance_due,
)
from engine.runner import _position_lock

logger = logging.getLogger("engine.rebalance_runner")

_POLL_INTERVAL = 60  # 초 — 발화 시점 점검 주기
_LAST_PREFIX = "rebalance:last:"
_LAST_TTL = 60 * 60 * 24 * 90  # 90일(휴장 등 대비 여유)


class RebalanceRunner:
    """단일 리밸런싱 전략을 주기적으로 점검·실행하는 실행기.

    :param strategy_id: 구동할 전략 ID
    :param redis: 마지막 실행일 보관·분산 락·이벤트 발행용 Redis 클라이언트
    """

    def __init__(self, strategy_id: int, redis: Redis):
        self.strategy_id = strategy_id
        self.redis = redis
        self._cfg: dict = {}
        self._user_id: int | None = None
        self._broker: BrokerClient | None = None

    async def _load(self) -> bool:
        async with AsyncSessionLocal() as db:
            s = await db.scalar(select(Strategy).where(Strategy.id == self.strategy_id))
            if s is None:
                logger.warning("전략 %d 없음 — 실행 취소", self.strategy_id)
                return False
            user = await db.scalar(select(User).where(User.id == s.user_id))
            if user is None or not user_has_credentials(user):
                logger.warning("전략 %d 사용자 증권사 미등록 — 실행 취소", self.strategy_id)
                return False
            self._cfg = dict(s.config)
            self._user_id = s.user_id
            self._broker = make_broker_for_user(user)
        return True

    async def run(self, stop_event: asyncio.Event) -> None:
        """전략 적재 후 stop_event 가 설정될 때까지 주기적으로 발화 조건을 점검한다."""
        if not await self._load():
            return
        logger.info(
            "리밸런싱 전략 %d 실행 시작 (universe=%d종목, %s)",
            self.strategy_id, len(self._cfg.get("universe", [])), self._cfg.get("cadence"),
        )
        while not stop_event.is_set():
            try:
                await self._maybe_rebalance()
            except Exception:  # noqa: BLE001
                logger.exception("리밸런싱 전략 %d 점검 오류", self.strategy_id)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
        logger.info("리밸런싱 전략 %d 실행 종료", self.strategy_id)

    async def _maybe_rebalance(self) -> None:
        now = now_kst()
        last = await self._get_last()
        if not is_rebalance_due(self._cfg, last, now):
            return
        logger.info("리밸런싱 전략 %d 발화 (%s)", self.strategy_id, now.isoformat())
        await self._rebalance_once(now)
        await self._set_last(now)

    # ───────────────────── 마지막 실행일 상태 ─────────────────────
    def _last_key(self) -> str:
        return f"{_LAST_PREFIX}{self.strategy_id}"

    async def _get_last(self) -> datetime | None:
        raw = await self.redis.get(self._last_key())
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    async def _set_last(self, dt: datetime) -> None:
        await self.redis.set(self._last_key(), dt.isoformat(), ex=_LAST_TTL)

    # ───────────────────── 리밸런싱 1회 ─────────────────────
    async def _rebalance_once(self, now: datetime) -> None:
        universe = list(self._cfg.get("universe", []))
        history = await self._seed_history(universe)
        targets = compute_target_weights(history, self._cfg)
        if not targets:
            logger.warning("리밸런싱 전략 %d 선정 종목 없음(데이터 부족) — 건너뜀", self.strategy_id)
            return

        async with AsyncSessionLocal() as db:
            positions = await self._holdings(db, universe)

        # 매매 후보 = 목표 종목 ∪ 현재 보유 종목
        symbols = set(targets) | set(positions)
        prices = await self._quotes(symbols)

        orders = compute_rebalance_orders(
            targets=targets,
            positions={s: float(q) for s, q in positions.items()},
            prices={s: float(p) for s, p in prices.items()},
            capital=float(self._cfg.get("capital", 10_000_000)),
            drift_band=float(self._cfg.get("drift_band_pct", 0.05)),
        )
        if not orders:
            logger.info("리밸런싱 전략 %d 드리프트 밴드 내 — 매매 없음", self.strategy_id)
            return

        bar_ts = f"{now.date().isoformat()}:rebal"
        await self._execute_orders(orders, prices, positions, bar_ts)

    async def _seed_history(self, universe: list[str]) -> dict[str, pd.Series]:
        """universe 각 종목의 종가 Series 를 시드한다(부족하면 외부 적재)."""
        lookback = int(self._cfg.get("selection", {}).get("lookback", 120))
        need = lookback + 1
        end = datetime.now()
        start = end - pd.Timedelta(days=need * 3)  # 거래일 고려 여유

        history: dict[str, pd.Series] = {}
        async with AsyncSessionLocal() as db:
            for sym in universe:
                series = await get_close_series(db, sym, start, end)
                if len(series) < need:
                    try:
                        df = await asyncio.to_thread(
                            load_ohlcv, sym, start.date(), end.date()
                        )
                        await upsert_price_ticks(db, sym, df)
                        series = await get_close_series(db, sym, start, end)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("%s 리밸런싱 시드 적재 실패: %s", sym, e)
                history[sym] = series.astype(float)
        return history

    async def _holdings(self, db, universe: list[str]) -> dict[str, Decimal]:
        """universe 종목의 보유 포지션을 dict(symbol→qty) 로 반환(수량>0).

        universe 밖의 보유(다른 전략 포지션 등)는 건드리지 않도록 제외한다.
        선정에서 빠진 universe 종목은 목표 0 으로 평가되어 자연히 청산 대상이 된다.
        """
        if not universe:
            return {}
        rows = await db.scalars(
            select(Position).where(
                Position.user_id == self._user_id,
                Position.qty > 0,
                Position.symbol.in_(universe),
            )
        )
        return {p.symbol: p.qty for p in rows}

    async def _quotes(self, symbols: set[str]) -> dict[str, Decimal]:
        """대상 종목들의 현재가를 REST 로 조회한다(실패 종목은 제외)."""
        prices: dict[str, Decimal] = {}
        for sym in symbols:
            try:
                quote = await self._broker.get_quote(sym)
                prices[sym] = quote.price
            except Exception as e:  # noqa: BLE001
                logger.warning("%s 현재가 조회 실패 — 이번 리밸런싱에서 제외: %s", sym, e)
        return prices

    async def _execute_orders(
        self,
        orders: list[tuple[str, str, int]],
        prices: dict[str, Decimal],
        positions: dict[str, Decimal],
        bar_ts: str,
    ) -> None:
        """매도 우선 정렬된 주문 목록을 순차 실행한다(매수는 리스크 검증 후)."""
        current_prices = {s: p for s, p in prices.items()}
        async with AsyncSessionLocal() as db:
            # 매수 신규 진입 차단: 일일 손실 한도 초과 시.
            daily = await risk.check_daily_loss_limit(
                db, self._user_id, self.strategy_id, current_prices
            )
            buys_allowed = daily.approved
            if not buys_allowed:
                logger.info("리밸런싱 매수 차단: %s", daily.reason)

        for sym, side, qty in orders:
            price = prices.get(sym)
            if price is None or qty <= 0:
                continue
            async with _position_lock(self.redis, self._user_id, sym) as acquired:
                if not acquired:
                    logger.info("포지션 락 경합 — %s 이번 리밸런싱 건너뜀", sym)
                    continue
                async with AsyncSessionLocal() as db:
                    if side == "sell":
                        held = await self._holding_qty(db, sym)
                        sell_qty = min(int(qty), int(held))
                        if sell_qty <= 0:
                            continue
                        await self._place(db, sym, "sell", sell_qty, price, bar_ts)
                    else:  # buy
                        if not buys_allowed:
                            continue
                        # max_position 한도 존중: evaluate_buy 가 남은 한도로 수량을 캡한다.
                        decision = await risk.evaluate_buy(
                            db, self._user_id, self.strategy_id, sym, price,
                            Decimal(qty) * price,
                        )
                        if not decision.approved:
                            logger.info("리밸런싱 매수 보류 %s: %s", sym, decision.reason)
                            continue
                        await self._place(db, sym, "buy", decision.qty, price, bar_ts)

    async def _holding_qty(self, db, symbol: str) -> Decimal:
        pos = await db.scalar(
            select(Position).where(
                Position.user_id == self._user_id, Position.symbol == symbol
            )
        )
        return pos.qty if pos else Decimal("0")

    async def _place(self, db, symbol: str, side: str, qty: int, price: Decimal, bar_ts: str) -> None:
        await execute_signal(
            db, self.redis, self._broker,
            user_id=self._user_id, strategy_id=self.strategy_id,
            symbol=symbol, side=side, qty=qty, price=price,
            idempotency_key=make_idempotency_key(self.strategy_id, symbol, side, bar_ts),
        )
