"""JSON 안전(safe) 숫자 변환 공용 유틸.

metrics·backtest 등 여러 모듈이 각자 정의하던 NaN/inf → None 변환 헬퍼를 한곳으로
통합한다. 응답 직렬화 직전 float/bool 값을 JSON 표현 가능한 형태로 정규화한다.
"""
from __future__ import annotations

from typing import Any

import numpy as np


def safe_float(val: Any) -> float | None:
    """NaN/None/무한대를 None 으로 변환해 JSON-safe float 를 반환한다."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def is_nan(val: Any) -> bool:
    """None 또는 NaN/무한대 여부를 확인한다."""
    if val is None:
        return True
    try:
        return not np.isfinite(float(val))
    except (TypeError, ValueError):
        return True


def safe_bool(val: Any) -> bool | None:
    """NaN/None 을 None 으로 변환해 bool 을 반환한다."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    return bool(val)


# 밑줄 접두 별칭 — 기존 호출부(_safe_float/_is_nan/_safe_bool/_safe) 호환용.
_safe_float = safe_float
_is_nan = is_nan
_safe_bool = safe_bool
_safe = safe_float  # portfolio 의 _safe 는 _safe_float 와 동일 의미(JSON-safe float)
