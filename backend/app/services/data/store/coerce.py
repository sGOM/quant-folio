"""DataFrame 값 → DB 컬럼 타입 변환.

리포지토리 3종(daily·periods·indexes)이 같은 변환 규칙을 쓴다. 다른 것은 어떤
컬럼이 어떤 타입인가뿐이라, 규칙은 여기 한 벌만 두고 컬럼→종류 매핑만 각자 갖는다.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

import pandas as pd

#: 컬럼 종류 — NUMERIC / BigInteger / String 에 대응.
NUMERIC = "numeric"
INTEGER = "integer"
TEXT = "text"

#: 테이블 컬럼 중 텍스트로 저장되는 컬럼 이름 집합 — daily.py/periods.py 의 읽기 경로가
#: 공유한다. pd.to_numeric 강제변환 루프에서 이 집합을 건너뛰지 않으면 "market"·
#: "name" 같은 문자열 컬럼이 전부 NaN 이 된다.
_TEXT_COLUMNS = frozenset({"market", "name"})


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
    if isinstance(value, float) and math.isinf(value):
        # pd.isna(inf) 는 False 라 위 가드를 통과한다. INTEGER 분기에서
        # int(float(inf)) 는 (TypeError, ValueError) 가 아니라 OverflowError 를
        # 던져 이 함수를 호출한 write_daily/write_periods 전체를 실패시킨다
        # (그 값은 DataSourceError 가 아니라 상위 예외 처리에도 안 잡힌다).
        # NUMERIC 분기는 예외 없이 Decimal('Infinity') 를 그대로 반환해버리는데,
        # 그 값을 저장 가능한지는 DB 쪽 사정이라 이 계층에서 보장할 수 없다.
        # 위 docstring 대로 이상값 한 개가 전체 적재를 막으면 안 되므로 None 으로
        # 떨어뜨린다.
        return None
    if kind == NUMERIC:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if kind == INTEGER:
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            # OverflowError 백스톱: 위 inf 가드는 float/np.float64 만 잡는다.
            # np.float32('inf')·문자열 "inf" 등 다른 경로로 들어온 무한대는 여기서
            # 걸러야 int(float(value)) 가 OverflowError 를 던지는 걸 막는다.
            return None
    return str(value)
