"""주문 실행기 — 멱등성 보장 + KIS 주문 + 주문/체결/포지션 DB 기록 + 이벤트 발행.

멱등성은 3중으로 보장한다:
  1) idempotency_key 결정적 생성(전략·종목·side·신호봉시각)
  2) Redis 분산 락(SET NX)으로 동시 중복 차단
  3) orders.idempotency_key UNIQUE 제약(최종 방어선, IntegrityError 흡수)

체결 처리: 모의투자 검증 단계에서는 시장가 주문 접수를 즉시 체결로 간주해
Execution·Position 을 갱신한다(정밀 체결 통보 연동은 5단계).
"""
from __future__ import annotations

import logging
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channels import ORDER_LOCK_PREFIX
from app.models import (
    Order,
    OrderSide,
    OrderStatus,
)
from app.services.broker import BrokerClient, BrokerError
from engine.alerts import publish_alert
from engine.fills import publish_event as _publish
from engine.fills import record_fill as _record_fill

logger = logging.getLogger("engine.executor")

_LOCK_TTL = 30  # 초


def make_idempotency_key(strategy_id: int, symbol: str, side: str, bar_ts: str) -> str:
    """결정적 멱등성 키. 같은 신호봉의 같은 주문은 항상 동일 키."""
    return f"s{strategy_id}:{symbol}:{side}:{bar_ts}"


async def execute_signal(
    db: AsyncSession,
    redis: Redis,
    broker: BrokerClient,
    *,
    user_id: int,
    strategy_id: int,
    symbol: str,
    side: str,
    qty: int,
    price: Decimal,
    idempotency_key: str,
    reason: str | None = None,
) -> Order | None:
    """신호에 따른 주문 실행. 중복이면 None 반환.

    :param reason: 감사 로그용 주문 사유(어떤 신호·공식·리스크·리밸런싱 기준으로
        이 주문이 나갔는지 사람이 읽을 수 있는 한국어 설명). Order.reason 에 기록된다.
    """
    # 1) Redis 분산 락
    lock_key = f"{ORDER_LOCK_PREFIX}{idempotency_key}"
    got_lock = await redis.set(lock_key, "1", nx=True, ex=_LOCK_TTL)
    if not got_lock:
        logger.info("중복 주문 차단(락 보유 중): %s", idempotency_key)
        return None

    try:
        # 2) DB 기존 주문 확인
        existing = await db.scalar(
            select(Order).where(Order.idempotency_key == idempotency_key)
        )
        if existing is not None:
            logger.info("중복 주문 차단(DB 존재): %s", idempotency_key)
            return None

        # 3) 주문 레코드 생성(pending)
        order = Order(
            user_id=user_id,
            strategy_id=strategy_id,
            symbol=symbol,
            side=OrderSide(side),
            qty=Decimal(qty),
            price=price,
            order_type="market",
            status=OrderStatus.PENDING,
            idempotency_key=idempotency_key,
            reason=reason,
        )
        db.add(order)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.info("중복 주문 차단(UNIQUE 충돌): %s", idempotency_key)
            return None
        await db.refresh(order)

        # 4) 증권사 주문 (시장가)
        try:
            res = await broker.place_order(symbol, side, qty, price=0, order_type="market")
            order.kis_order_id = res.order_id
            order.kis_order_org_no = res.order_org_no
            order.status = OrderStatus.SUBMITTED
        except BrokerError as e:
            # 브로커가 명시적으로 주문을 거부했다(HTTP 오류 status·rt_cd 오류 등 응답을
            # 정상 수신했고 그 응답이 실패). 주문이 접수되지 않았음이 확정이므로 REJECTED.
            order.status = OrderStatus.REJECTED
            await db.commit()
            logger.warning("주문 거부됨 %s: %s", idempotency_key, e)
            await _publish(redis, {"type": "order", "user_id": user_id,
                                   "order_id": order.id, "status": "rejected", "symbol": symbol})
            return order
        except Exception as e:  # noqa: BLE001
            # BrokerError 가 아닌 예외(httpx.TimeoutException·ConnectError·HTTPError,
            # json.JSONDecodeError 등 네트워크/파싱 레벨)는 "브로커가 거부했다"는 확정
            # 신호가 아니다. 요청은 브로커에 도달·처리됐는데 응답 수신 단계에서만 끊긴
            # 흔한 실패 패턴이라, 실제로는 주문이 접수·체결됐을 수 있다.
            #
            # 이때 REJECTED 로 확정하면(포지션 없음으로 오판) 실제 체결분이 무관리
            # 상태로 남고 다음 신호에서 중복 매수돼 계좌가 과다 노출될 수 있다. 반대로
            # 주문이 실제로 실패했더라도 SUBMITTED 로 남기면 그 신호봉 한 건을 놓칠 뿐
            # (idempotency_key 로 같은 봉 재주문은 차단됨) 팬텀 포지션은 생기지 않는다.
            # 두 오류 중 후자가 훨씬 안전하므로 보수적으로 SUBMITTED(주문 여부 불확정)로
            # 남긴다. kis_order_id 는 수신하지 못했으므로 None 그대로 둔다.
            #
            # 모의투자 BUY 는 reconcile 의 잔고 폴백이 자기수렴시키고(engine/reconcile.py),
            # 실전은 주문번호 없이 자동 정합이 불가하므로 critical 알림으로 사람이 증권사
            # 콘솔에서 직접 확인·처리하도록 유도한다("모르면 사람이 확인하게 만든다").
            order.status = OrderStatus.SUBMITTED
            await db.commit()
            logger.error(
                "주문 전송 응답 불확정 — SUBMITTED 유지, 사람 확인 필요 %s: %r",
                idempotency_key, e, exc_info=True,
            )
            await publish_alert(
                redis,
                user_id=user_id,
                strategy_id=strategy_id,
                severity="critical",
                code="order_ack_unknown",
                message=(
                    f"주문 전송 응답 불확정 — {symbol} {side} {qty:,}주. "
                    f"브로커 도달 여부 불명({type(e).__name__}). 실제 체결됐을 수 있으니 "
                    f"증권사 콘솔에서 주문·체결 여부를 직접 확인 후 처리하세요(주문번호 미수신)."
                ),
            )
            await _publish(redis, {
                "type": "order", "user_id": user_id, "order_id": order.id,
                "symbol": symbol, "side": side, "status": "submitted",
                "kis_order_id": None,
            })
            return order

        # 5) 실제 체결 조회 후 기록 + 포지션 갱신.
        #    시장가라도 실제 체결가는 신호 시점가(price)와 다르므로,
        #    증권사 체결 조회로 실제 체결수량·평균체결가를 받아 기록한다.
        fill_qty, fill_price = await _resolve_fill(broker, order, qty, price)
        if fill_qty <= 0:
            # 미체결(접수만 됨) — 포지션·체결 기록하지 않고 SUBMITTED 유지.
            await db.commit()
            await _publish(redis, {
                "type": "order", "user_id": user_id, "order_id": order.id,
                "symbol": symbol, "side": side, "status": "submitted",
                "kis_order_id": order.kis_order_id,
            })
            logger.info("주문 접수(미체결): %s %s %d주", side, symbol, qty)
            return order

        await _record_fill(db, order, fill_qty, fill_price, fully_filled=(fill_qty >= qty), redis=redis)
        await db.commit()

        await _publish(redis, {
            "type": "execution", "user_id": user_id, "order_id": order.id,
            "symbol": symbol, "side": side, "qty": fill_qty, "price": float(fill_price),
            # status 컬럼은 String 이라 DB 재로드(expire/refresh) 시 순수 str 로 복원돼
            # .value 가 AttributeError 를 낸다. StrEnum 은 str() 이 값 문자열을 주므로
            # enum·str 양쪽에서 안전하게 직렬화한다.
            "status": str(order.status), "kis_order_id": order.kis_order_id,
        })
        logger.info("주문 체결 기록: %s %s %d주 @ %s (실제 체결가)", side, symbol, fill_qty, fill_price)
        return order
    finally:
        await redis.delete(lock_key)


async def _resolve_fill(
    broker: BrokerClient, order: Order, qty: int, signal_price: Decimal
) -> tuple[int, Decimal]:
    """증권사 체결 조회로 실제 체결수량·평균체결가를 얻는다.

    조회 실패 시(네트워크/스키마 변경) 체결 여부를 알 수 없으므로 미체결(0)로
    보고해 주문을 SUBMITTED 로 남긴다 — 이후 reconcile 루프(engine/reconcile.py)가
    재조회해 실제 체결로 수렴시킨다. 여기서 요청 수량 전량을 체결로 간주하면
    FILLED 로 확정돼 reconcile 대상에서 빠지고, 미체결이었을 경우 포지션 장부가
    영구히 오염된다(자기 교정 불가).
    """
    if not order.kis_order_id:
        return 0, signal_price
    try:
        info = await broker.get_order_execution(order.kis_order_id, order.symbol)
    except BrokerError as e:
        logger.warning(
            "체결 조회 실패 — SUBMITTED 유지, reconcile 로 수렴 위임 %s: %s",
            order.idempotency_key, e,
        )
        return 0, signal_price

    filled = int(info.filled_qty or 0)
    avg = info.avg_price
    if filled <= 0:
        return 0, signal_price
    if avg is None or avg <= 0:
        logger.warning("체결 평균가 없음 — 신호가로 폴백 %s", order.idempotency_key)
        return filled, signal_price
    return filled, Decimal(str(avg))
