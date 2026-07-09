"""vectorbt 기반 백테스트 엔진.

단일 종목 다중 전략 유형의 성과(수익률·MDD·샤프·승률)와 equity curve(자산곡선),
매매 시점 마커를 계산한다. 손절·익절·트레일링 스탑은 vectorbt 의 sl_stop/tp_stop 으로
반영한다.

close-only 전략(SMA/EMA/RSI/MACD/볼린저/돌파/모멘텀/z-score/이격도/donchian_squeeze/trix)은
종가 Series 를, OHLC 전략(atr_trailing/volatility_breakout/keltner/stochastic/obv_trend)은
OHLCV DataFrame 을 입력으로 받는다. 어느 경우든 체결 기준가는 종가(close)를 사용한다.

체결 현실성(P0-1)
=================
- fill_mode="next_close"(기본): 신호는 종가(t) 확정 후 산출하되 체결은 익일 종가(t+1).
  신호와 동일 봉 종가로 체결하던 당일 미래참조(look-ahead)를 제거한다.
- fill_mode="same_close": 당일 종가 즉시 체결(구 동작, 민감도 분석용 opt-in).
- slippage_bps: 편도 슬리피지. 매수는 체결가 ×(1+slip), 매도는 ×(1−slip). vectorbt 의
  slippage 인자로 대칭 반영한다. slippage_vol_scale>0 이면 최근 20봉 수익률 표준편차/중앙값
  비로 봉별 슬리피지를 조정한다.
"""
from __future__ import annotations

import math

import pandas as pd

from app.services.backtest.signals import generate_signals, min_periods


def _safe(x) -> float | None:
    """NaN/inf 를 JSON 안전 값(None)으로 변환."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def run_backtest(data, config: dict) -> dict:
    """백테스트 실행. 동기(CPU) 함수이므로 스레드풀에서 호출할 것.

    :param data: 종가 Series(close-only 전략) 또는 OHLCV DataFrame(OHLC 전략).
        체결 가격은 항상 종가(close)를 사용한다(체결가는 종가 기준 유지).
    config: {type, ...유형별 파라미터, cash, fees,
             stop_loss_pct?, take_profit_pct?, trailing_stop_pct?}
    반환: {total_return, mdd, sharpe, sortino, win_rate, num_trades,
           equity_curve:[{t,v}], markers:[{t,type,price}]}
        sharpe·sortino 는 config.risk_free_rate(연) 초과 기준(기본 rf=0).
    """
    import vectorbt as vbt

    cash = float(config.get("cash", 10_000_000))
    fees = float(config.get("fees", 0.00015))   # 위탁수수료(매수·매도 양방향)
    tax = float(config.get("tax", 0.0020))       # 증권거래세(매도 시에만, 2026 KRX 0.20%)

    # vectorbt 의 fees 는 매수·매도에 '대칭'으로 적용된다(매도 전용 세율을 따로 받지 못함).
    # 매도세를 양방향에 절반씩 나눠 실효 수수료로 환산하면, 1회 왕복(매수+매도) 총비용이
    # 2*fees + tax 로 정확히 일치한다(매수쪽 tax/2 는 근사이나 왕복 총액은 정확).
    effective_fees = fees + tax / 2.0

    # 체결 현실성(P0-1): 체결 시점(fill_mode)·슬리피지.
    fill_mode = str(config.get("fill_mode", "next_close"))
    base_slip = max(0.0, float(config.get("slippage_bps", 5.0) or 0.0) / 10_000.0)
    slip_vol_scale = max(0.0, float(config.get("slippage_vol_scale", 0.0) or 0.0))
    rf_annual = float(config.get("risk_free_rate", 0.0) or 0.0)
    rf_daily = (1.0 + rf_annual) ** (1.0 / 252.0) - 1.0  # 위험조정지표용 일간 무위험수익률

    # OHLC DataFrame 이면 결측 봉(어느 컬럼이든 NaN)을 제거하고 close 를 별도 추출.
    if isinstance(data, pd.DataFrame):
        frame = data.astype(float).dropna()
        close = frame["close"]
        signal_input = frame
    else:
        close = data.astype(float).dropna()
        signal_input = close

    need = min_periods(config) + 1
    if len(close) < need:
        raise ValueError(f"데이터가 부족합니다 (필요 {need}봉 이상, 보유 {len(close)}봉).")

    entries, exits = generate_signals(signal_input, config)

    # 체결 시점: next_close 면 신호를 1봉 지연시켜 익일 종가에 체결한다(당일 미래참조 제거).
    if fill_mode == "next_close":
        exec_entries = entries.shift(1, fill_value=False)
        exec_exits = exits.shift(1, fill_value=False)
    else:
        exec_entries, exec_exits = entries, exits

    # 슬리피지: 고정(스칼라) 또는 변동성 비례(봉별 Series). vectorbt 는 매수 +slip·매도 −slip.
    slippage = base_slip
    if base_slip > 0 and slip_vol_scale > 0:
        vol = close.pct_change().rolling(20, min_periods=10).std()
        med = float(vol.median())
        if med == med and med > 0:  # med 가 NaN 이 아니고 양수
            factor = 1.0 + slip_vol_scale * (vol / med - 1.0)
            slippage = (base_slip * factor).clip(lower=0.0).fillna(base_slip)

    # 리스크 청산: 트레일링이 설정되면 추적 손절(sl_trail)로, 아니면 고정 손절로 반영.
    sl_stop = config.get("trailing_stop_pct") or config.get("stop_loss_pct")
    sl_trail = config.get("trailing_stop_pct") is not None
    tp_stop = config.get("take_profit_pct")

    pf = vbt.Portfolio.from_signals(
        close, exec_entries, exec_exits, init_cash=cash, fees=effective_fees,
        slippage=slippage, freq="1D",
        sl_stop=sl_stop, sl_trail=sl_trail, tp_stop=tp_stop,
    )

    equity = pf.value()
    trades = pf.trades

    win_rate = None
    try:
        if trades.count() > 0:
            win_rate = _safe(trades.win_rate())
    except Exception:  # noqa: BLE001
        win_rate = None

    # 위험조정지표: 무위험수익률(rf_daily) 초과 기준. Sortino 는 하방편차만 반영.
    try:
        sharpe = _safe(pf.sharpe_ratio(risk_free=rf_daily))
    except Exception:  # noqa: BLE001
        sharpe = _safe(pf.sharpe_ratio())
    try:
        # vectorbt sortino 는 목표수익(required_return, MAR)으로 하방편차를 잰다 → rf_daily 사용.
        sortino = _safe(pf.sortino_ratio(required_return=rf_daily))
    except Exception:  # noqa: BLE001
        sortino = None

    equity_curve = [
        {"t": ts.isoformat(), "v": _safe(v)}
        for ts, v in equity.items()
        if _safe(v) is not None
    ]

    markers = []
    for ts in close.index[exec_entries.values]:
        markers.append({"t": ts.isoformat(), "type": "buy", "price": _safe(close.loc[ts])})
    for ts in close.index[exec_exits.values]:
        markers.append({"t": ts.isoformat(), "type": "sell", "price": _safe(close.loc[ts])})
    markers.sort(key=lambda m: m["t"])

    return {
        "total_return": _safe(pf.total_return()),
        "mdd": _safe(pf.max_drawdown()),
        "sharpe": sharpe,
        "sortino": sortino,
        "win_rate": win_rate,
        "num_trades": int(trades.count()),
        "equity_curve": equity_curve,
        "markers": markers,
    }
