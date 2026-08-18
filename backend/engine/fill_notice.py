"""KIS 실시간 체결통보(H0STCNI0/H0STCNI9) 리스너 — 사용자(계좌) 단위 1구독.

체결가 WS(`engine/kis_ws.py`, `engine/price_feed.py`)와 달리 체결통보는 **종목이
아니라 사용자 계좌 전체**에 대해 한 번만 구독한다(tr_key=HTS ID). 따라서
`PriceFeedManager`(종목 집합 변경 시 재구독)가 아니라 사용자 존재 여부만 보는
구조를 쓰되, 감독 루프(지수 백오프 재연결)는 동일 패턴을 그대로 따른다.

체결 반영은 executor(주문 직후 1회)·reconcile(주기적 재조회)과 함께 **3중 경로**로
동시에 존재한다. 이 모듈도 반드시 `engine/reconcile.py._order_recorded_qty` 와
동일한 "이미 기록된 누적 체결수량 대비 델타만 반영" 방식을 써서 중복 기록을 막는다.

다만 델타 방식만으로는 부족하다 — "이미 기록된 수량 읽기 → record_fill → commit" 은
read-then-write 구간이라, WS 통보 처리와 reconcile 스윕(60초 주기)이 겹치면 둘 다
already=0 을 읽고 각자 같은 체결을 기록해 이중 가산될 수 있다(매수는 포지션 과다,
매도는 `engine/fills.py` 의 오버셀 클램프 오작동). 그래서 이 구간을 reconcile 과
**같은 주문 단위 분산 락 키**로 감싼다(아래 apply_fill_notice 참고).

── 중요한 미검증 가정(실계정으로 확인 전까지의 설계 결정) ──────────────────────
1) KIS 체결통보 프레임의 체결수량(CNTG_QTY) 필드를 "이 주문의 누적 체결수량"으로
   간주해 `_order_recorded_qty` 델타 방식을 적용한다. 만약 실계정에서 이 필드가
   "이번 이벤트에서 새로 체결된 증분수량"으로 밝혀지면(실무적으로 흔한 해석),
   이 델타 로직은 잘못 동작할 수 있다 — 반드시 `docs/live-order-guide.md` 의
   실계정 전환 체크리스트에서 로그로 실제 값을 확인해야 한다.
2) KIS 실시간 체결통보 표준 필드셋에는 주문조직번호(ORGNO)가 노출되지 않는 것으로
   보인다(REST 주문 응답의 KRX_FWDG_ORD_ORGNO 와 달리). 따라서 이 모듈은
   `Order.kis_order_id`(ODNO)만으로 매칭하고, `Order.kis_order_org_no` 는 있으면
   추가 AND 조건으로 걸되(향후 KIS 가 필드를 추가하는 경우 대비), 파싱 결과에
   ORGNO 가 없으면 이 조건은 적용되지 않는다.
3) 필드 인덱스(_parse_fill_notice)는 KIS 공식 샘플(국내주식 실시간체결통보) 순서를
   따른 것으로, 실계정 프레임으로 종단 검증되지 않았다.

AES 복호화는 `cryptography`(이미 프로젝트 의존성)의 저수준 AES-CBC + PKCS7 로
구현한다(KIS 공식 예제는 pycryptodome 을 쓰지만 동일 알고리즘이라 대체 가능하며,
신규 의존성 추가를 피한다).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import websockets
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import select

from app.core.channels import RECONCILE_LOCK_TTL, reconcile_lock_key
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import Order
from engine.fills import publish_event, record_fill
from engine.kis_ws import issue_approval_key
from engine.reconcile import _order_recorded_qty

logger = logging.getLogger("engine.fill_notice")

# KIS 국내주식 실시간체결통보 필드 순서(^ 구분, 복호화된 평문). 공식 샘플 기준 23개.
_IDX_ODNO = 2            # 주문번호(ODNO)
_IDX_SELN_BUY_DVSN = 4   # 매도매수구분: 01매도, 02매수
_IDX_SYMBOL = 8          # 주식단축종목코드
_IDX_QTY = 9             # 체결수량(CNTG_QTY) — 이 주문의 누적 체결수량으로 간주(위 가정 1 참고)
_IDX_PRICE = 10          # 체결단가(CNTG_UNPR)
_MIN_FIELDS = 23


@dataclass
class FillNoticeData:
    """복호화·파싱된 체결통보 1건."""

    odno: str
    symbol: str
    qty: int
    price: Decimal
    side: str  # "buy" | "sell" — 참고용(매칭에는 사용하지 않음)


def aes_cbc_base64_decrypt(key: str, iv: str, cipher_b64: str) -> str:
    """KIS 체결통보 AES-256-CBC(+PKCS7) 복호화. key/iv 는 구독 응답에서 받은 문자열."""
    raw = base64.b64decode(cipher_b64)
    cipher = Cipher(algorithms.AES(key.encode("utf-8")), modes.CBC(iv.encode("utf-8")))
    decryptor = cipher.decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(algorithms.AES.block_size).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode("utf-8")


def parse_fill_notice(plain: str) -> FillNoticeData | None:
    """복호화된 평문(^ 구분)에서 체결통보 필드를 추출한다.

    체결수량이 0(접수/거부/취소 통보)이면 반영할 체결이 없으므로 None 을 반환한다.
    """
    fields = plain.split("^")
    if len(fields) < _MIN_FIELDS:
        logger.warning("체결통보 필드 수 부족(%d) — 무시", len(fields))
        return None
    odno = fields[_IDX_ODNO].strip()
    symbol = fields[_IDX_SYMBOL].strip()
    if not odno or not symbol:
        return None
    try:
        qty = int(fields[_IDX_QTY] or 0)
    except ValueError:
        qty = 0
    if qty <= 0:
        return None  # 접수/거부/정정/취소 통보 — 체결 없음
    try:
        price = Decimal(fields[_IDX_PRICE] or "0")
    except (InvalidOperation, ValueError):
        price = Decimal("0")
    side = "sell" if fields[_IDX_SELN_BUY_DVSN] == "01" else "buy"
    return FillNoticeData(odno=odno, symbol=symbol, qty=qty, price=price, side=side)


def _subscribe_msg(approval_key: str, tr_id: str, tr_key: str, subscribe: bool = True) -> str:
    """체결통보 구독/해지 메시지(JSON 문자열). tr_id 는 모의/실전 분기, tr_key 는 HTS ID."""
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1" if subscribe else "2",
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
    })


def _fill_notice_tr_id() -> str:
    """모의투자/실전에 따른 체결통보 tr_id."""
    return "H0STCNI9" if settings.is_paper_trading else "H0STCNI0"


async def apply_fill_notice(user_id: int, notice: FillNoticeData, redis) -> bool:
    """파싱된 체결통보 1건을 델타 멱등으로 반영한다. 반영했으면 True, 스킵/실패면 False.

    reconcile.py 와 동일하게 "이미 기록된 누적 체결수량" 대비 델타만 record_fill 에
    넘겨 executor 즉시체결·reconcile 폴링과 중복 기록되지 않게 한다.

    델타 계산(read)과 기록(write) 사이에 reconcile 스윕이 끼어들면 둘 다 같은
    `already` 를 읽어 같은 체결을 두 번 기록하므로, 그 구간을 reconcile 과 **동일한**
    주문 단위 락(`app.core.channels.reconcile_lock_key`)으로 직렬화한다. 키를
    공유하는 것이 이 방어의 핵심이다 — 접두어가 다르면 서로를 배제하지 못해 락이
    무의미해진다. (`app.core.channels.ORDER_LOCK_PREFIX` 는 order.id 가 아니라
    idempotency_key 로 스코프된 executor 전용 키라 여기서는 쓸 수 없다.)

    락 획득 실패 시 reconcile.py·executor.py 와 동일하게 재시도 없이 이번 통보를
    스킵한다(중복 기록보다 스킵이 안전). 다만 **모의투자 매도** 통보를 스킵하면
    자동 복구되지 않는다 — reconcile 의 잔고 폴백은 매수 전용이고(reconcile.py 의
    `order.side != OrderSide.BUY` 조기 반환), 모의투자 일별체결조회는 늘 비어 있기
    때문이다. 그래서 debug 가 아니라 warning 으로 남긴다.
    """
    async with AsyncSessionLocal() as db:
        order = await db.scalar(
            select(Order).where(Order.user_id == user_id, Order.kis_order_id == notice.odno)
        )
        if order is None:
            logger.warning(
                "체결통보 매칭 실패(주문 없음) user=%d odno=%s symbol=%s",
                user_id, notice.odno, notice.symbol,
            )
            return False

        # ── 여기부터 commit 까지가 read-then-write 임계구간 ──
        lock_key = reconcile_lock_key(order.id)
        if not await redis.set(lock_key, "1", nx=True, ex=RECONCILE_LOCK_TTL):
            logger.warning(
                "체결통보 스킵(다른 프로세스가 같은 주문 정합 중) order=%s odno=%s 누적통보=%d",
                order.id, notice.odno, notice.qty,
            )
            return False

        try:
            # `already` 는 반드시 락 획득 이후에 읽는다(락 이전 값은 경쟁 상대의 커밋을
            # 못 본 stale 값일 수 있다). Postgres READ COMMITTED 는 문장마다 새 스냅샷을
            # 잡으므로, 이 시점 조회는 상대가 커밋한 executions 를 포함한다.
            already = await _order_recorded_qty(db, order.id)
            target = int(order.qty)
            delta = notice.qty - already
            if delta <= 0:
                logger.debug(
                    "체결통보 중복/이미 반영 — 스킵 odno=%s 누적통보=%d 기록됨=%d",
                    notice.odno, notice.qty, already,
                )
                return False

            fully = (already + delta) >= target
            price = notice.price if notice.price and notice.price > 0 else order.price
            await record_fill(db, order, delta, price, fully_filled=fully, redis=redis)
            await db.commit()
        finally:
            # 중복 통보(delta<=0)가 흔한 경로라, 여기서 놓치면 TTL 30초 동안 키가 남아
            # 다음 reconcile 스윕이 이 주문을 통째로 건너뛴다. 반드시 finally 로 푼다.
            await redis.delete(lock_key)

        await publish_event(redis, {
            "type": "execution", "user_id": user_id, "order_id": order.id,
            "symbol": order.symbol, "side": str(order.side), "qty": delta,
            "price": float(price), "status": str(order.status),
            "kis_order_id": order.kis_order_id, "source": "fill_notice",
        })
        logger.info(
            "체결통보 반영: 주문=%s +%d주 @ %s → %s", order.kis_order_id, delta, price, order.status,
        )
        return True


class FillNoticeManager:
    """사용자별 KIS 체결통보 WS 를 관리한다(사용자당 1개, 계좌 전체 스트림).

    `KIS_HTS_ID` 가 설정되지 않으면 안전하게 아무 것도 구독하지 않는다(no-op).
    """

    def __init__(self, redis):
        self.redis = redis
        self._feeds: dict[int, dict] = {}  # user_id -> {task, stop}

    def active_users(self) -> set[int]:
        """현재 구독 중인 사용자 id 집합(엔진의 재동기화 루프가 diff 계산에 사용)."""
        return set(self._feeds.keys())

    async def ensure(self, user_id: int, app_key: str, app_secret: str) -> None:
        """사용자의 체결통보 구독을 시작한다(이미 있으면 무시). HTS ID 미설정이면 no-op."""
        if not settings.KIS_HTS_ID:
            logger.info(
                "KIS_HTS_ID 미설정 — 체결통보 리스너 비활성(user=%d). 실계정 전환 전 설정 필요.",
                user_id,
            )
            return
        if user_id in self._feeds:
            return
        stop = asyncio.Event()
        task = asyncio.create_task(self._supervise(user_id, app_key, app_secret, stop))
        self._feeds[user_id] = {"task": task, "stop": stop}
        logger.info("체결통보 리스너 시작 user=%d", user_id)

    async def remove(self, user_id: int) -> None:
        """사용자의 체결통보 구독을 중지·정리한다(없으면 무시)."""
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
            logger.exception("체결통보 리스너 종료 중 오류 user=%d", user_id)

    async def shutdown(self) -> None:
        """모든 사용자 구독을 정리한다(엔진 종료 시)."""
        for uid in list(self._feeds):
            await self.remove(uid)

    async def _supervise(
        self, user_id: int, app_key: str, app_secret: str, stop: asyncio.Event
    ) -> None:
        """WS 연결을 감독하며 끊기면 재연결한다(지수 백오프, price_feed.py 와 동일 패턴)."""
        backoff = 5
        max_backoff = 120
        while not stop.is_set():
            try:
                await self._run(user_id, app_key, app_secret, stop)
                wait = backoff if stop.is_set() else 3
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "체결통보 WS 오류 user=%d, %ds 후 재연결: %s", user_id, backoff, e,
                )
                wait = backoff
                backoff = min(backoff * 2, max_backoff)
            else:
                backoff = 5
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def _run(
        self, user_id: int, app_key: str, app_secret: str, stop: asyncio.Event
    ) -> None:
        """WS 연결 → 구독 → 수신 루프. stop 이 set 되면 종료."""
        approval_key = await issue_approval_key(app_key, app_secret)
        tr_id = _fill_notice_tr_id()

        async with websockets.connect(
            settings.kis_ws_url, ping_interval=None, max_size=None
        ) as ws:
            await ws.send(_subscribe_msg(approval_key, tr_id, settings.KIS_HTS_ID))
            logger.info(
                "체결통보 구독 user=%d tr_id=%s (env=%s)", user_id, tr_id, settings.KIS_ENV,
            )
            key: str | None = None
            iv: str | None = None

            while not stop.is_set():
                try:
                    raw = await ws.recv()
                except websockets.ConnectionClosed:
                    logger.warning("체결통보 WS 연결 종료 user=%d", user_id)
                    break

                if raw.startswith("{"):
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    header = msg.get("header", {})
                    if header.get("tr_id") == "PINGPONG":
                        await ws.pong(raw)
                        continue
                    output = msg.get("body", {}).get("output", {})
                    if output.get("key") and output.get("iv"):
                        key, iv = output["key"], output["iv"]
                        logger.debug("체결통보 key/iv 수신 user=%d", user_id)
                    continue

                if key is None or iv is None:
                    logger.warning("체결통보 key/iv 미수신 — 데이터 프레임 무시 user=%d", user_id)
                    continue

                parts = raw.split("|")
                if len(parts) < 4 or parts[1] != tr_id:
                    continue
                try:
                    plain = aes_cbc_base64_decrypt(key, iv, parts[3])
                except Exception:  # noqa: BLE001
                    logger.exception("체결통보 복호화 실패 user=%d", user_id)
                    continue

                notice = parse_fill_notice(plain)
                if notice is None:
                    continue
                try:
                    await apply_fill_notice(user_id, notice, self.redis)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "체결통보 반영 실패 user=%d odno=%s", user_id, notice.odno,
                    )
