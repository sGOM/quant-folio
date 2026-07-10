"""KRX 장 운영시간/휴장일 판단.

정규장 09:00~15:30 (KST), 주말·공휴일 휴장. 공휴일은 pykrx 영업일로
best-effort 확인하고, 조회 실패 시 보수적으로 '영업일'로 간주하지 않는다
(주문을 막는 방향이 안전).
"""
from __future__ import annotations

import logging
import socket
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

logger = logging.getLogger(__name__)

# pykrx 의 get_nearest_business_day_in_a_week 는 자체 timeout 을 지정하지 않는다.
# is_business_day 는 엔진 러너(engine/runner.py, engine/rebalance_runner.py)의 코루틴
# 안에서 asyncio.to_thread 없이 "직접(동기)" 호출되므로, 응답이 없으면 이 한 호출이
# 프로세스의 이벤트 루프 전체를 멈춰 세운다(해당 전략뿐 아니라 다른 모든 태스크·
# 하트비트까지 정지) — 짧게라도 반드시 타임아웃을 걸어야 한다.
_BUSINESS_DAY_TIMEOUT = 5  # 초

KST = timezone(timedelta(hours=9))
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)


def now_kst() -> datetime:
    return datetime.now(KST)


@lru_cache(maxsize=512)
def is_business_day(d: date) -> bool:
    """영업일 여부. 주말 제외 + pykrx 휴장일 확인(가능할 때)."""
    if d.weekday() >= 5:  # 토(5)/일(6)
        return False
    prev_timeout = socket.getdefaulttimeout()
    try:
        from pykrx import stock

        socket.setdefaulttimeout(_BUSINESS_DAY_TIMEOUT)
        ymd = d.strftime("%Y%m%d")
        nearest = stock.get_nearest_business_day_in_a_week(ymd)
        return nearest == ymd
    except Exception as e:  # noqa: BLE001
        logger.debug("영업일 조회 실패(%s), 주말여부만 적용: %s", d, e)
        return True  # 주말이 아니면 일단 영업일로 (시세/주문에서 자연 차단)
    finally:
        socket.setdefaulttimeout(prev_timeout)


def is_market_open(now: datetime | None = None) -> bool:
    """현재 정규장 운영 중인지."""
    now = now or now_kst()
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    now = now.astimezone(KST)
    if not is_business_day(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE
