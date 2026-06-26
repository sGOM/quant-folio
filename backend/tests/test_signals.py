"""신호 로직 검증 — 백테스트·실거래 공유."""
import numpy as np
import pandas as pd
import pytest

from app.services.backtest.signals import (
    generate_signals,
    latest_signal,
    min_periods,
    requires_ohlc,
    sma_crossover_signals,
)


def _series(values):
    return pd.Series(
        values, index=pd.date_range("2024-01-01", periods=len(values), freq="D")
    )


def _ohlc(close_values, spread=1.0):
    """종가 리스트로 단순 OHLCV DataFrame 을 만든다.

    high=close+spread, low=close-spread, open=직전 종가(첫 봉은 close).
    추세·진동 시계열 테스트에 충분한 변동을 만든다.
    """
    close = pd.Series(close_values, dtype=float)
    open_ = close.shift(1).fillna(close)
    high = pd.concat([close, open_], axis=1).max(axis=1) + spread
    low = pd.concat([close, open_], axis=1).min(axis=1) - spread
    idx = pd.date_range("2024-01-01", periods=len(close_values), freq="D")
    return pd.DataFrame(
        {
            "open": open_.values,
            "high": high.values,
            "low": low.values,
            "close": close.values,
            "volume": [1000.0] * len(close_values),
        },
        index=idx,
    )


# ─────────────────────────── SMA(하위호환) ───────────────────────────


# 하락 → 상승(골든크로스) → 하락(데드크로스). 첫 상승 전 충분히 하락시켜
# 골든크로스가 이동평균 워밍업(NaN) 이후에 발생하도록 한다.
_TREND = list(range(50, 10, -1)) + list(range(10, 61)) + list(range(60, 9, -1))


def test_golden_and_dead_cross():
    s = _series(_TREND)
    entries, exits = sma_crossover_signals(s, fast=3, slow=10)
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    # 진입과 청산이 동시에 발생하지 않음
    assert not (entries & exits).any()


def test_fast_must_be_less_than_slow():
    s = _series(list(range(1, 30)))
    with pytest.raises(ValueError):
        sma_crossover_signals(s, fast=20, slow=5)


# ─────────────────────────── 디스패처/유형 ───────────────────────────


def test_unknown_type_raises():
    s = _series(list(range(1, 30)))
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "nonexistent"})


def test_min_periods_per_type():
    assert min_periods({"type": "sma_crossover", "slow": 20}) == 20
    assert min_periods({"type": "ema_crossover", "slow": 26}) == 26
    assert min_periods({"type": "rsi", "period": 14}) == 15
    assert min_periods({"type": "macd", "slow": 26, "signal": 9}) == 35
    assert min_periods({"type": "bollinger", "period": 20}) == 20
    assert min_periods({"type": "breakout", "period": 20}) == 21
    assert min_periods({"type": "momentum", "lookback": 120}) == 121
    assert min_periods({"type": "zscore", "period": 20}) == 20
    assert min_periods({"type": "disparity", "period": 20}) == 20
    assert min_periods({"type": "donchian_squeeze", "period": 20}) == 22
    assert min_periods({"type": "trix", "period": 15, "signal_period": 9}) == 54
    assert min_periods({"type": "atr_trailing", "period": 22, "atr_period": 14}) == 23
    assert min_periods({"type": "volatility_breakout", "k": 0.5}) == 2
    assert min_periods({"type": "keltner", "ema_period": 20, "atr_period": 10}) == 20
    assert min_periods({"type": "stochastic", "k_period": 14, "d_period": 3}) == 17
    assert min_periods({"type": "obv_trend", "period": 20}) == 21


@pytest.mark.parametrize(
    "config",
    [
        {"type": "sma_crossover", "fast": 3, "slow": 10},
        {"type": "ema_crossover", "fast": 5, "slow": 15},
        {"type": "macd", "fast": 5, "slow": 12, "signal": 4},
        {"type": "breakout", "period": 10},
        {"type": "momentum", "lookback": 10},
        {"type": "trix", "period": 5, "signal_period": 3},
    ],
)
def test_trend_following_types_produce_both_signals(config):
    s = _series(_TREND)
    entries, exits = generate_signals(s, config)
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    # 동일 봉에 진입·청산이 동시에 발생하지 않음
    assert not (entries & exits).any()
    # bool Series 이며 인덱스가 보존됨
    assert entries.dtype == bool and exits.dtype == bool
    assert entries.index.equals(s.index)


def test_rsi_signals_on_oscillating_series():
    # 진동 시계열에서 RSI 가 과매도/과매수를 오가며 신호 생성
    osc = (50 + 40 * np.sin(np.linspace(0, 6 * np.pi, 120))).tolist()
    s = _series(osc)
    entries, exits = generate_signals(s, {"type": "rsi", "period": 14, "lower": 30, "upper": 70})
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    assert not (entries & exits).any()


def test_bollinger_signals_on_oscillating_series():
    osc = (100 + 20 * np.sin(np.linspace(0, 8 * np.pi, 160))).tolist()
    s = _series(osc)
    entries, exits = generate_signals(s, {"type": "bollinger", "period": 20, "num_std": 2})
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    assert not (entries & exits).any()


def test_zscore_signals_on_oscillating_series():
    osc = (100 + 15 * np.sin(np.linspace(0, 8 * np.pi, 160))).tolist()
    s = _series(osc)
    entries, exits = generate_signals(s, {"type": "zscore", "period": 20, "entry": 1.0})
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    assert not (entries & exits).any()


def test_disparity_signals_on_oscillating_series():
    osc = (100 + 15 * np.sin(np.linspace(0, 8 * np.pi, 160))).tolist()
    s = _series(osc)
    entries, exits = generate_signals(
        s, {"type": "disparity", "period": 20, "lower": 98, "upper": 102}
    )
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    assert not (entries & exits).any()


def test_donchian_squeeze_signals_on_consolidation_then_breakout():
    # 스퀴즈는 '일간 변화의 분포 형태'(std/mean|Δ|)로 결정된다. 고르고 작은 저변동 구간에서
    # squeeze ON, 큰 점프(추세 가속)가 들어오면 std 가 급증해 OFF→해제→진입.
    rng = np.random.default_rng(0)
    base = 100.0
    vals = [base]
    for _ in range(40):  # 압축: ±0.2% 균일 미세등락 → squeeze ON
        base *= 1 + rng.uniform(-0.002, 0.002)
        vals.append(base)
    for _ in range(10):  # 해제+상승: 연속 +2% 점프 → ON→OFF, close>mid → 매수
        base *= 1.02
        vals.append(base)
    for _ in range(10):  # 하락: 연속 -2% → 중심선 하향 이탈 → 매도
        base *= 0.98
        vals.append(base)
    s = _series(vals)
    entries, exits = generate_signals(s, {"type": "donchian_squeeze", "period": 20})
    assert entries.sum() >= 1  # 스퀴즈 해제 + 상방 → 최소 1회 매수
    assert exits.sum() >= 1  # 하락 구간 중심선 하향 → 매도
    assert not (entries & exits).any()  # 동일 봉 동시 발생 금지
    assert entries.dtype == bool and exits.dtype == bool
    assert entries.index.equals(s.index)


def test_donchian_squeeze_mults_must_be_positive():
    s = _series(_TREND)
    with pytest.raises(ValueError):
        generate_signals(
            s, {"type": "donchian_squeeze", "period": 20, "bb_mult": 0, "kc_mult": 1.3}
        )


def test_donchian_squeeze_period_must_be_at_least_three():
    s = _series(_TREND)
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "donchian_squeeze", "period": 2})


def test_trix_signals_on_oscillating_series():
    osc = (100 + 20 * np.sin(np.linspace(0, 8 * np.pi, 200))).tolist()
    s = _series(osc)
    entries, exits = generate_signals(
        s, {"type": "trix", "period": 10, "signal_period": 5}
    )
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    assert not (entries & exits).any()


def test_trix_period_must_be_positive():
    s = _series(_TREND)
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "trix", "period": 0})


def test_zscore_entry_must_be_positive():
    s = _series(_TREND)
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "zscore", "period": 20, "entry": -1})


def test_disparity_lower_must_be_less_than_upper():
    s = _series(_TREND)
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "disparity", "period": 20, "lower": 110, "upper": 90})


def test_rsi_lower_must_be_less_than_upper():
    s = _series(_TREND)
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "rsi", "period": 14, "lower": 80, "upper": 20})


def test_macd_fast_must_be_less_than_slow():
    s = _series(_TREND)
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "macd", "fast": 26, "slow": 12, "signal": 9})


# ─────────────────────────── latest_signal ───────────────────────────


@pytest.mark.parametrize(
    "config",
    [
        {"type": "sma_crossover", "fast": 3, "slow": 10},
        {"type": "ema_crossover", "fast": 5, "slow": 15},
        {"type": "rsi", "period": 14, "lower": 30, "upper": 70},
        {"type": "macd", "fast": 5, "slow": 12, "signal": 4},
        {"type": "bollinger", "period": 20, "num_std": 2},
        {"type": "breakout", "period": 10},
        {"type": "momentum", "lookback": 10},
        {"type": "zscore", "period": 20, "entry": 2},
        {"type": "disparity", "period": 20, "lower": 95, "upper": 105},
        {"type": "donchian_squeeze", "period": 10, "bb_mult": 2.0, "kc_mult": 1.0},
        {"type": "trix", "period": 10, "signal_period": 5},
    ],
)
def test_latest_signal_values(config):
    s = _series(list(range(1, 80)))
    assert latest_signal(s, config) in ("buy", "sell", None)


def test_breakout_no_lookahead():
    # Donchian 채널은 직전 봉까지만 사용(당일 종가 미사용) → 단조 증가에서도
    # 첫 신호가 채널 형성 이후에만 발생.
    s = _series(list(range(1, 40)))
    entries, exits = generate_signals(s, {"type": "breakout", "period": 10})
    # 단조 상승: 매수 신호가 채널 형성(period) 이후 구간에서 발생
    first_entry = entries.idxmax() if entries.any() else None
    assert first_entry is None or entries.loc[:first_entry].sum() == 1


# ─────────────────────────── OHLC 전략 ───────────────────────────

# 하락 → 상승 → 하락 추세(OHLC 진입·청산 발생용). 상승 구간은 큰 보폭으로 만들어
# 종가가 직전 N봉 고가를 실제로 돌파하도록 한다(Donchian/켈트너 진입 유발).
_OHLC_TREND = (
    list(range(80, 20, -1))
    + list(range(20, 400, 4))   # 보폭 4 > spread → 직전 고가 돌파 가능
    + list(range(400, 19, -4))
)
# 진동(스토캐스틱·평균회귀 청산용).
_OSC = (100 + 40 * np.sin(np.linspace(0, 8 * np.pi, 200))).tolist()


def test_requires_ohlc_flag():
    for t in ("atr_trailing", "volatility_breakout", "keltner", "stochastic", "obv_trend"):
        assert requires_ohlc({"type": t}) is True
    for t in ("sma_crossover", "rsi", "breakout", "momentum", "donchian_squeeze", "trix"):
        assert requires_ohlc({"type": t}) is False


def test_close_only_accepts_dataframe():
    # 하위호환: close-only 전략에 OHLCV DataFrame 을 넘겨도 close 컬럼만 사용해 동작.
    df = _ohlc(_TREND)
    e_df, x_df = generate_signals(df, {"type": "sma_crossover", "fast": 3, "slow": 10})
    e_s, x_s = generate_signals(_series(_TREND), {"type": "sma_crossover", "fast": 3, "slow": 10})
    assert e_df.equals(e_s) and x_df.equals(x_s)


@pytest.mark.parametrize(
    "config",
    [
        {"type": "atr_trailing", "period": 10, "atr_period": 10, "k": 3.0},
        {"type": "keltner", "ema_period": 10, "atr_period": 10, "mult": 2.0},
    ],
)
def test_ohlc_trend_types_produce_both_signals(config):
    df = _ohlc(_OHLC_TREND)
    entries, exits = generate_signals(df, config)
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    assert not (entries & exits).any()
    assert entries.dtype == bool and exits.dtype == bool
    assert entries.index.equals(df.index)


def test_volatility_breakout_signals():
    # spread 를 range 대비 작게 둬 하락 구간에서는 목표가 미달(진입 없음) →
    # 상승→하락 전환 봉에서 청산이 발생하도록 한다.
    df = _ohlc(_OHLC_TREND, spread=1.0)
    entries, exits = generate_signals(df, {"type": "volatility_breakout", "k": 0.5})
    assert entries.sum() >= 1
    # 당일 종가 청산: 진입 다음 봉에 청산 신호
    assert exits.sum() >= 1
    # 동일 봉 진입·청산 동시 금지
    assert not (entries & exits).any()
    # 첫 봉(전일 range NaN)에는 진입 신호 없음 — 미래참조 방지
    assert not bool(entries.iloc[0])


def test_volatility_breakout_exit_follows_entry():
    df = _ohlc(_OHLC_TREND, spread=1.0)
    entries, exits = generate_signals(df, {"type": "volatility_breakout", "k": 0.5})
    # 모든 청산은 직전 봉이 진입이었던 봉에서 발생(entries.shift(1)).
    assert (exits.values <= entries.shift(1).fillna(False).values).all()


def test_obv_trend_signals_on_trend_reversal():
    df = _ohlc(_OHLC_TREND)
    entries, exits = generate_signals(df, {"type": "obv_trend", "period": 10})
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    assert not (entries & exits).any()
    assert entries.dtype == bool and exits.dtype == bool
    assert entries.index.equals(df.index)


def test_obv_trend_period_must_be_at_least_two():
    df = _ohlc(_OHLC_TREND)
    with pytest.raises(ValueError):
        generate_signals(df, {"type": "obv_trend", "period": 1})


def test_obv_trend_rejects_series_input():
    s = _series(_OHLC_TREND)
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "obv_trend", "period": 10})


def test_stochastic_signals_on_oscillating_series():
    df = _ohlc(_OSC, spread=2.0)
    entries, exits = generate_signals(
        df, {"type": "stochastic", "k_period": 14, "d_period": 3, "lower": 20, "upper": 80}
    )
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    assert not (entries & exits).any()


def test_ohlc_strategy_rejects_series_input():
    # OHLC 전략에 종가 Series 만 넘기면 명확한 에러.
    s = _series(_OHLC_TREND)
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "stochastic", "k_period": 14, "d_period": 3})


@pytest.mark.parametrize(
    "config",
    [
        {"type": "atr_trailing", "k": -1},
        {"type": "atr_trailing", "k": 0},
        {"type": "volatility_breakout", "k": 0},
        {"type": "keltner", "mult": 0},
        {"type": "stochastic", "lower": 90, "upper": 10},
        {"type": "obv_trend", "period": 1},
    ],
)
def test_ohlc_param_validation_raises(config):
    df = _ohlc(_OHLC_TREND)
    with pytest.raises(ValueError):
        generate_signals(df, config)


@pytest.mark.parametrize(
    "config",
    [
        {"type": "atr_trailing", "period": 10, "atr_period": 10, "k": 3.0},
        {"type": "volatility_breakout", "k": 0.5},
        {"type": "keltner", "ema_period": 10, "atr_period": 10, "mult": 2.0},
        {"type": "stochastic", "k_period": 14, "d_period": 3, "lower": 20, "upper": 80},
        {"type": "obv_trend", "period": 10},
    ],
)
def test_latest_signal_ohlc(config):
    df = _ohlc(_OHLC_TREND)
    assert latest_signal(df, config) in ("buy", "sell", None)


def test_atr_trailing_no_lookahead():
    # 단조 상승: 진입 채널 상단은 shift(1) 로 당일 고가 제외 → 첫 진입이 채널 형성 후.
    df = _ohlc(list(range(1, 60)))
    entries, _ = generate_signals(
        df, {"type": "atr_trailing", "period": 10, "atr_period": 10, "k": 3.0}
    )
    first_entry = entries.idxmax() if entries.any() else None
    # period 봉 이전(채널 미형성)에는 진입 없음
    assert first_entry is None or entries.iloc[:10].sum() == 0


# ─────────────────────────── 사용자 정의(룰 빌더) ───────────────────────────


def test_custom_sma_cross_equivalent_to_builtin():
    # 진입 SMA(3) > SMA(10), 청산 SMA(3) < SMA(10) 는 SMA 크로스와 동일 신호여야 한다.
    s = _series(_TREND)
    cfg = {
        "type": "custom",
        "entry": [
            {
                "left": {"kind": "sma", "period": 3},
                "op": ">",
                "right": {"kind": "sma", "period": 10},
            }
        ],
        "exit": [
            {
                "left": {"kind": "sma", "period": 3},
                "op": "<",
                "right": {"kind": "sma", "period": 10},
            }
        ],
    }
    e_custom, x_custom = generate_signals(s, cfg)
    e_builtin, x_builtin = sma_crossover_signals(s, fast=3, slow=10)
    assert e_custom.equals(e_builtin)
    assert x_custom.equals(x_builtin)


def test_custom_and_combination():
    # AND 결합: 두 조건이 동시에 충족되는 순간만 진입.
    s = _series(_TREND)
    cfg = {
        "type": "custom",
        "entry": [
            {
                "left": {"kind": "sma", "period": 3},
                "op": ">",
                "right": {"kind": "sma", "period": 10},
            },
            {
                "left": {"kind": "price", "source": "close"},
                "op": ">",
                "right": {"kind": "const", "value": 30},
            },
        ],
        "exit": [
            {
                "left": {"kind": "sma", "period": 3},
                "op": "<",
                "right": {"kind": "sma", "period": 10},
            }
        ],
    }
    entries, exits = generate_signals(s, cfg)
    assert entries.sum() >= 1
    assert exits.sum() >= 1
    assert not (entries & exits).any()
    assert entries.dtype == bool and exits.dtype == bool
    assert entries.index.equals(s.index)


def test_custom_requires_ohlc_flag():
    close_only = {
        "type": "custom",
        "entry": [
            {
                "left": {"kind": "rsi", "period": 14},
                "op": "<",
                "right": {"kind": "const", "value": 30},
            }
        ],
        "exit": [
            {
                "left": {"kind": "rsi", "period": 14},
                "op": ">",
                "right": {"kind": "const", "value": 70},
            }
        ],
    }
    assert requires_ohlc(close_only) is False

    with_high = {
        "type": "custom",
        "entry": [
            {
                "left": {"kind": "price", "source": "high"},
                "op": ">",
                "right": {"kind": "const", "value": 100},
            }
        ],
        "exit": [
            {
                "left": {"kind": "price", "source": "close"},
                "op": "<",
                "right": {"kind": "const", "value": 50},
            }
        ],
    }
    assert requires_ohlc(with_high) is True


def test_custom_min_periods():
    cfg = {
        "type": "custom",
        "entry": [
            {
                "left": {"kind": "sma", "period": 5},
                "op": ">",
                "right": {"kind": "sma", "period": 20},
            }
        ],
        "exit": [
            {
                "left": {"kind": "macd_line", "fast": 12, "slow": 26, "signal": 9},
                "op": "<",
                "right": {"kind": "macd_signal", "fast": 12, "slow": 26, "signal": 9},
            }
        ],
    }
    # 가장 긴 기간 = macd slow(26)+signal(9)=35 → +1
    assert min_periods(cfg) == 36

    # 상수만 있는 경우 최소 2봉.
    const_only = {
        "type": "custom",
        "entry": [
            {
                "left": {"kind": "price", "source": "close"},
                "op": ">",
                "right": {"kind": "const", "value": 10},
            }
        ],
        "exit": [
            {
                "left": {"kind": "price", "source": "close"},
                "op": "<",
                "right": {"kind": "const", "value": 5},
            }
        ],
    }
    assert min_periods(const_only) == 2


def test_custom_high_operand_rejects_series_input():
    # high 를 참조하는 custom 전략에 종가 Series 만 넘기면 명확한 에러.
    s = _series(_TREND)
    cfg = {
        "type": "custom",
        "entry": [
            {
                "left": {"kind": "price", "source": "high"},
                "op": ">",
                "right": {"kind": "const", "value": 100},
            }
        ],
        "exit": [
            {
                "left": {"kind": "price", "source": "close"},
                "op": "<",
                "right": {"kind": "const", "value": 50},
            }
        ],
    }
    with pytest.raises(ValueError):
        generate_signals(s, cfg)


def test_custom_empty_conditions_raise():
    s = _series(_TREND)
    with pytest.raises(ValueError):
        generate_signals(s, {"type": "custom", "entry": [], "exit": []})
    # 빈 그룹(children 없음)도 동일하게 거부.
    with pytest.raises(ValueError):
        generate_signals(
            s,
            {
                "type": "custom",
                "entry": {"combinator": "and", "children": []},
                "exit": {"combinator": "and", "children": []},
            },
        )


def test_custom_legacy_list_equals_and_group():
    # 레거시 list 입력과 동일 내용의 AND 그룹은 동일 신호를 내야 한다(하위호환).
    s = _series(_TREND)
    conds = [
        {
            "left": {"kind": "sma", "period": 3},
            "op": ">",
            "right": {"kind": "sma", "period": 10},
        }
    ]
    exit_conds = [
        {
            "left": {"kind": "sma", "period": 3},
            "op": "<",
            "right": {"kind": "sma", "period": 10},
        }
    ]
    legacy = {"type": "custom", "entry": conds, "exit": exit_conds}
    grouped = {
        "type": "custom",
        "entry": {"combinator": "and", "children": conds},
        "exit": {"combinator": "and", "children": exit_conds},
    }
    e1, x1 = generate_signals(s, legacy)
    e2, x2 = generate_signals(s, grouped)
    assert e1.equals(e2)
    assert x1.equals(x2)


def test_custom_or_combination_is_union_of_conditions():
    # OR 진입: 두 조건 중 하나라도 충족되는 상태의 상승 에지. AND 보다 신호가 잦다.
    s = _series(_TREND)
    a = {"kind": "sma", "period": 3}
    b = {"kind": "sma", "period": 10}
    c1 = {"left": a, "op": ">", "right": b}
    c2 = {"left": {"kind": "price", "source": "close"}, "op": ">", "right": {"kind": "const", "value": 30}}
    exit_node = {"combinator": "and", "children": [{"left": a, "op": "<", "right": b}]}
    or_cfg = {
        "type": "custom",
        "entry": {"combinator": "or", "children": [c1, c2]},
        "exit": exit_node,
    }
    and_cfg = {
        "type": "custom",
        "entry": {"combinator": "and", "children": [c1, c2]},
        "exit": exit_node,
    }
    e_or, _ = generate_signals(s, or_cfg)
    e_and, _ = generate_signals(s, and_cfg)
    assert e_or.dtype == bool
    # OR 결합 진입 횟수는 AND 결합 이상.
    assert e_or.sum() >= e_and.sum()


def test_custom_nested_group_mixed_and_or():
    # (SMA(3)>SMA(10) OR RSI<40) AND close>20 형태의 중첩 논리식이 정상 평가된다.
    s = _series(_TREND)
    cfg = {
        "type": "custom",
        "entry": {
            "combinator": "and",
            "children": [
                {
                    "combinator": "or",
                    "children": [
                        {"left": {"kind": "sma", "period": 3}, "op": ">", "right": {"kind": "sma", "period": 10}},
                        {"left": {"kind": "rsi", "period": 14}, "op": "<", "right": {"kind": "const", "value": 40}},
                    ],
                },
                {"left": {"kind": "price", "source": "close"}, "op": ">", "right": {"kind": "const", "value": 20}},
            ],
        },
        "exit": {
            "combinator": "and",
            "children": [
                {"left": {"kind": "sma", "period": 3}, "op": "<", "right": {"kind": "sma", "period": 10}}
            ],
        },
    }
    entries, exits = generate_signals(s, cfg)
    assert entries.dtype == bool and exits.dtype == bool
    assert not (entries & exits).any()
    assert entries.index.equals(s.index)
    # 중첩 그룹 안의 지표 기간(rsi 14 가 최장)이 min_periods 추론에도 반영된다.
    assert min_periods(cfg) == 15  # max period 14 + 1
    assert requires_ohlc(cfg) is False
