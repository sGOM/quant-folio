"""전략 실행기 — 단일 live 전략을 구동.

흐름: 과거 일봉 시드 → 주기적으로 현재가 반영 → 신호 평가(손절 우선) → 주문.
현재가는 PriceFeed(WS)가 채운 Redis 캐시를 우선 사용하고, 없으면 REST 폴백.
신호는 백테스트와 동일한 signals 모듈을 써서 일관성을 유지한다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal

import contextlib
import pandas as pd
from redis.asyncio import Redis
from sqlalchemy import select

from app.core.channels import position_lock_key
from app.core.database import AsyncSessionLocal
from app.models import Position, Strategy, User
from app.services.backtest.signals import latest_signal, min_periods, requires_ohlc
from app.services.broker import BrokerClient, make_broker_for_user, user_has_credentials
from app.services.data.loader import (
    get_close_series,
    get_ohlcv_frame,
    load_ohlcv,
    upsert_price_ticks,
)
from app.services.market import is_market_open
from engine import risk
from engine.executor import execute_signal, make_idempotency_key

logger = logging.getLogger("engine.runner")

_POLL_INTERVAL = 30  # 초
_PRICE_CACHE_PREFIX = "price:"
_POSITION_LOCK_TTL = 30  # 초
_TRAIL_PREFIX = "trail:"  # 트레일링 스탑 고점(high-water-mark) 캐시
_TRAIL_TTL = 60 * 60 * 24 * 7  # 7일(휴장 등 대비 여유)


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


class StrategyRunner:
    """단일 live 전략을 주기적으로 평가·주문하는 실행기.

    :param strategy_id: 구동할 전략 ID
    :param redis: 가격 캐시·분산 락·이벤트 발행에 쓰는 Redis 클라이언트
    """

    def __init__(self, strategy_id: int, redis: Redis):
        self.strategy_id = strategy_id
        self.redis = redis
        # close-only 전략은 종가 Series, OHLC 전략은 OHLCV DataFrame 을 시드한다.
        self._series: pd.Series | pd.DataFrame | None = None
        self._ohlc: bool = False
        self._cfg: dict = {}
        self._user_id: int | None = None
        self._symbol: str = ""
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
            self._symbol = self._cfg["symbol"]
            self._ohlc = requires_ohlc(self._cfg)
            self._broker = make_broker_for_user(user)
            await self._seed_series(db)
        return True

    async def _seed_series(self, db) -> None:
        """지표 계산용 과거 일봉 시드. price_ticks 없으면 적재.

        OHLC 전략이면 OHLCV DataFrame 을, close-only 전략이면 종가 Series 를 시드한다.
        """
        need_bars = min_periods(self._cfg)
        need = need_bars * 4
        end = datetime.now()
        start = end - pd.Timedelta(days=need * 2)  # 거래일 고려 여유

        async def _fetch():
            if self._ohlc:
                return await get_ohlcv_frame(db, self._symbol, start, end)
            return await get_close_series(db, self._symbol, start, end)

        series = await _fetch()
        if len(series) < need_bars + 1:
            try:
                df = await asyncio.to_thread(
                    load_ohlcv, self._symbol, start.date(), end.date()
                )
                await upsert_price_ticks(db, self._symbol, df)
                series = await _fetch()
            except Exception as e:  # noqa: BLE001
                logger.warning("%s 시드 적재 실패: %s", self._symbol, e)
        self._series = series.astype(float)

    async def _current_price(self) -> Decimal | None:
        """현재가를 조회한다. PriceFeed 가 채운 Redis 캐시를 우선 쓰고, 없으면 REST 폴백.

        :return: 현재가(Decimal), 조회 실패 시 None
        """
        cached = await self.redis.get(f"{_PRICE_CACHE_PREFIX}{self._symbol}")
        if cached:
            try:
                return Decimal(cached)
            except Exception:  # noqa: BLE001
                pass
        try:
            quote = await self._broker.get_quote(self._symbol)
            return quote.price
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 현재가 조회 실패: %s", self._symbol, e)
            return None

    async def _holding_qty(self, db) -> Decimal:
        """현재 보유 수량을 반환한다(포지션 없으면 0)."""
        pos = await db.scalar(
            select(Position).where(
                Position.user_id == self._user_id, Position.symbol == self._symbol
            )
        )
        return pos.qty if pos else Decimal("0")

    async def run(self, stop_event: asyncio.Event) -> None:
        """전략을 적재한 뒤 stop_event 가 설정될 때까지 _POLL_INTERVAL 마다 평가·주문한다.

        :param stop_event: 외부에서 중지를 요청하는 이벤트
        """
        if not await self._load():
            return
        logger.info("전략 %d 실행 시작 (%s)", self.strategy_id, self._symbol)

        while not stop_event.is_set():
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("전략 %d tick 오류", self.strategy_id)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

        logger.info("전략 %d 실행 종료", self.strategy_id)

    async def _tick(self) -> None:
        # 장 운영시간이 아니면 신호 평가·주문을 건너뛴다(휴장일·시간외 보호).
        if not is_market_open():
            return
        price = await self._current_price()
        if price is None or self._series is None:
            return

        # 오늘 봉을 현재가로 갱신해 최신 신호를 평가한다.
        today = pd.Timestamp(date.today())
        series = self._series.copy()
        px = float(price)
        if self._ohlc:
            # OHLC 전략: 오늘 봉의 OHLC 를 구성한다. 기존 오늘 행이 있으면
            # high/low 를 현재가로 갱신하고, 없으면 현재가로 시·고·저·종을 채운다.
            # (러너는 일중 고저를 추적하지 않으므로 보수적 근사 — 신호는 종가 기준이
            #  핵심이고 ATR/채널은 과거 봉 비중이 크다.)
            if today in series.index:
                row = series.loc[today]
                high = max(float(row.get("high", px)), px)
                low = min(float(row.get("low", px)), px)
                open_ = float(row.get("open", px))
            else:
                high = low = open_ = px
            series.loc[today] = {
                "open": open_, "high": high, "low": low, "close": px,
                "volume": float(series.loc[today]["volume"]) if today in series.index else 0.0,
            }
        else:
            series.loc[today] = px
        series = series.sort_index()

        sig = latest_signal(series, self._cfg)
        bar_ts = today.date().isoformat()

        # 포지션 읽기-판단-주문을 (user, symbol) 락으로 직렬화해 이중 매수/매도 방지.
        async with _position_lock(self.redis, self._user_id, self._symbol) as acquired:
            if not acquired:
                logger.info("포지션 락 경합 — 이번 tick 건너뜀 (%s)", self._symbol)
                return

            async with AsyncSessionLocal() as db:
                held = await self._holding_qty(db)

                # 1) 리스크 한도(RiskLimit) 기반 손절 우선
                if held > 0 and await risk.check_stop_loss(
                    db, self._user_id, self.strategy_id, self._symbol, price
                ):
                    await self._clear_trail()
                    await self._do_sell(db, price, bar_ts + ":stop")
                    return

                # 2) 전략 config 기반 청산(손절%/익절%/트레일링) 평가
                if held > 0:
                    exit_kind = await self._config_exit(db, price)
                    if exit_kind is not None:
                        await self._clear_trail()
                        await self._do_sell(db, price, f"{bar_ts}:{exit_kind}")
                        return
                else:
                    # 보유 없음 → 트레일링 고점 캐시 정리(잔여 키 방지).
                    await self._clear_trail()

                if sig == "buy" and held <= 0:
                    # 일일 손실 한도 초과 시 신규 진입 차단.
                    daily = await risk.check_daily_loss_limit(
                        db, self._user_id, self.strategy_id, {self._symbol: price}
                    )
                    if not daily.approved:
                        logger.info("매수 차단: %s", daily.reason)
                        return
                    decision = await risk.evaluate_buy(
                        db, self._user_id, self.strategy_id, self._symbol, price,
                        Decimal(str(self._cfg.get("cash", 10_000_000))),
                    )
                    if decision.approved:
                        await execute_signal(
                            db, self.redis, self._broker,
                            user_id=self._user_id, strategy_id=self.strategy_id,
                            symbol=self._symbol, side="buy", qty=decision.qty, price=price,
                            idempotency_key=make_idempotency_key(
                                self.strategy_id, self._symbol, "buy", bar_ts),
                        )
                    else:
                        logger.info("매수 보류: %s", decision.reason)
                elif sig == "sell" and held > 0:
                    await self._clear_trail()
                    await self._do_sell(db, price, bar_ts)

    def _trail_key(self) -> str:
        """트레일링 스탑 고점 캐시 키 — (strategy, symbol) 단위."""
        return f"{_TRAIL_PREFIX}{self.strategy_id}:{self._symbol}"

    async def _clear_trail(self) -> None:
        """트레일링 고점 캐시를 제거한다(포지션 종료·청산 시)."""
        await self.redis.delete(self._trail_key())

    async def _config_exit(self, db, price: Decimal) -> str | None:
        """전략 config 의 손절%/익절%/트레일링 청산 조건을 평가한다.

        보유 포지션의 평균단가(avg_price) 대비 현재가로 손절·익절을, 보유 중 고점
        대비 하락률로 트레일링을 판정한다. 고점은 Redis 에 보관한다.

        :return: 청산 사유 'sl'|'tp'|'trail', 해당 없으면 None
        """
        pos = await db.scalar(
            select(Position).where(
                Position.user_id == self._user_id, Position.symbol == self._symbol
            )
        )
        if pos is None or pos.qty <= 0 or pos.avg_price <= 0:
            return None

        avg = Decimal(str(pos.avg_price))
        sl = self._cfg.get("stop_loss_pct")
        tp = self._cfg.get("take_profit_pct")
        trail = self._cfg.get("trailing_stop_pct")

        # 익절: 평균단가 대비 +tp 이상 상승
        if tp is not None and (price - avg) / avg >= Decimal(str(tp)):
            logger.info("익절 도달: %s +%.4f", self._symbol, float((price - avg) / avg))
            return "tp"
        # 손절: 평균단가 대비 -sl 이상 하락
        if sl is not None and (avg - price) / avg >= Decimal(str(sl)):
            logger.info("손절 도달: %s -%.4f", self._symbol, float((avg - price) / avg))
            return "sl"
        # 트레일링: 보유 중 고점 대비 -trail 이상 하락
        if trail is not None:
            key = self._trail_key()
            cached = await self.redis.get(key)
            peak = Decimal(cached) if cached else avg
            if price > peak:
                peak = price
            await self.redis.set(key, str(peak), ex=_TRAIL_TTL)
            if peak > 0 and (peak - price) / peak >= Decimal(str(trail)):
                logger.info(
                    "트레일링 스탑 도달: %s 고점 %s 대비 -%.4f",
                    self._symbol, peak, float((peak - price) / peak),
                )
                return "trail"
        return None

    async def _do_sell(self, db, price: Decimal, bar_ts: str) -> None:
        """보유 수량 전량 매도를 실행한다(리스크 평가 후).

        :param price: 신호 시점가(체결 기록은 executor 가 실제 체결가로 보정)
        :param bar_ts: 멱등성 키 구성용 신호봉 식별자(손절은 ":stop" 접미)
        """
        decision = await risk.evaluate_sell(db, self._user_id, self._symbol)
        if not decision.approved:
            return
        await execute_signal(
            db, self.redis, self._broker,
            user_id=self._user_id, strategy_id=self.strategy_id,
            symbol=self._symbol, side="sell", qty=decision.qty, price=price,
            idempotency_key=make_idempotency_key(
                self.strategy_id, self._symbol, "sell", bar_ts),
        )
