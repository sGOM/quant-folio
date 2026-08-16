"""KRX 장 운영시간/휴장일 판단.

정규장 09:00~15:30 (KST), 주말·공휴일 휴장. 공휴일은 pykrx 로 best-effort 확인한다.
pykrx 는 데이터 기반이라 '당일 데이터 미발행'(실거래 아침·미래 시계)을 휴장으로 오판할
수 있어, 대상일 이후 지수 데이터 존재 여부로 '실제 휴장일'과 '미발행'을 구분한다. 판정
불가(조회 실패·미발행)면 평일은 영업일로 간주하고, 실제 휴장이면 시세·주문에서 자연 차단.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

logger = logging.getLogger(__name__)

# pykrx 의 get_nearest_business_day_in_a_week 는 자체 timeout 을 지정하지 않는다.
# is_business_day 는 엔진 러너(engine/runner.py, engine/rebalance_runner.py)의 코루틴
# 안에서 asyncio.to_thread 없이 "직접(동기)" 호출되므로, 응답이 없으면 이 한 호출이
# 프로세스의 이벤트 루프 전체를 멈춰 세운다(해당 전략뿐 아니라 다른 모든 태스크·
# 하트비트까지 정지) — 짧게라도 반드시 타임아웃을 걸어야 한다.
_BUSINESS_DAY_TIMEOUT = 5  # 초

#: 연간 한국 공휴일 대략치(설·추석 연휴 포함) — 공식 통계가 아니라 근사 계산용 상수다.
_KR_HOLIDAYS_PER_YEAR = 15

KST = timezone(timedelta(hours=9))
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)


def now_kst() -> datetime:
    return datetime.now(KST)


def estimated_trading_days(start: date, end: date) -> float:
    """[start, end] 구간의 대략적인 기대 거래일 수 — 근사치이지 정확한 달력이 아니다.

    달력일 × 5/7(평일 비중)에서 연 `_KR_HOLIDAYS_PER_YEAR` 일 가정으로 공휴일을 뺀다.
    pykrx 호출이 없는 순수 계산이라 반복 호출 비용이 없지만, 설·추석 등 실제 공휴일
    분포와는 어긋날 수 있다 — 정밀 판정이 아니라 "요청의 절반도 못 받은" 수준의 명백한
    부분 응답만 잡는 넉넉한 임계값과 함께 쓸 것을 전제한다.
    """
    calendar_days = (end - start).days + 1
    if calendar_days <= 0:
        return 0.0
    weekdays = calendar_days * 5 / 7
    holiday_estimate = calendar_days * (_KR_HOLIDAYS_PER_YEAR / 365)
    return max(weekdays - holiday_estimate, 0.0)


@lru_cache(maxsize=512)
def is_business_day(d: date) -> bool:
    """영업일 여부. 주말 제외 + pykrx 휴장일 확인(가능할 때)."""
    if d.weekday() >= 5:  # 토(5)/일(6)
        return False
    # 전역 소켓 타임아웃 직접 조작은 다른 러너 스레드의 조작과 경합한다(prev 캡처가 서로
    # 엉킴). 공유 컨텍스트 매니저로 통일해 락으로 직렬화된 안전한 타임아웃을 사용한다.
    from app.services.data.loader import bounded_socket_timeout

    try:
        from pykrx import stock

        with bounded_socket_timeout(_BUSINESS_DAY_TIMEOUT):
            ymd = d.strftime("%Y%m%d")
            nearest = stock.get_nearest_business_day_in_a_week(ymd)
            if nearest == ymd:
                return True
            # nearest != ymd: pykrx 가 대상일을 거래일로 확정하지 못했다. 두 경우가 있다.
            #   (a) 실제 휴장일 → 대상일 '이후'에도 거래일 데이터가 존재한다.
            #   (b) 데이터 미발행 → 당일 지수가 아직 미공표이거나(실거래 아침) 시계가
            #       pykrx 데이터 지평선보다 앞서(테스트/미래 시계) 대상일 이후 데이터가 없다.
            # get_nearest_business_day_in_a_week 는 휴장일·미발행 모두 '과거'로 되짚으므로
            # 방향으로는 구분할 수 없다. 대상일 이후 지수 데이터 존재 여부로 (a)/(b)를 가른다.
            after = stock.get_index_ohlcv_by_date(
                (d + timedelta(days=1)).strftime("%Y%m%d"),
                (d + timedelta(days=14)).strftime("%Y%m%d"),
                "1001",
            )
            if after is not None and not after.empty:
                return False  # 대상일 이후 거래일 존재 → 실제 휴장일
            # 대상일 이후 데이터 없음 = 미발행. 주말은 이미 걸렀으므로 영업일로 간주한다.
            # (이렇게 해야 당일 지수 미공표 상황에서도 엔진이 정상 발화한다. 실제로 휴장일
            # 이라면 시세·주문 단계에서 자연히 차단된다.)
            logger.warning(
                "영업일 조회: %s 이후 지수 데이터 없음(미발행 추정) — 평일이므로 영업일로 간주", d
            )
            return True
    except Exception as e:  # noqa: BLE001
        logger.debug("영업일 조회 실패(%s), 주말여부만 적용: %s", d, e)
        return True  # 주말이 아니면 일단 영업일로 (시세/주문에서 자연 차단)


async def is_market_open_async(now: datetime | None = None) -> bool:
    """`is_market_open` 의 비동기 래퍼 — 이벤트 루프 블로킹 방지.

    `is_business_day` 는 pykrx 네트워크 조회(최대 5초)를 동기로 수행하므로, 코루틴에서
    직접 호출하면 그동안 프로세스의 이벤트 루프 전체(다른 러너·하트비트·reconcile)가
    멈춘다. 러너 틱 등 async 경로에서는 이 래퍼로 스레드에 오프로드할 것.
    lru_cache 는 스레드와 공유되므로 같은 날짜의 후속 동기 호출은 즉시 반환된다.
    """
    import asyncio

    return await asyncio.to_thread(is_market_open, now)


def is_market_open(now: datetime | None = None) -> bool:
    """현재 정규장 운영 중인지."""
    now = now or now_kst()
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    now = now.astimezone(KST)
    if not is_business_day(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE
