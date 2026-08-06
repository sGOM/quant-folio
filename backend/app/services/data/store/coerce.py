"""DataFrame 값 → DB 컬럼 타입 변환.

리포지토리 3종(daily·periods·indexes)이 같은 변환 규칙을 쓴다. 다른 것은 어떤
컬럼이 어떤 타입인가뿐이라, 규칙은 여기 한 벌만 두고 컬럼→종류 매핑만 각자 갖는다.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

import pandas as pd

#: 컬럼 종류 — NUMERIC / BigInteger / String 에 대응.
NUMERIC = "numeric"
INTEGER = "integer"
TEXT = "text"


def coerce_value(value: object, kind: str) -> object | None:
    """DataFrame 셀 값을 저장 타입으로 변환한다. 결측·변환 불가는 None.

    pykrx 는 결측을 NaN·None·빈 문자열로 섞어 돌려주고, 컬럼 하나에 숫자와 문자열이
    섞여 오는 경우도 있다. 변환 실패를 예외로 올리지 않고 None 으로 떨어뜨리는 이유는
    종목 한 개의 이상값이 그 날짜 전체 적재를 막으면 안 되기 때문이다.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass  # pd.isna 가 스칼라를 못 주는 값(배열 등) — 아래 변환에서 걸러진다
    if kind == NUMERIC:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if kind == INTEGER:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return str(value)
