"""지표 화면 응답 스키마 — 섹터 및 종목별 지표 API 계약.

프론트엔드와 고정된 JSON 계약이므로 필드명·타입을 임의로 변경하지 말 것.
- 모든 비율(수익률·변동성·MDD·등락률)은 소수(0.0123 = 1.23%).
- 배당수익률 div는 pykrx 원값 그대로 퍼센트 숫자(2.1 = 2.1%).
- null 허용 필드는 계산 불가 시 None.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel


class SectorMetric(BaseModel):
    """섹터(업종지수) 1건 지표."""

    name: str
    market: str  # "KOSPI" | "KOSDAQ"
    # ── 모멘텀(소수) ──
    mom_1m: Optional[float] = None   # 21영업일 수익률
    mom_3m: Optional[float] = None   # 63영업일 수익률
    mom_6m: Optional[float] = None   # 126영업일 수익률
    # ── 상대강도 / 리스크 ──
    rs_126: Optional[float] = None          # 6M 섹터수익률 ÷ 기준시장 6M수익률
    vol_ann: Optional[float] = None         # 연율 변동성(일간 로그수익률 std × √252)
    risk_adj_mom: Optional[float] = None    # mom_6m ÷ vol_ann
    value_trend: Optional[float] = None     # 거래대금 MA20 ÷ MA60
    high_52w_ratio: Optional[float] = None  # 종가 ÷ 52주 최고가
    mdd_252: Optional[float] = None         # 252영업일 최대낙폭(음수)
    # 섹터 밸류(PER/PBR/배당) 중앙값은 KRX 업종 구성종목 API
    # (get_index_portfolio_deposit_file)가 현재 pykrx 버전에서 빈 값을 반환해
    # 산출 불가하므로 응답에서 제외한다(재확보 시 필드 복원).


class SectorsOut(BaseModel):
    """섹터 지표 목록 응답."""

    as_of: date
    items: list[SectorMetric]


class StockMetric(BaseModel):
    """종목 1건 지표."""

    code: str
    name: str
    market: str   # "KOSPI" | "KOSDAQ"
    price: int    # 종가(원)
    change_rate: Optional[float] = None  # 당일 등락률(소수)
    market_cap: int    # 시가총액(원)
    avg_value_20: float  # 20일 평균 거래대금(원)
    # ── 밸류에이션(div은 pykrx 원값 %) ──
    per: Optional[float] = None
    pbr: Optional[float] = None
    div: Optional[float] = None  # 퍼센트 단위 (pykrx 원값)
    # ── 모멘텀(소수) ──
    mom_1m: Optional[float] = None
    mom_3m: Optional[float] = None
    mom_6m: Optional[float] = None
    mom_12_1: Optional[float] = None        # 12-1 모멘텀(12M 수익률 중 최근 1M 제외)
    high_52w_ratio: Optional[float] = None  # 종가 ÷ 52주 최고가
    # ── 기술지표 ──
    rsi14: Optional[float] = None
    vol_ann: Optional[float] = None   # 연율 변동성(소수)
    mdd_252: Optional[float] = None   # 252영업일 최대낙폭(음수 소수)
    trend_aligned: Optional[bool] = None   # 정배열(SMA5>20>60>120)
    above_sma200: Optional[bool] = None    # 종가 > SMA200
    # ── 종합 점수(크로스섹션 z-score 합성) ──
    score: Optional[float] = None
    score_value: Optional[float] = None
    score_momentum: Optional[float] = None
    score_lowvol: Optional[float] = None


class StocksOut(BaseModel):
    """종목 지표 목록 응답."""

    as_of: date
    count: int
    items: list[StockMetric]


# ─────────────────────────── 패닉셀(투매) 지표 ───────────────────────────


class PanicSignal(BaseModel):
    """패닉셀 구성 시그널 1건.

    value는 원시 측정값(수익률·비율·배율 등, 시그널마다 단위가 다름),
    subscore는 경계(warn)=0·패닉(panic)=100으로 선형 보간한 0~100 점수.
    weight가 0이면 종합점수에 들어가지 않는 참고/하드트리거용 신호.
    """

    key: str
    label: str
    value: Optional[float] = None      # 원시 측정값(시그널별 단위 상이)
    subscore: Optional[float] = None   # 0~100 (경계 0 · 패닉 100)
    weight: float                       # 종합점수 가중(0=비가중 참고)
    warn: Optional[float] = None       # 경계 임계값
    panic: Optional[float] = None      # 패닉 임계값


class PanicMarket(BaseModel):
    """시장 1개(KOSPI 또는 KOSDAQ)의 패닉셀 판정."""

    market: str                        # "KOSPI" | "KOSDAQ"
    score: float                       # 0~100 (dd60 국면 보정·클램프 후)
    level: str                         # "normal"|"caution"|"warning"|"panic"
    gated: bool                        # 패닉 게이팅 충족(가격급락+거래폭증/브레드스)
    hard_trigger: bool                 # 하드트리거 발동(극단값 → 즉시 패닉)
    price_sub: Optional[float] = None    # 가격축 서브스코어(0~100)
    breadth_sub: Optional[float] = None  # 브레드스축 서브스코어(0~100)
    dd60: Optional[float] = None       # 60일 고점대비 낙폭(음수 소수, 국면 참고)
    universe: int                      # 브레드스 유효 종목 수(거래정지 제외)
    signals: list[PanicSignal]


class PanicOut(BaseModel):
    """패닉셀 지표 응답(시장별 목록)."""

    as_of: date
    items: list[PanicMarket]
    # 조회/데이터 부족으로 계산하지 못한 시장 목록(예: ["KOSDAQ"]).
    # items에서 조용히 누락되는 대신 여기에 명시해 "정상"과 "계산 불가"를 구분한다.
    unavailable: list[str] = []
