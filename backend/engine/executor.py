"""주문 실행기 — 멱등성 보장 + KIS 주문 + 주문/체결/포지션 DB 기록 + 이벤트 발행.

멱등성은 3중으로 보장한다:
  1) idempotency_key 결정적 생성(전략·종목·side·신호봉시각)
  2) Redis 분산 락(SET NX)으로 동시 중복 차단
  3) orders.idempotency_key UNIQUE 제약(최종 방어선, IntegrityError 흡수)

체결 처리: 모의투자 검증 단계에서는 시장가 주문 접수를 즉시 체결로 간주해
Execution·Position 을 갱신한다(정밀 체결 통보 연동은 5단계).

주문 전송(place_order) 결과는 **세 갈래**이며 상태 전이가 각각 다르다.
  1) 성공 → SUBMITTED(주문번호 기록) 후 체결 조회.
  2) 증권사가 명시적으로 거부(BrokerError: HTTP 오류·rt_cd≠0) → REJECTED.
     증권사까지 요청이 닿아 "접수되지 않음"이 확인된 경우이므로 실패로 확정해도 안전하다.
  3) 그 밖의 예외(네트워크 타임아웃·연결 끊김·JSON 파싱 실패 등) → **접수 여부 불명**.
     브로커 클라이언트는 httpx 예외를 BrokerError 로 감싸지 않으므로(app/services/kis/
     client.py::_request_json) 이 예외들은 원형 그대로 올라온다. 요청이 증권사에 닿아
     체결됐을 수도 있어 REJECTED 로 확정하면 실제 보유와 장부가 어긋난다. 그래서
     PENDING(주문번호 없음) 그대로 두고 critical 알림을 올린 뒤 예외를 재전파한다.
     회수는 engine/reconcile.py 의 자가치유 경로가 맡는다(주문번호 없는 PENDING 을
     유예시간 후 잔고로 교차확인). 멱등성 키가 이미 이 행에 걸려 있어 다음 틱에
     같은 신호로 재주문되지 않으므로, 이중 주문 위험 없이 대기시킬 수 있다.

같은 이유로 틱 타임아웃(base_runner 의 asyncio.wait_for) 취소도 PENDING 을 남긴다
(CancelledError 는 BaseException 이라 여기서 잡지 않는다) — 회수 경로는 3)과 동일하다.
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
            order.status = OrderStatus.REJECTED
            await db.commit()
            logger.warning("주문 거부됨 %s: %s", idempotency_key, e)
            await _publish(redis, {"type": "order", "user_id": user_id,
                                   "order_id": order.id, "status": "rejected", "symbol": symbol})
            return order
        except Exception as e:  # noqa: BLE001 — 접수 여부 불명(네트워크 타임아웃 등)
            # 증권사에 요청이 닿아 체결됐을 가능성이 남아 있으므로 REJECTED 로 확정하지
            # 않는다(모듈 docstring 3). PENDING·주문번호 없음 그대로 두어 reconcile 이
            # 잔고로 교차확인하게 하고, 조용한 장부 어긋남이 되지 않도록 즉시 알린다.
            logger.exception("주문 접수 결과 불명 %s (%s %s %d주)", idempotency_key, side, symbol, qty)
            try:
                await publish_alert(
                    redis,
                    user_id=user_id,
                    strategy_id=strategy_id,
                    severity="critical",
                    code="order_unconfirmed",
                    message=(
                        f"주문 접수 결과 불명 — {symbol} {side} {qty:,}주(주문 #{order.id}). "
                        f"증권사 응답을 받지 못해 접수·체결 여부를 알 수 없습니다. "
                        f"실제 체결됐을 수 있어 재주문하지 않고 대기시킵니다"
                        f"(정합 루프가 잔고로 교차확인). 원인: {e!r}"
                    ),
                )
            except Exception:  # noqa: BLE001 — 알림 실패가 원인 예외를 가리면 안 된다.
                logger.warning("접수 불명 알림 발행 실패(무시) %s", idempotency_key, exc_info=True)
            raise

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
