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
    # 체결 현실성 모델(P0-1). 신호는 종가(t) 확정 후 산출하되:
    #   next_close(기본): 익일 종가(t+1)에 체결 — 당일 종가 미래참조(look-ahead) 제거.
    #   same_close: 당일 종가(t) 즉시 체결(구 동작, 민감도 분석용 opt-in).
    fill_mode: Literal["next_close", "same_close"] = Field(
        default="next_close", description="체결 시점: 익일 종가(기본) 또는 당일 종가"
    )
    # 슬리피지(체결 미끄러짐, bps=0.01%). 매수는 +slip, 매도는 -slip 로 체결가에 가산한다.
    slippage_bps: float = Field(
        default=5.0, ge=0, le=200, description="편도 슬리피지(bps). 5=0.05%"
    )
    # 변동성 비례 스케일(0=고정 bps). >0 이면 최근 변동성/중앙값 비로 종목별 슬리피지를
    # 조정한다: slip = base×(1 + scale×(vol/median − 1)), 하한 0.
    slippage_vol_scale: float = Field(
        default=0.0, ge=0, le=5, description="변동성 비례 슬리피지 스케일(0=고정)"
    )
    # 위험조정지표(Sharpe·Sortino)의 무위험수익률(연). 0=구 동작(rf 미반영).
    risk_free_rate: float = Field(
        default=0.0, ge=0, le=0.2, description="위험조정지표용 무위험수익률(연)"
    )


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


class FactorWeights(BaseModel):
    """종합점수 합성 시 카테고리별 가중치(합=1.0).

    app.services.metrics._compute_stock_scores 와 동일한 5개 카테고리
    (모멘텀/밸류/저변동/퀄리티/성장)를 사용한다. 기본값은 metrics 화면과 동일한
    0.4/0.3/0.3/0.0/0.0 이며(quality=growth=0 이면 기존 3팩터 전략과 동일하게 동작),
    전략별로 다른 가중치를 자유롭게 설정할 수 있다.

    퀄리티(quality: ROE·부채비율·FCF)·성장(growth: 영업이익/순이익 YoY·흑자전환)은
    OpenDART 재무데이터로 산출하므로, 배정된 가중치를 실제로 반영하려면 OpenDART API
    키가 필요하다(키 부재 시 해당 카테고리는 중립 0 처리되어 남은 팩터로만 점수가
    산출된다 → 결과가 편향될 수 있으니 주의).
    """
    momentum: float = Field(default=0.4, ge=0, le=1, description="모멘텀 카테고리 가중치")
    value: float = Field(default=0.3, ge=0, le=1, description="밸류에이션 카테고리 가중치")
    lowvol: float = Field(default=0.3, ge=0, le=1, description="저변동성 카테고리 가중치")
    quality: float = Field(
        default=0.0, ge=0, le=1,
        description="퀄리티 카테고리 가중치(ROE·저부채·FCF, OpenDART 필요)",
    )
    growth: float = Field(
        default=0.0, ge=0, le=1,
        description="성장 카테고리 가중치(영업이익/순이익 YoY·흑자전환, OpenDART 필요)",
    )

    @model_validator(mode="after")
    def _sum_to_one(self):
        total = self.momentum + self.value + self.lowvol + self.quality + self.growth
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                "factor_weights 합은 1.0 이어야 합니다"
                f"(momentum+value+lowvol+quality+growth={total:.4f})."
            )
        return self


class UniverseRule(BaseModel):
    """동적 유니버스 규칙 — 리밸런싱 시점마다 후보풀에서 규칙으로 사전선정.

    고정 종목 리스트의 사후선택(look-ahead) 편향을 제거하기 위한 장치. 지정 시
    후보풀에서 각 리밸런싱일에 그 시점까지의 가격 데이터만으로 상대강도(추세) 상위 pick
    종목을 추린 뒤, 그 부분집합에서만 최종 선정(method 의 top_n)을 수행한다. 미래참조
    방지를 위해 규칙은 종가 패널의 d 시점까지 데이터만 사용한다.

    후보풀은 source 로 정한다:
    - "fixed"(기본): config.universe 를 후보풀로 사용(고정 목록).
    - 지수명(예: "KOSPI200"): 각 리밸런싱 시점의 실제 지수 구성종목을 후보풀로 사용한다
      (시점별 멤버십 = survivorship bias 제거). 이때 config.universe 는 비워도 된다.
    """
    type: Literal["momentum"] = Field(
        default="momentum", description="사전선정 기준. momentum=직전 lookback 거래일 상대강도."
    )
    source: Literal["fixed", "KOSPI200", "KOSPI100", "KRX300"] = Field(
        default="fixed",
        description="후보풀 출처. fixed=config.universe, 지수명=그 시점 실제 구성종목(PIT).",
    )
    lookback: int = Field(
        default=120, ge=20, le=480, description="상대강도 측정 거래일(예: 120≈6개월)"
    )
    pick: int = Field(
        default=20, ge=1, le=100, description="후보풀에서 사전선정할 종목 수(이후 top_n 으로 최종 선정)"
    )
    min_market_cap: int | None = Field(
        default=None, ge=0,
        description="유동성 필터: 후보풀에서 시가총액(억 원)이 이 값 미만인 종목을 제외한다. "
        "각 리밸런싱 시점 시총 기준(PIT). None 이면 필터 없음. 소형주 슬리피지 과대평가를 "
        "방지하는 안전장치(백테스트는 슬리피지를 반영하지 않으므로 대형·유동주로 한정 권장).",
    )


class RebalanceSelection(BaseModel):
    """리밸런싱 종목 선정 규칙.

    - method="momentum": lookback 봉 수익률 상위 top_n 종목 선정
    - method="all": universe 전체 사용(top_n 무시)
    - method="score": 종합점수(멀티팩터, app.services.metrics 와 동일 로직) 상위
      top_n 종목 선정. factor_weights 로 모멘텀/밸류/저변동 가중치를 조정한다.
      실제 점수는 실거래 러너/백테스트가 as_of(직전 확정 영업일) 기준으로
      compute_universe_scores 를 호출해 주입한다(이 스키마는 파라미터만 정의).
    - method="custom": universe 각 종목에 사용자 정의 진입/청산 규칙(entry/exit)을
      독립 적용해, '현재 진입 국면(진입 신호 후 아직 청산 안 됨)'인 종목만 동일비중으로
      편입한다(top_n 무시). 규칙은 리밸런싱 백테스트/실거래가 종가 패널만 사용하므로
      종가 기반 지표(가격 close·SMA·EMA·RSI·MACD·상수)만 허용된다(OHLC/거래량 불가).
    """
    method: Literal["momentum", "all", "score", "custom"] = "momentum"
    lookback: int = Field(default=120, ge=2, le=480, description="모멘텀 측정 봉 수(method=momentum)")
    top_n: int = Field(default=5, ge=1, le=50, description="선정 종목 수(momentum/score)")
    factor_weights: FactorWeights = Field(
        default_factory=FactorWeights, description="종합점수 카테고리 가중치(method=score)"
    )
    # 팩터 중립화(P1-3, method="score" 전용). 각 카테고리 z-score 를 지정 축에 대해
    # 직교화(residualize)해 의도치 않은 스타일 베팅을 제거한다.
    #   none(기본): 중립화 없음(기존 동작).
    #   size: 시가총액(로그) 축에 대해 각 팩터를 중립화 → 팩터가 대형/소형주 베팅으로
    #         변질되는 것을 막는다(순수 팩터 노출). 시가총액(PIT)이 필요하다.
    # 섹터 중립화는 신뢰할 종목→섹터 매핑 소스 확보 시 추가한다(현재 미지원).
    neutralize: Literal["none", "size"] = Field(
        default="none",
        description="팩터 중립화 축(method=score). size=시가총액 중립화(순수 팩터 노출).",
    )
    universe_rule: UniverseRule | None = Field(
        default=None,
        description="동적 유니버스 규칙(method=momentum/score). 지정 시 universe 는 후보풀로 해석된다.",
    )
    entry: ConditionGroup | None = Field(
        default=None, description="편입(매수) 논리식(method=custom). 각 종목에 독립 적용."
    )
    exit: ConditionGroup | None = Field(
        default=None, description="청산(매도) 논리식(method=custom). 각 종목에 독립 적용."
    )

    @field_validator("entry", "exit", mode="before")
    @classmethod
    def _wrap_legacy_list(cls, v):
        """레거시 조건 list(AND) → 그룹 dict 로 정규화(CustomConfig 와 동일 규약)."""
        if isinstance(v, list):
            return {"combinator": "and", "children": v}
        return v


def _iter_group_operands(node: "RuleNode"):
    """조건/그룹 트리를 순회하며 모든 피연산자(Operand)를 yield 한다(검증용)."""
    if isinstance(node, ConditionGroup):
        for child in node.children:
            yield from _iter_group_operands(child)
    else:  # Condition
        yield node.left
        yield node.right


class RegimeFilter(BaseModel):
    """현금화 오버레이(레짐 필터) — 시계열(절대) 모멘텀 기반 하방 방어 장치.

    크로스섹션 선정(종합점수/모멘텀)은 '상대적으로 좋은 종목'을 고를 뿐, 시장 전체가
    무너지는 국면에서 '시장에서 나가라'는 신호는 주지 못한다. 이 필터는 기준지수
    (KOSPI/KOSDAQ)의 종가가 ma_period 이동평균 아래로 내려가면 위험회피(risk-off)로
    보고 신규 매수를 중단하고 보유를 청산해 현금화한다. 지수가 이동평균 위로 회복하면
    다음 리밸런싱일에 정상 선정·매수를 재개한다.

    Faber(2007), Antonacci(Dual Momentum) 등에서 장기 수익률은 크게 훼손하지 않으면서
    최대낙폭(MDD)을 완화하는 효과가 보고된다.
    """
    enabled: bool = Field(default=True, description="오버레이 사용 여부")
    index: Literal["KOSPI", "KOSDAQ"] = Field(
        default="KOSPI", description="추세 판정 기준지수"
    )
    ma_period: int = Field(
        default=200, ge=5, le=400, description="추세 판정 이동평균 기간(거래일)"
    )
    # 히스테리시스(비대칭 밴드). 무상태 rs>=ma 대신 경로 의존 판정으로 박스권 휩쏘를
    # 걸러 잦은 청산·재진입(bull-trap)을 줄인다. 둘 다 0.0(기본)이면 기존 무상태 동작과 동일.
    reentry_buffer_pct: float = Field(
        default=0.0, ge=0, le=0.20,
        description="재진입 버퍼(off→on). 지수가 MA×(1+이 값) 이상으로 회복해야 재진입"
        "(예: 0.02 = MA 2% 상회 시 재진입). 0.0 이면 MA 회복 즉시 재진입.",
    )
    exit_buffer_pct: float = Field(
        default=0.0, ge=0, le=0.20,
        description="청산 버퍼(on→off). 지수가 MA×(1−이 값) 미만으로 하락해야 청산"
        "(예: 0.0 = MA 하회 즉시 청산). 값이 클수록 청산을 늦춰 얕은 조정을 버틴다.",
    )


class RiskLayer(BaseModel):
    """포트폴리오 리스크 레이어(P1-2) — 선정·비중 산정 이후 적용되는 위험 통제 오버레이.

    selection·weighting 으로 목표비중을 산정한 뒤, 체결 직전에 순서대로 적용된다:

      1) 종목 집중 한도(max_position_pct): 단일 종목 목표비중 상한. 초과분은 상한 미만
         종목에 비례 재분배하며(재분배로 다시 상한을 넘으면 반복), 재분배 여력이 없으면
         남는 비중은 현금으로 둔다. 소수 종목 쏠림(idiosyncratic) 리스크를 제한한다.
      2) 변동성 타겟팅(target_vol): 선정 포트폴리오의 최근 실현변동성(vol_lookback 봉,
         공분산 기반 연율)을 추정해 목표변동성/실현변동성 비율로 총투자비중(gross)을
         스케일한다. max_leverage(기본 1.0=차입 없음)로 캡하므로 변동성이 낮아도 100%를
         넘기지 않고, 변동성이 높으면 비중을 줄여 현금을 늘린다(디레버리징 전용).
      3) MDD 킬스위치(mdd_kill_pct): 고점(High-Water Mark) 대비 낙폭이 임계를 넘으면
         전량 청산·현금화하는 파국 방어 서킷브레이커. mdd_rearm_days 거래일 쿨다운 후
         재가동하며(고점 기준선 리셋), 재진입 자체는 기존 레짐 필터 게이팅을 따른다
         (레짐=추세 오버레이, 킬스위치=파국 백스톱으로 역할 분리).

    섹터/집중도 한도 중 '섹터 한도'는 신뢰할 수 있는 종목→섹터 매핑 소스가 확보되면
    추가한다(현재 pykrx 업종 구성종목 API 가 빈 값을 반환해 미구현).
    """
    max_position_pct: float | None = Field(
        default=None, gt=0, le=1,
        description="단일 종목 목표비중 상한(0~1, 예: 0.10=10%). None=미적용. 초과분은 상한 "
        "미만 종목에 비례 재분배, 여력 없으면 현금.",
    )
    target_vol: float | None = Field(
        default=None, gt=0, le=2,
        description="목표 연율 변동성(예: 0.15=15%). None=미적용. 실현변동성이 목표를 넘으면 "
        "총투자비중을 낮춰 디레버리징(max_leverage 상한까지만, 차입 없음).",
    )
    vol_lookback: int = Field(
        default=20, ge=5, le=120, description="변동성 타겟팅용 실현변동성 측정 거래일",
    )
    max_leverage: float = Field(
        default=1.0, ge=0.1, le=1.0,
        description="변동성 타겟팅 총투자비중 상한(≤1.0=차입 없음). 변동성이 낮아도 이 값을 "
        "넘지 않는다.",
    )
    mdd_kill_pct: float | None = Field(
        default=None, gt=0, le=1,
        description="고점 대비 낙폭 킬스위치 임계(예: 0.25=−25%). None=미적용. 초과 시 전량 "
        "청산·현금화.",
    )
    mdd_rearm_days: int = Field(
        default=20, ge=1, le=250,
        description="킬스위치 발동 후 재가동까지 쿨다운 거래일. 경과 후 고점 기준선을 리셋하고 "
        "재진입은 레짐 게이팅을 따른다.",
    )


class RebalanceConfig(BaseModel):
    """주기적 포트폴리오 리밸런싱 전략.

    단일 종목 전략(_BaseConfig)과 달리 다종목 universe 를 운용하며, 주기적으로
    선정 규칙에 따라 목표 비중을 재산정해 드리프트 밴드를 넘는 종목만 매매한다.
    실제 발화·주문은 엔진의 RebalanceRunner 가 담당한다.
    """
    type: Literal["rebalance"] = "rebalance"
    universe: list[str] = Field(
        default_factory=list, max_length=300,
        description="종목코드 목록. selection.method=all/momentum/custom 은 매매 대상,"
        " universe_rule 지정 시(동적 유니버스)는 사전선정용 후보풀로 해석된다."
        " universe_rule.source 가 지수명이면 후보풀을 지수 구성종목에서 얻으므로 비워도 된다.",
    )
    selection: RebalanceSelection = Field(default_factory=RebalanceSelection)
    weighting: Literal["equal", "score", "inverse_vol"] = Field(
        default="equal",
        description="선정 종목 비중 방식. 'score' 는 selection.method='score' 일 때만 허용"
        "(순위 기반 선형 가중 — 1위가 가장 큰 비중, 음수 점수에도 안전). 'inverse_vol'은"
        " selection.method 무관(momentum/score/all/custom 모두)하게 적용 가능하며,"
        " 선정 종목의 연율 변동성(vol_ann) 역수로 배분해(risk-parity 근사) 상하한 캡"
        "(0.5%~15%, engine.rebalance.INVERSE_VOL_MIN/MAX_WEIGHT)을 적용한다. 저변동"
        " 종목이 drift_band 미만 비중이 되면 영원히 미체결될 수 있으니 drift_band 를"
        " 작게(예: 0.02) 설정할 것.",
    )
    cadence: Literal["daily", "weekly", "monthly", "quarterly"] = "monthly"
    # weekly: 0=월~4=금 / monthly·quarterly: 1~28(영업일 보정은 러너가 수행)
    # quarterly: 각 분기(1·4·7·10월)의 rebalance_dom 이후 첫 영업일에 리밸런싱
    rebalance_weekday: int | None = Field(default=None, ge=0, le=4)
    rebalance_dom: int | None = Field(default=None, ge=1, le=28)
    rebalance_time: str = Field(
        default="14:30", description="실행 시각 HH:MM (KST), 장 마감 전 체결 여유"
    )
    initial_fill_immediate: bool = Field(
        default=False,
        description="콜드 스타트 즉시 발화. True 면 전략 첫 실행(마지막 실행 기록 없음·보유"
        " 없음)에 한해 cadence 발화일/시각(rebalance_dom·rebalance_time)을 기다리지 않고"
        " 장중이면 즉시 1회 리밸런싱한다. 이번 주기의 정기 리밸런싱을 대체(스케줄 소비)하며,"
        " 레짐 위험회피 국면이면 발동하지 않는다. 이후 주기는 정상 cadence 를 따른다.",
    )
    drift_band_pct: float = Field(
        default=0.05, ge=0, le=1, description="목표 대비 비중 편차 임계(초과만 매매)"
    )
    regime_filter: RegimeFilter | None = Field(
        default=None,
        description="현금화 오버레이(레짐 필터). None 이면 미적용(항상 100% 투자).",
    )
    risk_layer: RiskLayer | None = Field(
        default=None,
        description="포트폴리오 리스크 레이어(P1-2): 종목 집중 한도·변동성 타겟팅·MDD 킬스위치. "
        "None 이면 미적용.",
    )
    capital: float = Field(default=10_000_000, gt=0, description="이 전략이 운용할 배정 자본")
    fees: float = Field(default=0.00015, ge=0, le=0.01, description="위탁수수료율")
    tax: float = Field(default=0.0020, ge=0, le=0.01, description="증권거래세율(매도 시)")
    # 체결 현실성 모델(P0-1) — _BaseConfig 와 동일 규약.
    #   next_close(기본): 리밸런싱일 t 의 선정은 t 종가까지의 데이터로 하되 체결은 t+1 종가.
    #   same_close: 당일 종가 즉시 체결(구 동작, opt-in).
    fill_mode: Literal["next_close", "same_close"] = Field(
        default="next_close", description="체결 시점: 익일 종가(기본) 또는 당일 종가"
    )
    slippage_bps: float = Field(
        default=5.0, ge=0, le=200, description="편도 슬리피지(bps). 매수 +slip·매도 −slip"
    )
    slippage_vol_scale: float = Field(
        default=0.0, ge=0, le=5, description="변동성 비례 슬리피지 스케일(0=고정 bps)"
    )
    # 벤치마크 상대성과(alpha·beta·IR)용 지수. 위험조정지표 무위험수익률(연).
    benchmark_index: Literal["KOSPI200", "KOSPI", "KOSDAQ"] = Field(
        default="KOSPI200", description="벤치마크 상대성과 산출용 지수"
    )
    risk_free_rate: float = Field(
        default=0.0, ge=0, le=0.2, description="위험조정지표용 무위험수익률(연)"
    )

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
        rule = self.selection.universe_rule
        pit = rule is not None and rule.source != "fixed"
        if not self.universe and not pit:
            raise ValueError("universe 가 비어 있습니다(지수 소스가 아니면 종목 목록 필수).")
        if self.selection.method in ("momentum", "score"):
            if rule is not None:
                # 동적 유니버스: top_n≤pick. 후보풀이 지수(PIT)면 크기가 가변이라
                # pick≤풀크기 검사는 fixed(config.universe) 일 때만 적용한다.
                if not pit and rule.pick > len(self.universe):
                    raise ValueError("universe_rule.pick 은 universe(후보풀) 종목 수 이하여야 합니다.")
                if self.selection.top_n > rule.pick:
                    raise ValueError("selection.top_n 은 universe_rule.pick 이하여야 합니다.")
            elif self.selection.top_n > len(self.universe):
                raise ValueError("selection.top_n 은 universe 종목 수 이하여야 합니다.")
        if self.weighting == "score" and self.selection.method != "score":
            raise ValueError("weighting='score' 는 selection.method='score' 일 때만 사용할 수 있습니다.")
        # drift_band 가드: 비중 방식·top_n 대비 밴드가 과대하면 목표비중이 밴드 안에 갇혀
        # 진입/청산이 아예 발생하지 않는다(compute_rebalance_orders 의 abs(dev)<=drift_band
        # 스킵). 선정 종목 수가 고정인 momentum/score 에만 적용한다(all/custom 은 가변).
        if self.selection.method in ("momentum", "score") and self.selection.top_n > 0:
            n = self.selection.top_n
            if self.weighting == "score":
                # 순위가중은 상위 집중이 의도 — 최상위 종목(2/(n+1)) 만이라도 진입 가능하면 허용.
                floor = 2.0 / (n + 1)
            else:  # equal / inverse_vol — 광범위 보유 의도(각 ~1/n)
                floor = 1.0 / n
            if self.drift_band_pct >= floor:
                raise ValueError(
                    f"drift_band_pct({self.drift_band_pct})가 과대합니다: 선정 {n}종목·"
                    f"'{self.weighting}' 비중에선 목표비중이 드리프트 밴드({floor:.3f}) 안에 "
                    f"갇혀 매매가 발생하지 않습니다. drift_band_pct 를 {floor:.3f} 미만으로 낮추세요."
                )
        if self.selection.method == "custom":
            if self.selection.entry is None or self.selection.exit is None:
                raise ValueError("selection.method='custom' 은 entry·exit 규칙이 모두 필요합니다.")
            if self.weighting not in ("equal", "inverse_vol"):
                raise ValueError(
                    "selection.method='custom' 은 weighting='equal' 또는 'inverse_vol' 만 지원합니다."
                )
            # 리밸런싱 백테스트/실거래는 종가 패널만 사용하므로 종가 기반 지표만 허용한다.
            ohlc = {"open", "high", "low", "volume"}
            for grp in (self.selection.entry, self.selection.exit):
                for op in _iter_group_operands(grp):
                    if op.kind == "price" and op.source in ohlc:
                        raise ValueError(
                            "리밸런싱 커스텀 규칙은 종가 기반 지표만 사용할 수 있습니다"
                            "(open/high/low/volume 불가)."
                        )
        # 리스크 레이어(P1-2) 집중 한도 가드: 상한이 드리프트 밴드 이하이면 상한으로 눌린
        # 목표비중이 밴드 안에 갇혀 진입 자체가 발생하지 않는다(현금서 dev=cap≤band → 스킵).
        if self.risk_layer is not None and self.risk_layer.max_position_pct is not None:
            if self.risk_layer.max_position_pct <= self.drift_band_pct:
                raise ValueError(
                    f"risk_layer.max_position_pct({self.risk_layer.max_position_pct})는 "
                    f"drift_band_pct({self.drift_band_pct})보다 커야 합니다: 상한이 드리프트 밴드 "
                    "이하이면 목표비중이 밴드 안에 갇혀 매매가 발생하지 않습니다."
                )
        if self.cadence == "weekly" and self.rebalance_weekday is None:
            self.rebalance_weekday = 0  # 기본: 월요일
        if self.cadence in ("monthly", "quarterly") and self.rebalance_dom is None:
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
