"""전략 신호 생성 — 백테스트와 실시간 매매 엔진이 공유하는 순수 로직.

동일한 신호 함수를 양쪽에서 써서 백테스트와 실거래의 일관성을 보장한다.

전략 유형(config["type"])별 진입(entries)·청산(exits) bool Series 를 생성한다.
모든 신호는 '크로스오버 이벤트'(조건이 거짓→참으로 바뀌는 순간)로 만들어
연속 봉에서 동일 신호가 중복 발생하지 않도록 한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ─────────────────────────── 지표 헬퍼 ───────────────────────────


def _sma(close: pd.Series, n: int) -> pd.Series:
    """단순이동평균."""
    return close.rolling(window=n, min_periods=n).mean()


def _ema(close: pd.Series, n: int) -> pd.Series:
    """지수이동평균(adjust=False — 재귀식, 실시간과 일관)."""
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI(0~100). 평균이득/평균손실을 Wilder 평활(ewm alpha=1/period)로 계산."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    # avg_loss==0 이면 rs=inf → rsi=100. avg_gain==0 이면 rsi=0.
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return rsi


def _macd(
    close: pd.Series, fast: int, slow: int, signal: int
) -> tuple[pd.Series, pd.Series]:
    """MACD 선과 시그널 선을 반환."""
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line, signal_line


def _cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    """a 가 b 를 상향 돌파하는 순간(직전 봉 a<=b, 현재 a>b)."""
    prev_a, prev_b = a.shift(1), b.shift(1)
    return ((a > b) & (prev_a <= prev_b)).fillna(False)


def _cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    """a 가 b 를 하향 돌파하는 순간(직전 봉 a>=b, 현재 a<b)."""
    prev_a, prev_b = a.shift(1), b.shift(1)
    return ((a < b) & (prev_a >= prev_b)).fillna(False)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    """Wilder ATR. TR=max(H-L, |H-PrevClose|, |L-PrevClose|), ATR=Wilder평활(alpha=1/n).

    당일 봉(H,L,Close)만 사용하므로 미래참조 없음(t 시점 ATR 은 t 종가 확정 후 계산).
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


# ─────────────────────────── OHLC 입력 헬퍼 ───────────────────────────


def _as_close(data) -> pd.Series:
    """입력이 DataFrame(OHLCV)이면 'close' 컬럼을, Series 면 그대로 반환.

    close-only 빌더 9종은 이 헬퍼로 추출된 종가 Series 만 받으므로 본문 무수정.
    """
    if isinstance(data, pd.DataFrame):
        return data["close"]
    return data


def _require_ohlc(data, cols) -> None:
    """OHLC 전략 빌더 진입 가드: DataFrame 이며 필요한 컬럼을 갖췄는지 검증."""
    if not isinstance(data, pd.DataFrame):
        raise ValueError("이 전략은 OHLC DataFrame 입력이 필요합니다(종가 단일 시계열 불가).")
    missing = [c for c in cols if c not in data.columns]
    if missing:
        raise ValueError(f"OHLC 컬럼 누락: {missing}")


#: OHLC(고/저/시가/거래량)가 필요한 전략 유형 집합(데이터 로더 분기에 사용).
OHLC_TYPES = frozenset(
    {"atr_trailing", "volatility_breakout", "keltner", "stochastic", "obv_trend"}
)


def requires_ohlc(config: dict) -> bool:
    """해당 전략이 OHLC 입력을 요구하는지 여부(백테스트/실시간 경로 분기용)."""
    stype = config.get("type", "sma_crossover")
    if stype == "custom":
        return _custom_requires_ohlc(config)
    return stype in OHLC_TYPES


# ─────────────────────── 유형별 신호 생성기 ───────────────────────


def _ma_crossover_signals(
    close: pd.Series, fast: int, slow: int, ma
) -> tuple[pd.Series, pd.Series]:
    """이동평균 골든/데드 크로스. ma 는 _sma 또는 _ema."""
    if fast >= slow:
        raise ValueError("fast 기간은 slow 기간보다 작아야 합니다.")
    fast_ma, slow_ma = ma(close, fast), ma(close, slow)
    return _cross_up(fast_ma, slow_ma), _cross_down(fast_ma, slow_ma)


def sma_crossover_signals(
    close: pd.Series, fast: int, slow: int
) -> tuple[pd.Series, pd.Series]:
    """SMA 골든/데드 크로스 진입·청산 신호(하위호환 유지).

    반환: (entries, exits) — 각 시점의 bool Series.
      entries: fast SMA 가 slow SMA 를 상향 돌파(골든크로스) → 매수
      exits:   fast SMA 가 slow SMA 를 하향 돌파(데드크로스) → 매도
    """
    return _ma_crossover_signals(close, fast, slow, _sma)


def _sma_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    return _ma_crossover_signals(
        close, int(cfg.get("fast", 5)), int(cfg.get("slow", 20)), _sma
    )


def _ema_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    return _ma_crossover_signals(
        close, int(cfg.get("fast", 12)), int(cfg.get("slow", 26)), _ema
    )


def _rsi_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """RSI 가 lower 를 상향 복귀=매수, upper 를 하향 이탈=매도."""
    period = int(cfg.get("period", 14))
    lower = float(cfg.get("lower", 30))
    upper = float(cfg.get("upper", 70))
    if lower >= upper:
        raise ValueError("RSI lower 는 upper 보다 작아야 합니다.")
    rsi = _rsi(close, period)
    lower_s = pd.Series(lower, index=close.index)
    upper_s = pd.Series(upper, index=close.index)
    entries = _cross_up(rsi, lower_s)
    exits = _cross_down(rsi, upper_s)
    return entries, exits


def _macd_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """MACD 선이 시그널 선을 상향=매수, 하향=매도."""
    fast = int(cfg.get("fast", 12))
    slow = int(cfg.get("slow", 26))
    signal = int(cfg.get("signal", 9))
    if fast >= slow:
        raise ValueError("MACD fast 는 slow 보다 작아야 합니다.")
    macd_line, signal_line = _macd(close, fast, slow, signal)
    return _cross_up(macd_line, signal_line), _cross_down(macd_line, signal_line)


def _bollinger_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """평균회귀: 종가가 하단밴드 상향 복귀=매수, 상단밴드 하향 이탈=매도."""
    period = int(cfg.get("period", 20))
    num_std = float(cfg.get("num_std", 2.0))
    mid = _sma(close, period)
    std = close.rolling(window=period, min_periods=period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    entries = _cross_up(close, lower)
    exits = _cross_down(close, upper)
    return entries, exits


def _breakout_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """Donchian 돌파: 직전 N봉 최고가 상향 돌파=매수, 최저가 하향 돌파=매도.

    당일 종가는 채널 계산에서 제외(shift)해 미래 정보 누설을 방지한다.
    """
    period = int(cfg.get("period", 20))
    upper = close.rolling(window=period, min_periods=period).max().shift(1)
    lower = close.rolling(window=period, min_periods=period).min().shift(1)
    prev_close = close.shift(1)
    entries = ((close > upper) & (prev_close <= upper)).fillna(False)
    exits = ((close < lower) & (prev_close >= lower)).fillna(False)
    return entries, exits


def _momentum_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """절대모멘텀(타임시리즈 모멘텀): 과거 L봉 수익률 부호 전환을 매매한다.

    mom(t) = close(t)/close(t-L) - 1.
    진입(매수): mom 이 0 을 상향 돌파(추세 양전), 청산(매도): 0 을 하향 돌파(음전).
    """
    lookback = int(cfg.get("lookback", 120))
    if lookback < 1:
        raise ValueError("모멘텀 lookback 은 1 이상이어야 합니다.")
    mom = close / close.shift(lookback) - 1.0
    zero = pd.Series(0.0, index=close.index)
    return _cross_up(mom, zero), _cross_down(mom, zero)


def _zscore_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """평균회귀 z-score: 종가의 이동평균 대비 표준화 편차로 매매.

    z(t) = (close - SMA_n) / std_n.
    진입(매수): z 가 -entry 를 상향 복귀(과도한 저평가 해소 시작),
    청산(매도): z 가 0 을 상향 돌파(평균 회귀 완료).
    """
    period = int(cfg.get("period", 20))
    entry = float(cfg.get("entry", 2.0))
    if entry <= 0:
        raise ValueError("z-score entry 임계는 0 보다 커야 합니다.")
    mid = _sma(close, period)
    std = close.rolling(window=period, min_periods=period).std()
    z = (close - mid) / std
    lower = pd.Series(-entry, index=close.index)
    zero = pd.Series(0.0, index=close.index)
    return _cross_up(z, lower), _cross_up(z, zero)


def _donchian_squeeze_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """변동성 스퀴즈 돌파: 일간 변화의 표준편차(볼린저) < 평균절대값(켈트너)인 '스퀴즈'
    해제 시 추세 방향으로 진입.

    r = close - close.shift(1)                      # 일간 변화(1차 차분)
    sigma_bb = rolling_std_n(r, ddof=0)             # 일간 변화의 표준편차
    sigma_kc = mean(|r|, n)                          # 일간 변화의 평균절대값
    squeeze_on = bb_mult·sigma_bb < kc_mult·sigma_kc
              ⇔ std(r)/mean|r| < kc_mult/bb_mult     # 분포 형태(꼬리)로 결정
    진입(매수): squeeze 해제(직전 봉 squeeze_on, 당일 미해당) 순간 & 중심선 위(close>mid)
    청산(매도): close 가 중심선(mid=SMA_n(close))을 하향 돌파

    두 척도를 모두 '일간 변화' 기반으로 통일해 스케일을 맞춘다(과거 레벨 std 와 차분
    평균 혼용 시 스케일이 √n 배 어긋나 스퀴즈가 절대 안 켜지는 문제를 해소). 스퀴즈 ON =
    변동이 고르고 작은 상태, OFF = 큰 변화/점프가 섞여 std 가 평균절대값보다 커진 상태.

    미래참조 처리: r·sigma_bb·sigma_kc·mid 모두 당일 종가 확정 후 산출(또는 과거 shift
    참조)되어 t 종가 체결과 정합적이다.
    """
    period = int(cfg.get("period", 20))
    bb_mult = float(cfg.get("bb_mult", 1.0))
    kc_mult = float(cfg.get("kc_mult", 1.3))
    if period < 3:
        raise ValueError("Donchian 스퀴즈 period 는 3 이상이어야 합니다.")
    if bb_mult <= 0 or kc_mult <= 0:
        raise ValueError("Donchian 스퀴즈 bb_mult/kc_mult 는 0 보다 커야 합니다.")

    r = close.diff()
    sigma_bb = r.rolling(window=period, min_periods=period).std(ddof=0)
    sigma_kc = r.abs().rolling(window=period, min_periods=period).mean()
    squeeze_on = bb_mult * sigma_bb < kc_mult * sigma_kc

    mid = _sma(close, period)
    released = (squeeze_on.shift(1).fillna(False)) & (~squeeze_on.fillna(False))
    entries = (released & (close > mid)).fillna(False)
    exits = _cross_down(close, mid)
    exits = exits & ~entries
    return entries, exits


def _trix_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """TRIX 삼중지수평활 모멘텀: TRIX 가 시그널선을 교차할 때 매매.

    ema1 = EMA_n(close), ema2 = EMA_n(ema1), ema3 = EMA_n(ema2)
    trix = (ema3 - ema3.shift(1)) / ema3.shift(1) * 100
    signal_line = EMA_s(trix)
    진입(매수): trix 가 signal_line 을 상향 돌파, 청산(매도): 하향 돌파.

    미래참조 처리: 모든 EMA·TRIX 는 당일 종가 확정 후 산출(재귀식, adjust=False).
    """
    period = int(cfg.get("period", 15))
    signal_period = int(cfg.get("signal_period", 9))
    if period < 1 or signal_period < 1:
        raise ValueError("TRIX period/signal_period 는 1 이상이어야 합니다.")

    ema1 = _ema(close, period)
    ema2 = _ema(ema1, period)
    ema3 = _ema(ema2, period)
    prev_ema3 = ema3.shift(1)
    trix = (ema3 - prev_ema3) / prev_ema3 * 100.0
    signal_line = trix.ewm(span=signal_period, adjust=False, min_periods=signal_period).mean()
    return _cross_up(trix, signal_line), _cross_down(trix, signal_line)


def _disparity_signals(close: pd.Series, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """이격도(Disparity): 종가/이동평균 비율(%)로 평균회귀 매매.

    disp(t) = 100 * close / SMA_n.
    진입(매수): disp 가 lower 를 상향 복귀(과도한 이격 해소),
    청산(매도): disp 가 upper 를 하향 이탈(과열 해소).
    """
    period = int(cfg.get("period", 20))
    lower = float(cfg.get("lower", 95.0))
    upper = float(cfg.get("upper", 105.0))
    if lower >= upper:
        raise ValueError("이격도 lower 는 upper 보다 작아야 합니다.")
    disp = 100.0 * close / _sma(close, period)
    lower_s = pd.Series(lower, index=close.index)
    upper_s = pd.Series(upper, index=close.index)
    return _cross_up(disp, lower_s), _cross_down(disp, upper_s)


# ───────────────────── OHLC 기반 신호 생성기 ─────────────────────


def _atr_trailing_signals(data, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """ATR 트레일링 / 샹들리에 청산 (strategies.md 3.1).

    진입: Donchian 돌파 — 직전 N봉 최고가(High) 상향 돌파.
      Upper = rolling_max(High, period).shift(1)  # 당일봉 제외(미래참조 방지)
      진입 = close(t) 가 직전 채널 상단을 상향 돌파.
    청산: 샹들리에 — ChandelierExit(t) = max(High, n봉) - k·ATR_n.
      종가가 트레일링 청산선을 하향 돌파하면 청산.

    체결 가정: 신호봉 t 의 종가로 체결(엔진과 일치). 채널 상단은 shift(1) 로
    당일 고가를 제외하므로, t 시점에 사용 가능한 정보(t-1까지 채널 + t 종가)만 사용.
    샹들리에선은 당일 고가·ATR(당일 종가 확정 후 산출)을 포함하며, 종가 확정 시점에
    동시에 평가되므로 미래참조가 아니다.
    """
    _require_ohlc(data, ("high", "low", "close"))
    high, low, close = data["high"], data["low"], data["close"]
    period = int(cfg.get("period", 22))
    atr_period = int(cfg.get("atr_period", 22))
    k = float(cfg.get("k", 3.0))
    if period < 1 or atr_period < 1:
        raise ValueError("ATR 트레일링 period/atr_period 는 1 이상이어야 합니다.")
    if k <= 0:
        raise ValueError("ATR 트레일링 k 는 0 보다 커야 합니다.")

    upper = high.rolling(window=period, min_periods=period).max().shift(1)
    prev_close = close.shift(1)
    entries = ((close > upper) & (prev_close <= upper)).fillna(False)

    atr = _atr(high, low, close, atr_period)
    highest = high.rolling(window=period, min_periods=period).max()
    chandelier = highest - k * atr
    exits = _cross_down(close, chandelier)
    return entries, exits


def _volatility_breakout_signals(data, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """변동성 돌파(래리 윌리엄스) (strategies.md 3.2).

    Range(t-1) = High(t-1) - Low(t-1)
    Target(t)  = Open(t) + k·Range(t-1)
    진입: 당일 고가 High(t) >= Target(t)  (장중 목표가 돌파 → 매수)
    청산: 일봉 백테스트에서는 '당일 종가 청산'으로 가정(데이트레이딩).
      → 진입한 봉의 종가에서 청산. 단, 크로스오버 규약상 동일 봉 진입·청산 동시
        발생을 금지하므로, 청산은 '직전 봉에서 진입했던' 봉의 종가에 발생시킨다
        (entries.shift(1)). 즉 t 진입 → t 종가 보유 → t+1 시작 시 청산 신호.

    미래참조 처리: Range 는 전일(t-1) 고저만 사용(shift(1)), Open(t) 는 당일 시가로
    장 시작 시점에 확정. Target 은 장중 내내 고정. High(t) >= Target 판정은 당일
    장중 정보지만 일봉 백테스트에서는 봉 종료 후 평가되므로 t 종가 체결과 정합적이다.
    """
    _require_ohlc(data, ("open", "high", "low", "close"))
    open_, high, low = data["open"], data["high"], data["low"]
    k = float(cfg.get("k", 0.5))
    if k <= 0:
        raise ValueError("변동성 돌파 k 는 0 보다 커야 합니다.")

    prev_range = (high.shift(1) - low.shift(1))
    target = open_ + k * prev_range
    entries = (high >= target).fillna(False)
    # 전일 range NaN(첫 봉) 구간 신호 제거
    entries = entries.where(prev_range.notna(), False)
    # 당일 종가 청산: 진입 다음 봉에서 청산(동일 봉 동시신호 금지 규약 준수)
    exits = entries.shift(1).fillna(False).astype(bool)
    # 진입과 청산이 같은 봉에 겹치면(연속 진입) 진입을 우선해 청산 제거
    exits = exits & ~entries
    return entries, exits


def _keltner_signals(data, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """켈트너 채널 (strategies.md 3.6).

    Mid   = EMA_n(close)
    Upper = Mid + m·ATR_p,  Lower = Mid - m·ATR_p
    진입: close 가 Upper 를 상향 돌파(돌파 추종)
    청산: close 가 Mid(중심선)를 하향 돌파

    미래참조 처리: Mid/ATR 모두 당일 종가·고저 포함이며 t 종가 확정 후 산출되어
    같은 t 종가 체결과 정합적(돌파 전략 breakout 과 동일한 당일-종가 규약).
    """
    _require_ohlc(data, ("high", "low", "close"))
    high, low, close = data["high"], data["low"], data["close"]
    ema_period = int(cfg.get("ema_period", 20))
    atr_period = int(cfg.get("atr_period", 10))
    mult = float(cfg.get("mult", 2.0))
    if ema_period < 1 or atr_period < 1:
        raise ValueError("켈트너 ema_period/atr_period 는 1 이상이어야 합니다.")
    if mult <= 0:
        raise ValueError("켈트너 mult 는 0 보다 커야 합니다.")

    mid = _ema(close, ema_period)
    atr = _atr(high, low, close, atr_period)
    upper = mid + mult * atr
    entries = _cross_up(close, upper)
    exits = _cross_down(close, mid)
    return entries, exits


def _stochastic_signals(data, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """스토캐스틱 (strategies.md 3.7).

    %K = 100·(close - LowestLow_n)/(HighestHigh_n - LowestLow_n)
    %D = SMA(%K, d)
    진입: 과매도(%K < lower)에서 %K 가 %D 를 상향 교차
    청산: 과매수(%K > upper)에서 %K 가 %D 를 하향 교차

    미래참조 처리: LowestLow/HighestHigh 는 당일 봉 포함 rolling(당일 고저 포함),
    %K·%D 모두 t 종가 확정 후 산출되어 t 종가 체결과 정합적.
    """
    _require_ohlc(data, ("high", "low", "close"))
    high, low, close = data["high"], data["low"], data["close"]
    k_period = int(cfg.get("k_period", 14))
    d_period = int(cfg.get("d_period", 3))
    lower = float(cfg.get("lower", 20.0))
    upper = float(cfg.get("upper", 80.0))
    if k_period < 1 or d_period < 1:
        raise ValueError("스토캐스틱 k_period/d_period 는 1 이상이어야 합니다.")
    if lower >= upper:
        raise ValueError("스토캐스틱 lower 는 upper 보다 작아야 합니다.")

    lowest = low.rolling(window=k_period, min_periods=k_period).min()
    highest = high.rolling(window=k_period, min_periods=k_period).max()
    rng = highest - lowest
    pct_k = 100.0 * (close - lowest) / rng
    # 고저 동일(rng=0) 구간은 중립 50 으로(0/0 방지)
    pct_k = pct_k.where(rng != 0, 50.0)
    pct_d = pct_k.rolling(window=d_period, min_periods=d_period).mean()

    cross_up = _cross_up(pct_k, pct_d)
    cross_down = _cross_down(pct_k, pct_d)
    # 과매도 영역에서의 상향교차만 진입, 과매수 영역에서의 하향교차만 청산
    entries = (cross_up & (pct_k < lower)).fillna(False)
    exits = (cross_down & (pct_k > upper)).fillna(False)
    return entries, exits


def _obv_trend_signals(data, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """OBV(On-Balance Volume) 거래량 추세 확인: OBV 가 자기 이동평균을 교차할 때 매매.

    sign = sign(close - close.shift(1))  # 상승+1/하락-1/보합0
    obv = (sign * volume).fillna(0).cumsum()
    obv_ma = SMA_n(obv)
    진입(매수): obv 가 obv_ma 를 상향 돌파, 청산(매도): 하향 돌파.

    유일하게 거래량(volume)을 사용하는 전략. 당일 거래량·종가는 마감 시 확정되므로
    당일 종가 체결과 정합적이며 미래참조가 없다.
    """
    _require_ohlc(data, ("close", "volume"))
    close, volume = data["close"], data["volume"]
    period = int(cfg.get("period", 20))
    if period < 2:
        raise ValueError("OBV 추세 period 는 2 이상이어야 합니다.")

    sign = pd.Series(np.sign(close.diff().to_numpy()), index=close.index)
    obv = (sign * volume).fillna(0.0).cumsum()
    obv_ma = _sma(obv, period)
    return _cross_up(obv, obv_ma), _cross_down(obv, obv_ma)


# ───────────────── 사용자 정의(비주얼 룰 빌더) 신호 생성기 ─────────────────


def _operand_series(op: dict, data) -> pd.Series:
    """규칙 피연산자(operand)를 시계열 Series 로 평가한다.

    지표는 모두 기존 헬퍼(_sma/_ema/_rsi/_macd)를 재사용하며 종가 기반으로 계산한다.
    price 의 open/high/low/volume 은 OHLCV DataFrame 입력을 요구한다.
    """
    kind = op.get("kind")
    close = _as_close(data)
    if kind == "const":
        return pd.Series(float(op["value"]), index=close.index)
    if kind == "price":
        source = op.get("source", "close")
        if source == "close":
            return close
        _require_ohlc(data, (source,))
        return data[source]
    if kind == "sma":
        return _sma(close, int(op["period"]))
    if kind == "ema":
        return _ema(close, int(op["period"]))
    if kind == "rsi":
        return _rsi(close, int(op["period"]))
    if kind in ("macd_line", "macd_signal"):
        macd_line, signal_line = _macd(
            close, int(op["fast"]), int(op["slow"]), int(op["signal"])
        )
        return macd_line if kind == "macd_line" else signal_line
    raise ValueError(f"알 수 없는 피연산자 종류: {kind}")


def _eval_condition(cond: dict, data) -> tuple[pd.Series, pd.Series]:
    """단일 조건을 (상태 bool, 유효성 bool) Series 쌍으로 평가한다.

    상태: 비교 결과(NaN 비교는 False). 유효성: 좌·우 피연산자가 모두 non-NaN 인지.
    유효성은 지표 워밍업(NaN) 구간에서의 가짜 크로스 신호를 막는 데 쓴다.
    """
    left = _operand_series(cond["left"], data)
    right = _operand_series(cond["right"], data)
    op = cond["op"]
    if op == ">":
        state = left > right
    elif op == "<":
        state = left < right
    elif op == ">=":
        state = left >= right
    elif op == "<=":
        state = left <= right
    else:
        raise ValueError(f"알 수 없는 비교 연산자: {op}")
    valid = left.notna() & right.notna()
    return state.fillna(False), valid


def _as_group(node) -> dict:
    """규칙 노드를 그룹 dict 로 정규화한다.

    하위호환: 레거시 조건 list 는 AND 그룹으로 변환한다(combinator='and').
    """
    if isinstance(node, list):
        return {"combinator": "and", "children": node}
    return node


def _iter_conditions(node):
    """규칙 노드(list/그룹/단일 조건) 트리를 순회하며 잎 조건(dict)을 모두 yield."""
    if not node:
        return
    if isinstance(node, list):
        for child in node:
            yield from _iter_conditions(child)
    elif isinstance(node, dict) and "children" in node:
        for child in node["children"]:
            yield from _iter_conditions(child)
    else:
        yield node


def _eval_node(node, data) -> tuple[pd.Series, pd.Series]:
    """규칙 노드(단일 조건 또는 AND/OR 그룹)를 (상태, 유효성) Series 쌍으로 평가한다.

    그룹은 children 을 combinator(and/or)로 결합한다(중첩 가능). 유효성은 결합 방식과
    무관하게 트리 내 모든 피연산자가 non-NaN 인지(AND)로 판정해, 워밍업(NaN) 구간의
    가짜 상승 에지를 막는다(빌트인 _cross_up 의 직전봉 유효성 규약과 동일 취지).
    """
    if isinstance(node, dict) and "children" in node:
        index = _as_close(data).index
        is_or = node.get("combinator", "and") == "or"
        combined_state = pd.Series(is_or is False, index=index)  # OR→False, AND→True 시작
        combined_valid = pd.Series(True, index=index)
        for child in node["children"]:
            state, valid = _eval_node(child, data)
            combined_state = combined_state | state if is_or else combined_state & state
            combined_valid &= valid
        return combined_state, combined_valid
    return _eval_condition(node, data)


def _rising_edge(state: pd.Series, valid: pd.Series) -> pd.Series:
    """상태가 거짓→참으로 바뀌는 순간을 이벤트로 반환한다.

    직전 봉이 '유효'(모든 피연산자 non-NaN)했을 때만 에지를 인정해, 워밍업 구간에서
    NaN→True 전환을 가짜 크로스로 오인하지 않도록 한다(빌트인 _cross_up 과 동일 의미).
    """
    # shift(fill_value=False) 로 bool dtype 유지(object 가 되면 ~ 가 비트 NOT 으로 오작동).
    prev_state = state.shift(1, fill_value=False)
    prev_valid = valid.shift(1, fill_value=False)
    return state & ~prev_state & prev_valid


def _custom_signals(data, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """사용자 정의 전략: 진입/청산 논리식(AND/OR 그룹)의 상승 에지를 매매 신호로 변환한다.

    entry_state = 진입 논리식 평가, exit_state = 청산 논리식 평가(각각 중첩 AND/OR 트리).
    entries = entry_state 가 거짓→참으로 바뀌는 순간, exits 도 동일(상승 에지).
    동일 봉에서 진입·청산이 겹치면 진입을 우선해 청산을 제거한다.
    단일 '>' 조건은 자연히 상향 돌파(골든크로스) 순간이 된다.
    """
    entry_node = cfg.get("entry")
    exit_node = cfg.get("exit")
    if next(_iter_conditions(entry_node), None) is None or next(
        _iter_conditions(exit_node), None
    ) is None:
        raise ValueError("사용자 정의 전략은 진입·청산 조건이 각각 1개 이상 필요합니다.")

    entry_state, entry_valid = _eval_node(_as_group(entry_node), data)
    exit_state, exit_valid = _eval_node(_as_group(exit_node), data)
    entries = _rising_edge(entry_state, entry_valid)
    exits = _rising_edge(exit_state, exit_valid)
    exits = exits & ~entries
    return entries, exits


def _custom_operands(cfg: dict):
    """custom config(레거시 list 또는 AND/OR 그룹)의 모든 피연산자를 순회 제너레이터로 반환."""
    for cond in _iter_conditions(cfg.get("entry")):
        yield cond["left"]
        yield cond["right"]
    for cond in _iter_conditions(cfg.get("exit")):
        yield cond["left"]
        yield cond["right"]


def _custom_requires_ohlc(cfg: dict) -> bool:
    """custom 전략이 종가 외 OHLC/거래량을 참조하면 True."""
    return any(
        op.get("kind") == "price" and op.get("source") in ("open", "high", "low", "volume")
        for op in _custom_operands(cfg)
    )


def _custom_min_periods(cfg: dict) -> int:
    """custom 전략 신호에 필요한 최소 봉 수(가장 긴 지표 기간 + 1, 최소 2)."""
    longest = 1
    for op in _custom_operands(cfg):
        kind = op.get("kind")
        if kind in ("sma", "ema", "rsi"):
            longest = max(longest, int(op.get("period", 1)))
        elif kind in ("macd_line", "macd_signal"):
            longest = max(longest, int(op.get("slow", 1)) + int(op.get("signal", 1)))
    return max(2, longest + 1)


# ─────────────────────────── 디스패처 ───────────────────────────

#: close-only 빌더(종가 Series 만 받음). 디스패처가 _as_close 로 추출해 전달.
_CLOSE_BUILDERS = {
    "sma_crossover": _sma_signals,
    "ema_crossover": _ema_signals,
    "rsi": _rsi_signals,
    "macd": _macd_signals,
    "bollinger": _bollinger_signals,
    "breakout": _breakout_signals,
    "momentum": _momentum_signals,
    "zscore": _zscore_signals,
    "disparity": _disparity_signals,
    "donchian_squeeze": _donchian_squeeze_signals,
    "trix": _trix_signals,
}

#: OHLC 빌더(OHLCV DataFrame 을 받음).
_OHLC_BUILDERS = {
    "atr_trailing": _atr_trailing_signals,
    "volatility_breakout": _volatility_breakout_signals,
    "keltner": _keltner_signals,
    "stochastic": _stochastic_signals,
    "obv_trend": _obv_trend_signals,
}

# 하위호환: 기존 이름 유지(close-only + OHLC 통합 뷰).
_SIGNAL_BUILDERS = {**_CLOSE_BUILDERS, **_OHLC_BUILDERS}


def generate_signals(data, config: dict) -> tuple[pd.Series, pd.Series]:
    """전략 유형(config["type"])에 따라 (entries, exits) 신호를 생성한다.

    :param data: 종가 Series(close-only 전략) 또는 OHLCV DataFrame(OHLC 전략).
        close-only 전략은 DataFrame 을 받아도 'close' 컬럼만 사용(하위호환).
    :raises ValueError: 알 수 없는 전략 유형이거나 파라미터가 유효하지 않을 때
    """
    stype = config.get("type", "sma_crossover")
    if stype == "custom":
        return _custom_signals(data, config)
    if stype in _CLOSE_BUILDERS:
        return _CLOSE_BUILDERS[stype](_as_close(data), config)
    builder = _OHLC_BUILDERS.get(stype)
    if builder is None:
        raise ValueError(f"알 수 없는 전략 유형: {stype}")
    return builder(data, config)


def min_periods(config: dict) -> int:
    """신호 계산에 필요한 최소 봉 수(시드 길이·데이터 충분성 검사용)."""
    stype = config.get("type", "sma_crossover")
    if stype == "custom":
        return _custom_min_periods(config)
    if stype in ("sma_crossover", "ema_crossover"):
        return int(config.get("slow", 20))
    if stype == "rsi":
        return int(config.get("period", 14)) + 1
    if stype == "macd":
        return int(config.get("slow", 26)) + int(config.get("signal", 9))
    if stype == "bollinger":
        return int(config.get("period", 20))
    if stype == "breakout":
        return int(config.get("period", 20)) + 1
    if stype == "momentum":
        return int(config.get("lookback", 120)) + 1
    if stype == "zscore":
        return int(config.get("period", 20))
    if stype == "disparity":
        return int(config.get("period", 20))
    if stype == "donchian_squeeze":
        # 차분(1봉) + rolling_n + squeeze_on.shift(1) 여유
        return int(config.get("period", 20)) + 2
    if stype == "trix":
        return 3 * int(config.get("period", 15)) + int(config.get("signal_period", 9))
    if stype == "atr_trailing":
        # 채널(period, shift1)·ATR(atr_period) 중 큰 쪽 + shift 여유
        return max(int(config.get("period", 22)), int(config.get("atr_period", 22))) + 1
    if stype == "volatility_breakout":
        # 전일 range(shift1) 만 필요 → 2봉이면 첫 신호 가능
        return 2
    if stype == "keltner":
        return max(int(config.get("ema_period", 20)), int(config.get("atr_period", 10)))
    if stype == "stochastic":
        return int(config.get("k_period", 14)) + int(config.get("d_period", 3))
    if stype == "obv_trend":
        return int(config.get("period", 20)) + 1
    raise ValueError(f"알 수 없는 전략 유형: {stype}")


def latest_signal(data, config: dict) -> str | None:
    """가장 최근 봉의 신호를 반환: 'buy' | 'sell' | None.

    실시간 매매 엔진에서 직전 봉 기준 신호 판단에 사용.

    :param data: 종가 Series(close-only) 또는 OHLCV DataFrame(OHLC 전략).
    """
    entries, exits = generate_signals(data, config)
    if len(entries) == 0:
        return None
    if bool(entries.iloc[-1]):
        return "buy"
    if bool(exits.iloc[-1]):
        return "sell"
    return None
