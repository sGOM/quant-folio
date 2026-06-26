"""전략·백테스트 스키마.

전략 config 는 유형(type)별 discriminated union 으로, 모든 유형이 공통 필드
(종목·초기자본·수수료·리스크 청산 파라미터)를 상속한다. config 컬럼은 JSONB 라
유형/파라미터 추가에 DB 마이그레이션이 필요 없다.
"""
from datetime import date, datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _BaseConfig(BaseModel):
    """모든 전략 유형의 공통 파라미터(종목·자본·수수료·리스크 청산)."""
    symbol: str = Field(min_length=6, max_length=20, description="KRX 종목코드 예: 005930")
    cash: float = Field(default=10_000_000, gt=0)
    # 거래비용(비율, 0~0.01=0~1%).
    # fees: 위탁수수료율(매수·매도 양방향). 일반적으로 온라인 0.0001~0.00015(0.01~0.015%).
    fees: float = Field(default=0.00015, ge=0, le=0.01, description="위탁수수료율(매수·매도)")
    # tax: 증권거래세율(매도 시에만). 2026년 KRX 0.0020(0.20%, 코스피=거래세0.05%+농특세0.15%, 코스닥0.20%).
    tax: float = Field(default=0.0020, ge=0, le=0.01, description="증권거래세율(매도 시)")
    # 리스크 청산(None=비활성). 비율 0~1 (예: 0.05 = 5%).
    stop_loss_pct: float | None = Field(default=None, gt=0, le=1)
    take_profit_pct: float | None = Field(default=None, gt=0, le=1)
    trailing_stop_pct: float | None = Field(default=None, gt=0, le=1)


class _MaCrossMixin(_BaseConfig):
    """이동평균 크로스 공통(fast < slow)."""
    fast: int = Field(ge=1, le=240)
    slow: int = Field(ge=2, le=480)

    @model_validator(mode="after")
    def _fast_lt_slow(self):
        if self.fast >= self.slow:
            raise ValueError("fast 기간은 slow 기간보다 작아야 합니다.")
        return self


class SmaConfig(_MaCrossMixin):
    type: Literal["sma_crossover"] = "sma_crossover"
    fast: int = Field(default=5, ge=1, le=240)
    slow: int = Field(default=20, ge=2, le=480)


class EmaConfig(_MaCrossMixin):
    type: Literal["ema_crossover"] = "ema_crossover"
    fast: int = Field(default=12, ge=1, le=240)
    slow: int = Field(default=26, ge=2, le=480)


class RsiConfig(_BaseConfig):
    type: Literal["rsi"] = "rsi"
    period: int = Field(default=14, ge=2, le=240)
    lower: float = Field(default=30, ge=1, le=99, description="과매도 임계(매수)")
    upper: float = Field(default=70, ge=1, le=99, description="과매수 임계(매도)")

    @model_validator(mode="after")
    def _lower_lt_upper(self):
        if self.lower >= self.upper:
            raise ValueError("RSI lower 는 upper 보다 작아야 합니다.")
        return self


class MacdConfig(_BaseConfig):
    type: Literal["macd"] = "macd"
    fast: int = Field(default=12, ge=1, le=240)
    slow: int = Field(default=26, ge=2, le=480)
    signal: int = Field(default=9, ge=1, le=120)

    @model_validator(mode="after")
    def _fast_lt_slow(self):
        if self.fast >= self.slow:
            raise ValueError("MACD fast 는 slow 보다 작아야 합니다.")
        return self


class BollingerConfig(_BaseConfig):
    type: Literal["bollinger"] = "bollinger"
    period: int = Field(default=20, ge=2, le=240)
    num_std: float = Field(default=2.0, gt=0, le=5)


class BreakoutConfig(_BaseConfig):
    type: Literal["breakout"] = "breakout"
    period: int = Field(default=20, ge=2, le=480, description="Donchian 채널 기간")


class MomentumConfig(_BaseConfig):
    type: Literal["momentum"] = "momentum"
    lookback: int = Field(default=120, ge=1, le=480, description="모멘텀 측정 봉 수")


class ZScoreConfig(_BaseConfig):
    type: Literal["zscore"] = "zscore"
    period: int = Field(default=20, ge=2, le=240)
    entry: float = Field(default=2.0, gt=0, le=5, description="진입 z 임계(저평가)")


class DisparityConfig(_BaseConfig):
    type: Literal["disparity"] = "disparity"
    period: int = Field(default=20, ge=2, le=240)
    lower: float = Field(default=95.0, ge=1, le=200, description="과매도 이격도(매수)")
    upper: float = Field(default=105.0, ge=1, le=200, description="과매수 이격도(매도)")

    @model_validator(mode="after")
    def _lower_lt_upper(self):
        if self.lower >= self.upper:
            raise ValueError("이격도 lower 는 upper 보다 작아야 합니다.")
        return self


class DonchianSqueezeConfig(_BaseConfig):
    """변동성 스퀴즈 돌파. 일간 변화(Δclose)의 표준편차(볼린저) < 평균절대값(켈트너)인
    '스퀴즈'(고르고 작은 저변동) 해제 시 추세 방향으로 진입.

    두 변동성 척도를 모두 '일간 변화' 기반으로 통일해 스케일을 맞춘다. 임계
    kc_mult/bb_mult 가 std/mean|Δ| 비율(정규분포 기준 ≈1.25)을 넘나들며 스퀴즈가 결정된다.
    """
    type: Literal["donchian_squeeze"] = "donchian_squeeze"
    period: int = Field(default=20, ge=3, le=480, description="변동성/평활 기간")
    bb_mult: float = Field(default=1.0, gt=0, le=10, description="일간변화 표준편차 배수(볼린저)")
    kc_mult: float = Field(default=1.3, gt=0, le=10, description="일간변화 평균절대값 배수(켈트너)")


class TrixConfig(_BaseConfig):
    """TRIX 삼중지수평활 모멘텀. TRIX 가 시그널선을 교차할 때 매매."""
    type: Literal["trix"] = "trix"
    period: int = Field(default=15, ge=1, le=240, description="삼중 EMA 기간")
    signal_period: int = Field(default=9, ge=1, le=120, description="TRIX 시그널선 EMA 기간")


class ObvTrendConfig(_BaseConfig):
    """OBV 거래량 추세 확인. OBV 가 자기 이동평균을 교차할 때 매매(OHLC+volume)."""
    type: Literal["obv_trend"] = "obv_trend"
    period: int = Field(default=20, ge=2, le=480, description="OBV 이동평균 기간")


class AtrTrailingConfig(_BaseConfig):
    """ATR 트레일링 / 샹들리에 청산(OHLC). 진입=Donchian 돌파, 청산=샹들리에."""
    type: Literal["atr_trailing"] = "atr_trailing"
    period: int = Field(default=22, ge=2, le=480, description="Donchian/샹들리에 채널 기간")
    atr_period: int = Field(default=22, ge=2, le=240, description="ATR(Wilder) 기간")
    k: float = Field(default=3.0, gt=0, le=20, description="ATR 배수(청산폭)")


class VolatilityBreakoutConfig(_BaseConfig):
    """변동성 돌파(래리 윌리엄스, OHLC). Target=Open+k·전일Range, 당일종가 청산."""
    type: Literal["volatility_breakout"] = "volatility_breakout"
    k: float = Field(default=0.5, gt=0, le=5, description="전일 변동폭 돌파 계수")


class KeltnerConfig(_BaseConfig):
    """켈트너 채널(OHLC). Mid=EMA, 밴드=Mid±m·ATR. 상단 돌파 매수, 중심선 하향 청산."""
    type: Literal["keltner"] = "keltner"
    ema_period: int = Field(default=20, ge=2, le=240, description="중심선 EMA 기간")
    atr_period: int = Field(default=10, ge=2, le=240, description="ATR(Wilder) 기간")
    mult: float = Field(default=2.0, gt=0, le=10, description="ATR 밴드 배수")


class StochasticConfig(_BaseConfig):
    """스토캐스틱(OHLC). %K/%D 교차로 과매도 매수·과매수 매도."""
    type: Literal["stochastic"] = "stochastic"
    k_period: int = Field(default=14, ge=2, le=240, description="%K 룩백 기간")
    d_period: int = Field(default=3, ge=1, le=60, description="%D(=%K SMA) 기간")
    lower: float = Field(default=20.0, ge=1, le=99, description="과매도 임계(매수)")
    upper: float = Field(default=80.0, ge=1, le=99, description="과매수 임계(매도)")

    @model_validator(mode="after")
    def _lower_lt_upper(self):
        if self.lower >= self.upper:
            raise ValueError("스토캐스틱 lower 는 upper 보다 작아야 합니다.")
        return self


# ───────────────────── 사용자 정의(비주얼 룰 빌더) ─────────────────────


class Operand(BaseModel):
    """규칙의 피연산자. kind 에 따라 필요한 필드만 사용한다.

    - price:  source(close/open/high/low/volume) 가격/거래량 시계열
    - const:  value 상수
    - sma/ema/rsi:  period 단일 기간 지표(종가 기반)
    - macd_line/macd_signal:  fast/slow/signal MACD 선·시그널선(종가 기반)
    """
    kind: Literal[
        "price", "const", "sma", "ema", "rsi", "macd_line", "macd_signal"
    ]
    source: Literal["close", "open", "high", "low", "volume"] | None = None
    value: float | None = None
    period: int | None = Field(default=None, ge=1, le=480)
    fast: int | None = Field(default=None, ge=1, le=240)
    slow: int | None = Field(default=None, ge=2, le=480)
    signal: int | None = Field(default=None, ge=1, le=120)

    @model_validator(mode="after")
    def _check_required(self):
        if self.kind == "price" and self.source is None:
            raise ValueError("price 피연산자는 source 가 필요합니다.")
        if self.kind == "const" and self.value is None:
            raise ValueError("const 피연산자는 value 가 필요합니다.")
        if self.kind in ("sma", "ema", "rsi") and self.period is None:
            raise ValueError(f"{self.kind} 피연산자는 period 가 필요합니다.")
        if self.kind in ("macd_line", "macd_signal"):
            if self.fast is None or self.slow is None or self.signal is None:
                raise ValueError("MACD 피연산자는 fast/slow/signal 이 필요합니다.")
            if self.fast >= self.slow:
                raise ValueError("MACD fast 는 slow 보다 작아야 합니다.")
        return self


class Condition(BaseModel):
    """좌변과 우변을 비교 연산자로 묶은 단일 조건(논리식의 잎 노드)."""
    left: Operand
    op: Literal[">", "<", ">=", "<="]
    right: Operand


class ConditionGroup(BaseModel):
    """조건/하위 그룹을 AND 또는 OR 로 결합하는 논리 그룹(중첩 가능).

    children 은 단일 조건(Condition) 또는 다시 그룹(ConditionGroup)일 수 있어
    `(A OR B) AND C` 같은 임의의 논리식을 트리로 표현한다.
    """
    combinator: Literal["and", "or"] = "and"
    children: list["RuleNode"] = Field(min_length=1, description="조건 또는 하위 그룹들")


#: 논리식 노드 = 단일 조건 또는 (중첩) 그룹. smart union 으로 구조에 따라 판별
#: (그룹은 children, 조건은 left/op/right 를 가져 필드가 겹치지 않는다).
RuleNode = Annotated[Union[ConditionGroup, Condition], Field(union_mode="smart")]

ConditionGroup.model_rebuild()


class CustomConfig(_BaseConfig):
    """사용자 정의 전략(no-code 룰 빌더).

    진입(entry)·청산(exit)은 각각 하나의 논리 그룹(AND/OR 중첩 트리)이며, 결합
    상태가 거짓→참으로 바뀌는 순간(상승 에지)을 매매 신호로 본다(연속 봉 중복 신호 방지).
    하위호환: 조건 list 로 들어오면 AND 그룹으로 자동 변환한다.
    """
    type: Literal["custom"] = "custom"
    entry: ConditionGroup = Field(description="진입(매수) 논리식")
    exit: ConditionGroup = Field(description="청산(매도) 논리식")

    @field_validator("entry", "exit", mode="before")
    @classmethod
    def _wrap_legacy_list(cls, v):
        """레거시 조건 list(AND) → 그룹 dict 로 정규화(기존 저장 설정 호환)."""
        if isinstance(v, list):
            return {"combinator": "and", "children": v}
        return v


# ───────────────────── 리밸런싱(포트폴리오) ─────────────────────


class RebalanceSelection(BaseModel):
    """리밸런싱 종목 선정 규칙.

    - method="momentum": lookback 봉 수익률 상위 top_n 종목 선정
    - method="all": universe 전체 사용(top_n 무시)
    """
    method: Literal["momentum", "all"] = "momentum"
    lookback: int = Field(default=120, ge=2, le=480, description="모멘텀 측정 봉 수")
    top_n: int = Field(default=5, ge=1, le=50, description="선정 종목 수(momentum)")


class RebalanceConfig(BaseModel):
    """주기적 포트폴리오 리밸런싱 전략.

    단일 종목 전략(_BaseConfig)과 달리 다종목 universe 를 운용하며, 주기적으로
    선정 규칙에 따라 목표 비중을 재산정해 드리프트 밴드를 넘는 종목만 매매한다.
    실제 발화·주문은 엔진의 RebalanceRunner 가 담당한다.
    """
    type: Literal["rebalance"] = "rebalance"
    universe: list[str] = Field(
        min_length=1, max_length=50, description="후보 종목코드 목록"
    )
    selection: RebalanceSelection = Field(default_factory=RebalanceSelection)
    weighting: Literal["equal"] = Field(default="equal", description="선정 종목 비중 방식")
    cadence: Literal["daily", "weekly", "monthly"] = "monthly"
    # weekly: 0=월~4=금 / monthly: 1~28(영업일 보정은 러너가 수행)
    rebalance_weekday: int | None = Field(default=None, ge=0, le=4)
    rebalance_dom: int | None = Field(default=None, ge=1, le=28)
    rebalance_time: str = Field(
        default="14:30", description="실행 시각 HH:MM (KST), 장 마감 전 체결 여유"
    )
    drift_band_pct: float = Field(
        default=0.05, ge=0, le=1, description="목표 대비 비중 편차 임계(초과만 매매)"
    )
    capital: float = Field(default=10_000_000, gt=0, description="이 전략이 운용할 배정 자본")
    fees: float = Field(default=0.00015, ge=0, le=0.01, description="위탁수수료율")
    tax: float = Field(default=0.0020, ge=0, le=0.01, description="증권거래세율(매도 시)")

    @field_validator("universe")
    @classmethod
    def _validate_symbols(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v]
        for s in cleaned:
            if not (6 <= len(s) <= 20):
                raise ValueError(f"종목코드 길이가 올바르지 않습니다: {s!r}")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("universe 에 중복 종목코드가 있습니다.")
        return cleaned

    @field_validator("rebalance_time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        try:
            hh, mm = v.split(":")
            h, m = int(hh), int(mm)
        except (ValueError, AttributeError):
            raise ValueError("rebalance_time 은 'HH:MM' 형식이어야 합니다.")
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("rebalance_time 시각 범위가 올바르지 않습니다.")
        return f"{h:02d}:{m:02d}"

    @model_validator(mode="after")
    def _validate_consistency(self):
        if self.selection.method == "momentum" and self.selection.top_n > len(self.universe):
            raise ValueError("selection.top_n 은 universe 종목 수 이하여야 합니다.")
        if self.cadence == "weekly" and self.rebalance_weekday is None:
            self.rebalance_weekday = 0  # 기본: 월요일
        if self.cadence == "monthly" and self.rebalance_dom is None:
            self.rebalance_dom = 1  # 기본: 1일(영업일 보정)
        return self


StrategyConfig = Annotated[
    Union[
        SmaConfig,
        EmaConfig,
        RsiConfig,
        MacdConfig,
        BollingerConfig,
        BreakoutConfig,
        MomentumConfig,
        ZScoreConfig,
        DisparityConfig,
        DonchianSqueezeConfig,
        TrixConfig,
        ObvTrendConfig,
        AtrTrailingConfig,
        VolatilityBreakoutConfig,
        KeltnerConfig,
        StochasticConfig,
        CustomConfig,
        RebalanceConfig,
    ],
    Field(discriminator="type"),
]


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    config: StrategyConfig


class StrategyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    config: StrategyConfig | None = None


class StrategyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    config: dict
    status: str
    featured_backtest_id: int | None = None
    is_shared: bool = False
    is_favorite: bool = False
    sort_order: int = 0
    created_at: datetime
    updated_at: datetime


class BacktestSummary(BaseModel):
    """공유 시 노출되는 경량 백테스트 성과 요약(대용량 equity_curve 등은 제외)."""
    id: int
    total_return: float | None = None
    mdd: float | None = None
    sharpe: float | None = None
    period_start: datetime
    period_end: datetime


class SharedStrategyOut(BaseModel):
    """공유 전략 목록 항목. 작성자 닉네임·좋아요 수·설명·대표 백테스트 성과를 포함한다."""
    id: int
    name: str
    description: str | None
    config: dict
    author_name: str
    like_count: int
    liked_by_me: bool
    is_mine: bool
    backtest: BacktestSummary | None
    created_at: datetime


class ReorderRequest(BaseModel):
    """내 전략 표시 순서 일괄 갱신. 배열 순서대로 sort_order 를 0..n 부여."""
    ordered_ids: list[int] = Field(min_length=1)


class LikeOut(BaseModel):
    """좋아요 토글 결과."""
    like_count: int
    liked_by_me: bool


class FeaturedBacktestIn(BaseModel):
    """대표 백테스트 지정/해제. None 이면 해제."""
    backtest_id: int | None = None


class BacktestRequest(BaseModel):
    period_start: date
    period_end: date


class BacktestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: int
    period_start: datetime
    period_end: datetime
    total_return: float | None = None
    mdd: float | None = None
    sharpe: float | None = None
    result: dict | None = None
    created_at: datetime
