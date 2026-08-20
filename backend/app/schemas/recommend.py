"""추천 화면 응답 스키마 — KOSPI200 팩터 가중 추천 API 계약.

프론트엔드와 고정된 JSON 계약이므로 필드명·타입을 임의로 변경하지 말 것.
- 모든 비율(수익률·변동성·MDD·등락률)은 소수(0.0123 = 1.23%).
- 배당수익률 div는 pykrx 원값 그대로 퍼센트 숫자(2.1 = 2.1%).
- score_* 세부점수는 KOSPI200 크로스섹션 내 z-score(대략 -2~+2)이며 **가중치와 무관**하다.
  프론트가 사용자 팩터 비중으로 가중합해 종합점수·Top-N 을 산출한다.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel


class RecommendMember(BaseModel):
    """KOSPI200 구성종목 1건 — 메타데이터 + 팩터별 세부점수."""

    code: str
    name: str
    price: Optional[int] = None  # 결측(종가 조회 실패)이면 None — 0 sentinel 아님(§50)
    change_rate: Optional[float] = None  # 당일 등락률(소수)
    market_cap: Optional[int] = None  # 결측(시총 조회 실패)이면 None — 0 sentinel 아님
    avg_value_20: float
    # ── 밸류에이션(div은 pykrx 원값 %) ──
    per: Optional[float] = None
    pbr: Optional[float] = None
    div: Optional[float] = None
    # ── 참고 지표(소수) ──
    mom_3m: Optional[float] = None
    mom_6m: Optional[float] = None
    vol_ann: Optional[float] = None
    mdd_252: Optional[float] = None
    trend_aligned: Optional[bool] = None
    above_sma200: Optional[bool] = None
    # ── 팩터별 세부점수(가중치 무관, KOSPI200 크로스섹션 z-score) ──
    score_momentum: Optional[float] = None
    score_value: Optional[float] = None
    score_lowvol: Optional[float] = None
    score_quality: Optional[float] = None
    score_growth: Optional[float] = None


class RecommendOut(BaseModel):
    """KOSPI200 추천 응답 목록."""

    as_of: date
    index: str  # "KOSPI200"
    count: int
    opendart_enabled: bool  # 퀄리티·성장 팩터가 실제 데이터로 채워졌는지
    items: list[RecommendMember]
