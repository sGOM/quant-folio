"""vectorbt 기반 백테스트 엔진.

단일 종목 다중 전략 유형의 성과(수익률·MDD·샤프·승률)와 equity curve(자산곡선),
매매 시점 마커를 계산한다. 손절·익절·트레일링 스탑은 vectorbt 의 sl_stop/tp_stop 으로
반영한다.

close-only 전략(SMA/EMA/RSI/MACD/볼린저/돌파/모멘텀/z-score/이격도/donchian_squeeze/trix)은
종가 Series 를, OHLC 전략(atr_trailing/volatility_breakout/keltner/stochastic/obv_trend)은
OHLCV DataFrame 을 입력으로 받는다. 어느 경우든 **체결 가격은 종가(close)** 를 사용한다.
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
    반환: {total_return, mdd, sharpe, win_rate, num_trades,
           equity_curve:[{t,v}], markers:[{t,type,price}]}
    """
    import vectorbt as vbt

    cash = float(config.get("cash", 10_000_000))
    fees = float(config.get("fees", 0.00015))   # 위탁수수료(매수·매도 양방향)
    tax = float(config.get("tax", 0.0020))       # 증권거래세(매도 시에만, 2026 KRX 0.20%)

    # vectorbt 의 fees 는 매수·매도에 '대칭'으로 적용된다(매도 전용 세율을 따로 받지 못함).
    # 매도세를 양방향에 절반씩 나눠 실효 수수료로 환산하면, 1회 왕복(매수+매도) 총비용이
    # 2*fees + tax 로 정확히 일치한다(매수쪽 tax/2 는 근사이나 왕복 총액은 정확).
    effective_fees = fees + tax / 2.0

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

    # 리스크 청산: 트레일링이 설정되면 추적 손절(sl_trail)로, 아니면 고정 손절로 반영.
    sl_stop = config.get("trailing_stop_pct") or config.get("stop_loss_pct")
    sl_trail = config.get("trailing_stop_pct") is not None
    tp_stop = config.get("take_profit_pct")

    pf = vbt.Portfolio.from_signals(
        close, entries, exits, init_cash=cash, fees=effective_fees, freq="1D",
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

    equity_curve = [
        {"t": ts.isoformat(), "v": _safe(v)}
        for ts, v in equity.items()
        if _safe(v) is not None
    ]

    markers = []
    for ts in close.index[entries.values]:
        markers.append({"t": ts.isoformat(), "type": "buy", "price": _safe(close.loc[ts])})
    for ts in close.index[exits.values]:
        markers.append({"t": ts.isoformat(), "type": "sell", "price": _safe(close.loc[ts])})
    markers.sort(key=lambda m: m["t"])

    return {
        "total_return": _safe(pf.total_return()),
        "mdd": _safe(pf.max_drawdown()),
        "sharpe": _safe(pf.sharpe_ratio()),
        "win_rate": win_rate,
        "num_trades": int(trades.count()),
        "equity_curve": equity_curve,
        "markers": markers,
    }
