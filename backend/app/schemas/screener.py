"""소형주 턴어라운드 스크리너 응답 스키마.

전 시장을 스캔해 하드 필터(시총 하위·거래대금 급증·저부채·만성적자 제외·흑자전환)를
통과한 후보를 반환한다. 고정 유니버스 랭킹(리밸런싱 전략)과 달리 시장 전체에서
후보를 발굴하는 발굴(스크리닝) 용도다.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel


class TurnaroundCandidate(BaseModel):
    """턴어라운드 후보 1건."""

    code: str
    name: str
    market: str
    market_cap: int             # 시가총액(원)
    avg_value_20: float         # 20일 평균 거래대금(원)
    turnover_surge: Optional[float] = None  # 최근5일 평균 ÷ 최근60일 평균 거래대금
    # OpenDART 재무(없으면 None)
    net_income: Optional[float] = None
    debt_ratio: Optional[float] = None      # 부채/자본(배수)
    turnaround: Optional[float] = None       # 흑자전환 1.0/0.0
    net_growth: Optional[float] = None       # 순이익 YoY
    f_score: Optional[int] = None            # 수정 Piotroski(0~8)
    loss_years_3: Optional[int] = None       # 최근 3년 중 적자 연수
    per: Optional[float] = None
    pbr: Optional[float] = None
    score: float                # 종합 후보점수(정렬용)


class TurnaroundScreenOut(BaseModel):
    """턴어라운드 스크리너 응답."""

    as_of: date
    market: str
    scanned: int                # 스캔한 소형주 후보 수(재무조회 대상)
    count: int
    opendart_enabled: bool      # OpenDART 키 유무(없으면 재무 필터 미적용)
    # 이번 응답에 재무 하드 필터(부채비율·만성적자)가 실제로 적용됐는지. 키가 있어도
    # (opendart_enabled=True) 조회가 실패하면 필터가 후보 전원에 대해 건너뛰어져,
    # 걸러졌어야 할 종목이 결과에 남는다. 그 사실이 응답에 드러나지 않으면 사용자는
    # 정상 결과와 구분할 수 없다 — 로그에만 남기면 §44-1 과 같은 형태가 된다.
    financial_filter_applied: bool = True
    items: list[TurnaroundCandidate]
