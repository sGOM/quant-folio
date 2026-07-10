"""지표 계산 공통 헬퍼 — 영업일/날짜 변환, JSON-safe 숫자, MDD·변동성, 시장 파싱.

pykrx 조회·팩터·섹터·종목 계산 모듈이 공유하는 순수 유틸이며 내부 다른 metrics
서브모듈에 의존하지 않는다(순환 회피의 최하위 계층).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.services.market import is_business_day
# JSON-safe 숫자 변환은 서비스 공용 유틸로 통합했다. 기존 import 경로
# (metrics.common / metrics 패키지)를 보존하기 위해 여기서 재노출한다.
from app.services._num import _is_nan, _safe_bool, _safe_float  # noqa: F401

logger = logging.getLogger("app.services.metrics")


def _last_business_day() -> date:
    """직전 확정 영업일을 반환한다(당일 미확정 데이터 혼입 방지).

    오늘 날짜에서 하루씩 거슬러 올라가며 is_business_day를 확인한다.
    """
    d = date.today() - timedelta(days=1)
    for _ in range(14):  # 최대 2주 탐색 (연휴 대비)
        if is_business_day(d):
            return d
        d -= timedelta(days=1)
    # 그래도 못 찾으면 최근 월요일 반환(극단적 폴백)
    return date.today() - timedelta(days=date.today().weekday() + 7)


def _prev_business_day(d: date) -> date:
    """d 바로 이전 영업일을 반환한다."""
    candidate = d - timedelta(days=1)
    for _ in range(10):
        if is_business_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    return d - timedelta(days=3)


def _approx_start(as_of: date, bdays: int, buffer: int = 14) -> date:
    """as_of 기준 bdays 영업일 이전의 시작일을 캘린더 일수로 근사한다.

    7/5 변환 + 여유 버퍼를 더해 주말·공휴일로 인한 부족분을 방지한다.
    """
    cal_days = int(bdays * 7 / 5) + buffer
    return as_of - timedelta(days=cal_days)


def _ymd(d: date) -> str:
    """날짜를 pykrx 형식 문자열(YYYYMMDD)로 변환."""
    return d.strftime("%Y%m%d")


def _pct_dec(series: pd.Series) -> pd.Series:
    """pykrx 퍼센트 값을 소수로 변환 (1.23% → 0.0123)."""
    return series / 100.0


def _compute_mdd(close: pd.Series) -> float | None:
    """Rolling 고점 대비 최대낙폭(MDD)을 계산한다. 결과는 음수 소수."""
    if close.empty or len(close) < 2:
        return None
    peak = close.expanding().max()
    dd = (close - peak) / peak
    return _safe_float(dd.min())


def _compute_vol_ann(close: pd.Series) -> float | None:
    """일간 로그수익률의 연율 변동성(std × √252)을 계산한다."""
    if len(close) < 20:
        return None
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) < 10:
        return None
    return _safe_float(log_ret.std() * np.sqrt(252))


def _mkts(market: str) -> list[str]:
    """API market 파라미터를 pykrx market 문자열 목록으로 변환한다."""
    if market.upper() == "ALL":
        return ["KOSPI", "KOSDAQ"]
    return [market.upper()]
