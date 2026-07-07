"""리밸런싱(다종목 포트폴리오) 백테스트 엔진.

단일종목 vectorbt 엔진(engine.py)과 달리, universe 를 주기적으로 재선정·리밸런싱하는
전략(RebalanceConfig)의 성과를 일별 시뮬레이션으로 계산한다.

설계 원칙
=========
- **실거래와 동일 로직 재사용**: 종목 선정·목표비중은 실거래 러너가 쓰는
  engine.rebalance.compute_target_weights 를, 종합점수는 metrics._compute_stock_scores 를
  그대로 재사용해 백테스트/실거래 정합성을 보장한다.
- **미래참조(look-ahead) 방지**: 리밸런싱일 t 의 선정은 t 종가까지의 데이터만 사용하고
  체결도 t 종가로 한다(signals.py 전반의 '당일 종가 확정 후 산출·당일 종가 체결' 규약과 동일).
- **거래비용**: 매수 레그는 위탁수수료(fees), 매도 레그는 위탁수수료+증권거래세(fees+tax)를
  거래대금에 비례해 차감한다. 드리프트 밴드 내 종목은 매매하지 않아 회전율·비용을 줄인다.
- **현금화 오버레이(레짐 필터)**: regime_filter 가 켜져 있으면 기준지수가 이동평균 아래로
  내려간 국면(risk-off)에서는 즉시 보유를 청산해 현금화하고 신규 매수를 중단한다. 지수가
  이동평균 위로 회복(risk-off→risk-on 전환)하면 현금 상태에서 그날 즉시 신규 선정·매수로
  재진입한다(cadence 를 기다리지 않는다). V자 반등 초기 수익을 놓치지 않기 위함이다.
- **거래 로그**: 매수·매도 1건마다 체결가·거래대금과 '그 순간'의 포트폴리오 누적수익률,
  매도 시 해당 포지션의 보유수익률(원가 대비)을 trades 로 남겨 사후 전략 개선에 활용한다.

알려진 한계(실거래와의 차이)
--------------------------
- 비중은 분수(fractional)로 시뮬레이션한다(실거래 compute_rebalance_orders 는 정수 1주 단위).
  대형주·충분한 capital 에서는 근사오차가 작으나, 고가주/소액 capital 에서는 실거래와
  괴리가 있을 수 있다.
- 목표비중은 '현재 포트폴리오 평가액'(복리) 기준으로 산정한다(실거래는 배정 capital 고정).
  백테스트는 복리 성장을 반영하는 것이 성과추정에 더 적절하다.
- 슬리피지·부분체결·상하한가·호가단위는 반영하지 않는다(보수적으로 비용을 별도 가산해 해석할 것).
- 생존편향: 가격 패널이 '현재 상장 종목' 기준이면 과거 성과가 상방 편향될 수 있다.
- 레짐 필터는 일별로 판정한다. 청산은 risk-off 즉시, 재진입은 risk-on 회복 즉시 이뤄진다
  (실거래 러너와 동일 규약). 왕복 매매비용(fees+tax)은 _apply_rebalance 에서 그대로 차감된다.
"""
from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

from engine.rebalance import _period_key, compute_target_weights

# 주의: app.services.metrics 는 app.services.backtest.signals 를 임포트하므로,
# 이 모듈에서 metrics 를 최상위 임포트하면 순환 임포트가 된다
# (backtest/__init__ → portfolio → metrics → backtest.signals). 따라서
# metrics 의 헬퍼는 사용 시점에 함수 내부에서 지연 임포트한다.

logger = logging.getLogger(__name__)


def _normalize_index(panel: pd.DataFrame) -> pd.DataFrame:
    """패널 인덱스를 tz-naive 일자(자정)로 정규화하고 중복 일자를 제거한다."""
    idx = panel.index
    if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
        idx = idx.tz_localize(None)
    panel = panel.copy()
    panel.index = pd.DatetimeIndex(idx).normalize()
    panel = panel[~panel.index.duplicated(keep="last")]
    return panel.sort_index()


def _rebalance_dates(dates: pd.DatetimeIndex, cfg: dict) -> set[pd.Timestamp]:
    """cadence·rebalance_dom/weekday 규칙으로 각 주기의 실제 리밸런싱 거래일을 고른다.

    각 주기(일/주/월)에서 지정 임계(dom/weekday) 이상인 '첫 거래일'을 선택한다.
    지정일이 휴장이면 같은 주기 내 다음 거래일에 자연 발화한다(is_rebalance_due 와 동일 취지).
    """
    cadence = cfg.get("cadence", "monthly")
    weekday = int(cfg.get("rebalance_weekday") or 0)
    dom = int(cfg.get("rebalance_dom") or 1)

    picked: set[pd.Timestamp] = set()
    seen_periods: set = set()
    for d in dates:
        key = _period_key(d, cadence)
        if key in seen_periods:
            continue
        if cadence == "weekly" and d.weekday() < weekday:
            continue
        if cadence == "monthly" and d.day < dom:
            continue
        seen_periods.add(key)
        picked.add(d)
    return picked


def _regime_on_flags(
    panel_index: pd.DatetimeIndex,
    regime_series: pd.Series | None,
    ma_period: int,
    *,
    exit_buffer: float = 0.0,
    reentry_buffer: float = 0.0,
) -> pd.Series | None:
    """리밸런싱/청산 판정용 위험선호(risk-on) 불리언 시리즈를 만든다(stateful 히스테리시스).

    기준지수 종가(regime_series)를 패널 거래일에 정렬(ffill)한 뒤, ma_period 이동평균과
    비대칭 밴드로 경로 의존(stateful) 레짐을 판정한다. 밴드 사이(hold zone)에서는 직전
    상태를 유지해 박스권 휩쏘(bull-trap)로 인한 잦은 청산·재진입을 억제한다.

      - 청산(on→off): 지수 < MA×(1 − exit_buffer)
      - 재진입(off→on): 지수 ≥ MA×(1 + reentry_buffer)
      - 그 사이: 직전 상태 유지
      - 초기 상태: 첫 유효일(MA 확정)에 지수 ≥ MA 면 on, 아니면 off.
        MA 미확정(초기) 구간은 참여(True)로 둬 데이터 부족만으로 현금화되지 않게 한다.

    exit_buffer=reentry_buffer=0.0 이면 무상태 rs>=ma 판정과 완전히 동일하다(하위호환).
    미래참조 없음: 각 일자 판정은 그날까지의 지수 종가·MA 만 사용한다.

    :return: panel_index 로 정렬된 bool Series. regime_series 가 없으면 None.
    """
    if regime_series is None or regime_series.empty:
        return None
    rs = regime_series.copy()
    idx = rs.index
    if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
        idx = idx.tz_localize(None)
    rs.index = pd.DatetimeIndex(idx).normalize()
    rs = rs[~rs.index.duplicated(keep="last")].sort_index()
    rs = rs.reindex(panel_index).ffill()

    min_p = max(5, ma_period // 2)
    ma = rs.rolling(ma_period, min_periods=min_p).mean()

    exit_buffer = max(0.0, float(exit_buffer))
    reentry_buffer = max(0.0, float(reentry_buffer))

    rs_vals = rs.to_numpy(dtype=float)
    ma_vals = ma.to_numpy(dtype=float)
    flags = np.ones(len(panel_index), dtype=bool)  # 기본 참여(True)
    state = True
    initialized = False
    for i in range(len(panel_index)):
        v = rs_vals[i]
        m = ma_vals[i]
        if np.isnan(v) or np.isnan(m):
            # MA 미확정(초기) 구간 — 참여(True), 상태는 아직 확정하지 않는다.
            flags[i] = True
            continue
        if not initialized:
            state = bool(v >= m)          # 첫 유효일: 무상태 rs>=ma 로 초기화
            initialized = True
        elif state:                        # 직전 위험선호(on) → 밴드 하단 이탈 시만 청산
            if v < m * (1.0 - exit_buffer):
                state = False
        else:                              # 직전 위험회피(off) → 밴드 상단 회복 시만 재진입
            if v >= m * (1.0 + reentry_buffer):
                state = True
        flags[i] = state
    return pd.Series(flags, index=panel_index, dtype=bool)


def _score_factor_frame(hist: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    """리밸런싱일까지의 가격 패널로 종합점수 입력 팩터를 만든다(가격 기반 팩터만).

    밸류 팩터(PER/PBR/DIV)는 펀더멘털이 필요하므로 호출자가 주입한다(없으면 중립 처리).
    metrics 의 _compute_vol_ann/_compute_mdd 를 재사용해 실거래 점수 정의와 일치시킨다.
    """
    from app.services.metrics import _compute_mdd, _compute_vol_ann  # 지연(순환 회피)

    rows: dict[str, dict] = {}
    for code in codes:
        row: dict = {}
        if code in hist.columns:
            c = hist[code].dropna()
            n = len(c)
            last = float(c.iloc[-1]) if n else None
            if n >= 22 and c.iloc[-22] > 0:
                row["mom_1m"] = last / float(c.iloc[-22]) - 1.0
            if n >= 64 and c.iloc[-64] > 0:
                row["mom_3m"] = last / float(c.iloc[-64]) - 1.0
            if n >= 127 and c.iloc[-127] > 0:
                row["mom_6m"] = last / float(c.iloc[-127]) - 1.0
            if n >= 252:
                hi = float(c.iloc[-252:].max())
                if hi > 0:
                    row["high_52w_ratio"] = last / hi
            row["vol_ann"] = _compute_vol_ann(c.tail(253))
            row["mdd_252"] = _compute_mdd(c.tail(252))
        rows[code] = row
    return pd.DataFrame.from_dict(rows, orient="index")


def _targets_at(
    d: pd.Timestamp,
    panel: pd.DataFrame,
    config: dict,
    fundamentals_provider,
) -> dict[str, float]:
    """리밸런싱일 d 의 목표비중을 산정한다(d 종가까지의 데이터만 사용)."""
    from app.services.metrics import _compute_stock_scores  # 지연(순환 회피)

    hist = panel.loc[:d]
    universe = list(config.get("universe", []))
    method = config.get("selection", {}).get("method", "momentum")

    if method == "score":
        fac = _score_factor_frame(hist, universe)
        if fundamentals_provider is not None:
            try:
                fdf = fundamentals_provider(d.date(), universe)
            except Exception:  # noqa: BLE001
                fdf = None
            if fdf is not None and not fdf.empty:
                # 밸류(PER/PBR/DIV) + 퀄리티(roe/debt_ratio/fcf) 컬럼을 스코어링
                # 프레임에 합류한다. 존재하지 않는 컬럼은 건너뛰어 중립 처리되게 한다.
                for col in ("PER", "PBR", "DIV", "roe", "debt_ratio", "fcf", "f_score",
                            "op_growth", "net_growth", "turnaround"):
                    if col in fdf.columns:
                        fac[col] = fdf[col].reindex(fac.index)
        weights = config.get("selection", {}).get("factor_weights")
        scored = _compute_stock_scores(fac, weights=weights)
        scores = {
            code: float(v)
            for code, v in scored["score"].items()
            if v is not None and not (isinstance(v, float) and math.isnan(v))
        }
        return compute_target_weights({}, config, scores=scores)

    price_history = {
        sym: hist[sym].dropna() for sym in universe if sym in hist.columns
    }
    return compute_target_weights(price_history, config)


def _trade_rec(
    d: pd.Timestamp,
    sym: str,
    side: str,
    amt_norm: float,
    prices: pd.Series | None,
    capital: float,
    port_return: float,
    position_return: float | None,
    reason: str,
) -> dict:
    """거래 1건의 로그 레코드(JSON 안전)를 만든다.

    :param amt_norm: 정규화 거래대금(총자산=1.0 기준). capital 을 곱해 원화 환산한다.
    :param port_return: 체결 시점의 포트폴리오 누적수익률(시작=0).
    :param position_return: 매도 시 해당 포지션의 원가 대비 보유수익률(매수는 None).
    :param reason: 'rebalance'(정기 리밸런싱) 또는 'regime_exit'(레짐 청산).
    """
    price = prices.get(sym) if prices is not None else None
    return {
        "t": d.isoformat(),
        "symbol": sym,
        "side": side,
        "amount": round(float(amt_norm) * float(capital)),  # 원화 거래대금(근사)
        "price": _safe(price),
        "port_return": _safe(port_return),
        "position_return": _safe(position_return),
        "reason": reason,
    }


def _apply_rebalance(
    val: dict[str, float],
    cost: dict[str, float],
    targets: dict[str, float],
    equity: float,
    prices: pd.Series | None,
    d: pd.Timestamp,
    capital: float,
    drift_band: float,
    fees: float,
    tax: float,
    trades: list[dict],
    *,
    liquidate: bool = False,
    reason: str = "rebalance",
) -> tuple[float, float]:
    """목표비중으로 리밸런싱하며 비용을 차감하고 거래 로그를 남긴다(매도 먼저 → 매수).

    :param val: 종목→평가액(정규화 단위). 함수가 제자리 수정한다.
    :param cost: 종목→원가 기준 누적 투자액(정규화 단위). 포지션 수익률 산출용. 제자리 수정.
    :param equity: 리밸런싱 직전 총자산(cash+보유평가액).
    :param liquidate: True 면 드리프트 밴드를 무시하고 보유 전량을 청산한다(레짐 현금화).
    :param reason: 거래 로그의 사유 태그.
    :return: (리밸런싱 후 cash, 회전율 turnover=Σ|Δw|).
    """
    if equity <= 0:
        return 0.0, 0.0

    port_return = equity - 1.0  # 시작 자산 1.0 앵커 기준 누적수익률

    sells: list[tuple[str, float]] = []
    buys: list[tuple[str, float]] = []
    turnover = 0.0
    if liquidate:
        for sym, v in list(val.items()):
            if v > 1e-12:
                sells.append((sym, v))     # 전량 매도
                turnover += v / equity
    else:
        for sym in set(targets) | set(val):
            cur_w = val.get(sym, 0.0) / equity
            tgt_w = targets.get(sym, 0.0)
            dev = tgt_w - cur_w
            if abs(dev) <= drift_band:
                continue
            turnover += abs(dev)
            if dev < 0:
                sells.append((sym, -dev * equity))  # 매도 금액
            else:
                buys.append((sym, dev * equity))    # 매수 금액

    # 시작 현금 = equity - 보유평가액 합
    cash = equity - sum(val.values())

    # 매도 먼저 실행(현금 확보) — 포지션 원가 대비 수익률을 로그로 남긴다
    for sym, amt in sells:
        cur_val = val.get(sym, 0.0)
        amt = min(amt, cur_val)
        if amt <= 1e-12:
            continue
        proceeds = amt * (1.0 - fees - tax)
        cash += proceeds
        frac = amt / cur_val if cur_val > 0 else 0.0
        cost_portion = cost.get(sym, 0.0) * frac
        pos_ret = (amt / cost_portion - 1.0) if cost_portion > 1e-12 else None
        val[sym] = cur_val - amt
        cost[sym] = cost.get(sym, 0.0) - cost_portion
        if val[sym] <= 1e-12:
            val.pop(sym, None)
            cost.pop(sym, None)
        trades.append(
            _trade_rec(d, sym, "sell", amt, prices, capital, port_return, pos_ret, reason)
        )

    # 매수: 현금 부족 시 비례 축소(음수 현금 방지)
    total_buy_cost = sum(amt * (1.0 + fees) for _, amt in buys)
    scale = 1.0
    if total_buy_cost > cash and total_buy_cost > 0:
        scale = cash / total_buy_cost
    for sym, amt in buys:
        amt *= scale
        if amt <= 1e-12:
            continue
        buy_cost = amt * (1.0 + fees)
        cash -= buy_cost
        val[sym] = val.get(sym, 0.0) + amt
        cost[sym] = cost.get(sym, 0.0) + amt  # 원가는 수수료 제외 매수액으로 적립
        trades.append(
            _trade_rec(d, sym, "buy", amt, prices, capital, port_return, None, reason)
        )

    return cash, turnover


def _safe(x) -> float | None:
    """NaN/inf → None(JSON 안전)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def run_rebalance_backtest(
    close_panel: pd.DataFrame,
    config: dict,
    sim_start,
    sim_end,
    fundamentals_provider=None,
    regime_series: pd.Series | None = None,
) -> dict:
    """리밸런싱 전략을 일별 시뮬레이션한다.

    :param close_panel: index=일자, columns=종목코드, 값=종가. sim_start 이전 워밍업 구간을
        포함해야 한다(모멘텀·52주고가·변동성 팩터 계산용, 권장 ~300 거래일).
    :param config: RebalanceConfig(dict).
    :param sim_start, sim_end: 시뮬레이션(성과측정) 구간 경계(포함).
    :param fundamentals_provider: method="score" 전용. (as_of_date, codes)->DataFrame(index=code,
        columns 포함 PER/PBR/DIV). None 이면 밸류 팩터는 중립(0) 처리된다.
    :param regime_series: 현금화 오버레이용 기준지수 종가 Series(워밍업 포함). config.regime_filter
        가 켜져 있을 때만 사용된다. None 이면 오버레이 미적용.
    :return: {total_return, mdd, sharpe, cagr, win_rate, num_trades, num_rebalances,
        avg_turnover, equity_curve, markers, holdings, trades} — Backtest.result 로 저장 가능한 JSON.
    """
    panel = _normalize_index(close_panel).ffill()
    if panel.empty:
        raise ValueError("가격 데이터가 비어 있습니다.")

    start = pd.Timestamp(pd.Timestamp(sim_start).date())
    end = pd.Timestamp(pd.Timestamp(sim_end).date())
    sim_dates = panel.index[(panel.index >= start) & (panel.index <= end)]
    if len(sim_dates) < 2:
        raise ValueError("시뮬레이션 구간의 거래일이 부족합니다(최소 2일).")

    capital = float(config.get("capital", 10_000_000))
    fees = float(config.get("fees", 0.00015))
    tax = float(config.get("tax", 0.0020))
    drift_band = float(config.get("drift_band_pct", 0.05))
    rebal_dates = _rebalance_dates(sim_dates, config)

    # 현금화 오버레이(레짐 필터) 준비
    rf = config.get("regime_filter") or {}
    regime_on: pd.Series | None = None
    if rf.get("enabled"):
        regime_on = _regime_on_flags(
            panel.index, regime_series, int(rf.get("ma_period", 200)),
            exit_buffer=float(rf.get("exit_buffer_pct", 0.0) or 0.0),
            reentry_buffer=float(rf.get("reentry_buffer_pct", 0.0) or 0.0),
        )
        if regime_on is None:
            logger.warning(
                "레짐 필터가 켜져 있으나 기준지수 시세를 확보하지 못해 오버레이를 적용하지 않는다."
            )

    cash = 1.0            # 정규화 총자산(=capital 배율)
    val: dict[str, float] = {}    # 종목→평가액
    cost: dict[str, float] = {}   # 종목→원가 누적 투자액(포지션 수익률용)
    prev_prices: pd.Series | None = None
    prev_risk_off = False  # 직전일 레짐 상태(risk-off→risk-on 전환 즉시 재진입 판정용)

    equities_norm: list[float] = []   # 일별 종가 시점(리밸런싱 반영 후) 총자산
    equity_curve: list[dict] = []
    markers: list[dict] = []
    turnovers: list[float] = []
    trades: list[dict] = []

    for d in sim_dates:
        prices = panel.loc[d]
        # 1) 당일 수익률 반영(보유 종목 평가액 갱신)
        if prev_prices is not None and val:
            for sym in list(val):
                p0 = prev_prices.get(sym)
                p1 = prices.get(sym)
                if p0 and p1 and p0 > 0 and pd.notna(p0) and pd.notna(p1):
                    val[sym] *= float(p1) / float(p0)
        prev_prices = prices

        # 2) 레짐 판정(당일). risk-off 이면 즉시 청산·현금화하고 매수 중단.
        risk_off = bool(regime_on is not None and not bool(regime_on.get(d, True)))
        if risk_off and val:
            equity = cash + sum(val.values())
            cash, turnover = _apply_rebalance(
                val, cost, {}, equity, prices, d, capital,
                drift_band, fees, tax, trades,
                liquidate=True, reason="regime_exit",
            )
            if turnover > 0:
                turnovers.append(turnover)
                markers.append({"t": d.isoformat(), "type": "regime_exit"})

        # 3) 리밸런싱: 정기 리밸런싱일이거나, 레짐이 risk-off→risk-on 으로 회복돼
        #    현금 상태에서 재진입해야 하는 날(cadence 를 기다리지 않고 회복 즉시 신규
        #    선정·매수). 재진입 게이팅 = (d in rebal_dates) 또는 (risk-on 전환 & 현금).
        #    미래참조 방지: 재진입일 d 의 선정·목표비중도 기존과 동일하게 d 까지의
        #    데이터(_targets_at 의 panel.loc[:d])만 사용한다.
        regime_reentry = bool(
            regime_on is not None and not risk_off and prev_risk_off and not val
        )
        if not risk_off and (d in rebal_dates or regime_reentry):
            equity = cash + sum(val.values())
            targets = _targets_at(d, panel, config, fundamentals_provider)
            # 목표 종목 중 당일 가격이 없는 종목은 제외(매매 불가)
            targets = {
                s: w for s, w in targets.items()
                if pd.notna(prices.get(s)) and float(prices.get(s) or 0) > 0
            }
            if targets:
                cash, turnover = _apply_rebalance(
                    val, cost, targets, equity, prices, d, capital,
                    drift_band, fees, tax, trades,
                    reason="rebalance",
                )
                if turnover > 0:
                    turnovers.append(turnover)
                    markers.append({
                        "t": d.isoformat(),
                        "type": "rebalance",
                        "holdings": len(val),
                    })

        equity_now = cash + sum(val.values())
        equities_norm.append(equity_now)
        equity_curve.append({"t": d.isoformat(), "v": _safe(equity_now * capital)})
        prev_risk_off = risk_off

    # ── 성과지표 산출 ──
    series = np.array([1.0] + equities_norm, dtype=float)  # 시작 자산 1.0 앵커
    rets = series[1:] / series[:-1] - 1.0
    total_return = _safe(series[-1] - 1.0)

    # MDD
    running_max = np.maximum.accumulate(series)
    drawdown = series / running_max - 1.0
    mdd = _safe(drawdown.min())

    # Sharpe(무위험 0, 일간→연율 √252)
    sharpe = None
    if len(rets) > 1 and rets.std(ddof=1) > 0:
        sharpe = _safe(rets.mean() / rets.std(ddof=1) * math.sqrt(252))

    # CAGR
    n_days = len(rets)
    cagr = None
    if n_days > 0 and series[-1] > 0:
        cagr = _safe(series[-1] ** (252.0 / n_days) - 1.0)

    holdings = {
        sym: round(float(v) / float(max(series[-1], 1e-12)), 4)
        for sym, v in sorted(val.items(), key=lambda kv: kv[1], reverse=True)
    }

    logger.info(
        "리밸런싱 백테스트 완료 — 리밸런싱 %d회, 거래 %d건, 총수익률 %.4f, MDD %.4f%s",
        len(turnovers), len(trades), total_return or 0.0, mdd or 0.0,
        " (레짐 오버레이 적용)" if regime_on is not None else "",
    )

    return {
        "total_return": total_return,
        "mdd": mdd,
        "sharpe": sharpe,
        "cagr": cagr,
        "win_rate": None,  # 포트폴리오 전략에는 종목단위 승률 개념이 맞지 않음
        "num_trades": len(trades),
        "num_rebalances": len(turnovers),
        "avg_turnover": _safe(np.mean(turnovers)) if turnovers else 0.0,
        "equity_curve": equity_curve,
        "markers": markers,
        "holdings": holdings,
        "trades": trades,
    }
