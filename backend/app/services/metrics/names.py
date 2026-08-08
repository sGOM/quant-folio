"""종목 코드→이름 매핑 — KRX 조회 결과 재활용 맵과 내장 카탈로그 맵."""
from __future__ import annotations

import logging

import pandas as pd

from app.services.symbols import get_catalog

logger = logging.getLogger("app.services.metrics")


def _build_krx_name_map(*frames: pd.DataFrame) -> dict[str, str]:
    """price_change 프레임들의 '종목명' 컬럼에서 코드→이름 맵을 만든다.

    이미 조회한 데이터(get_market_price_change)를 재활용하므로 추가 KRX 호출이
    없다. 여러 프레임을 넘기면 먼저 채워진 값을 유지한다(먼저 온 프레임 우선).
    코드는 6자리 zero-fill 로 정규화한다.
    """
    names: dict[str, str] = {}
    for df in frames:
        if df is None or df.empty or "종목명" not in df.columns:
            continue
        for code, name in df["종목명"].items():
            key = str(code).zfill(6)
            if key not in names:
                # 결측을 먼저 걸러낸다 — str(None)=="None", str(nan)=="nan" 은 둘 다
                # truthy 라 그대로 두면 종목명이 "None"/"nan" 으로 등록된다. 로컬
                # 저장소에서 읽은 프레임은 name 컬럼이 NULL 일 수 있어(§49 I1 이전
                # 적재분) 실제로 도달 가능한 경로다.
                if name is None or (isinstance(name, float) and name != name):
                    continue
                text = str(name).strip()
                if text:
                    names[key] = text
    return names


def _build_name_map() -> dict[str, str]:
    """symbol 카탈로그에서 코드→이름 딕셔너리를 반환한다.

    blocking 함수이지만 프로세스 캐시이므로 최초 1회만 빌드된다.
    """
    try:
        catalog = get_catalog()
        return {item["code"]: item["name"] for item in catalog}
    except Exception:
        logger.warning("종목 이름 카탈로그 로드 실패", exc_info=True)
        return {}
