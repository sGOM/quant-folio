"""모의투자 실측 성과 ↔ 백테스트 기대곡선 괴리 추적 (improvements.md §6).

전략의 라이브/모의투자 체결(executions)로 일별 실측 자산가치(NAV) 곡선을 재구성하고,
동일 전략의 백테스트 기대 곡선(equity_curve)과 나란히 비교해 트래킹 에러·누적 괴리를
계량한다. 백테스트 코어(engine.py 등)는 건드리지 않는 별도 분석 파이프라인이며, 슬리피지
캘리브레이션(fill_quality)이 "체결 품질"만 다루는 것과 달리 여기서는 전략 단위 "수익률
궤적"을 비교한다.

────────────────────────────────────────────────────────────────────────
실측 NAV 재구성 방법(코드 사실)
────────────────────────────────────────────────────────────────────────
- 계좌는 **첫 체결 거래일**에 initial_capital 원의 현금만 보유(사전 포지션 0)한다고
  가정한다. positions 테이블은 히스토리가 없어 '지금' 스냅샷만 있으므로, 대신 executions
  를 시간순으로 재생(replay)해 각 거래일 종가(price_ticks)로 시가평가한 NAV 를 만든다.
- 매수 체결: cash -= filled_qty·filled_price + fee, 보유수량 += filled_qty.
  매도 체결: cash += filled_qty·filled_price − fee, 보유수량 −= filled_qty.
  fee 는 Execution.fee(수수료+세금)를 그대로 반영한다(있을 때만).
- 거래일 d 의 NAV = cash(d) + Σ 보유수량_i(d)·종가_i(d). 종가는 그날 값이 없으면 직전
  거래일 종가로 이월(ffill/bfill)한다.
- **창(window)과 재생 구간은 다르다(§5)**: 호출자(app.api.routes.tracking)가 date_from 으로
  조회 범위를 좁혀도, 이 함수는 항상 '진짜 첫 체결'부터 재생해 현금·보유수량을 정확히
  누적한다. display_from 을 주면 재생은 그대로 처음부터 하되 **반환 곡선만** display_from
  이후로 잘라낸다 — date_from 이전부터 보유하던 종목이 있어도 NAV 레벨이 어긋나지 않는다
  (긴 운용 기간의 재기동·기간 필터 조회에서도 괴리 지표가 오염되지 않는다).

────────────────────────────────────────────────────────────────────────
비교 지표(부호·정규화 규약)
────────────────────────────────────────────────────────────────────────
- 정규화 인덱스: 각 곡선을 자기 첫 값 기준 100 으로 재기준(overlay 비교용).
- tracking_error: 공통 거래일에서 (실측 일수익률 − 백테스트 일수익률)의 표본표준편차를
  √252 로 연율화한 값(소수, 0.08 = 8%/yr).
- cumulative_divergence_pct: 공통구간 첫날 대비 마지막날의 실측/백테스트 상대 성과 격차
  (%). (1+r_real)/(1+r_bt) − 1. 양수면 실측이 백테스트를 초과, 음수면 미달.

────────────────────────────────────────────────────────────────────────
한계(가정)
────────────────────────────────────────────────────────────────────────
- 사전 포지션 0·전액 현금 가정은 '진짜 첫 체결일' 시점에만 적용된다(§5로 해소). 다만 그
  전략의 **가장 이른 체결 이전부터** 보유하던 포지션(예: 수동으로 미리 사둔 종목, 다른
  전략에서 이관된 보유)은 여전히 잡히지 않는다 — executions 로그 자체가 유일한 재생 소스라,
  로그 밖의 보유는 원천적으로 알 수 없다.
- 유휴 현금은 NAV 레벨에 그대로 반영된다(initial_capital 이 실제 투입자본보다 크면 곡선이
  둔감해짐). 궤적 비교는 정규화 인덱스로 보정되나 절대 레벨은 initial_capital 에 의존한다.
- 백테스트 곡선은 익일종가 체결·슬리피지 가정 위에서 산출된 것이라, 체결가·타이밍·유니버스
  차이가 괴리의 원인이 될 수 있다(괴리 자체를 노출하는 것이 이 기능의 목적).
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

# 연율화 상수(거래일 기준). 백테스트 지표(engine.py)와 동일 관례.
TRADING_DAYS_PER_YEAR = 252
# 보유수량을 0 으로 간주하는 임계치(부동소수 잔량 정리).
_QTY_EPS = 1e-9


def _curve_to_series(curve: list[dict[str, Any]]) -> pd.Series:
    """[{t, v}] 곡선을 날짜 인덱스 Series 로 변환한다(중복일은 마지막 값 유지).

    t 는 date/datetime ISO 문자열 모두 허용하며 날짜로 정규화한다. v 가 없거나 유한하지
    않은 점은 제외한다.
    """
    idx: list[pd.Timestamp] = []
    vals: list[float] = []
    for pt in curve:
        t = pt.get("t")
        v = pt.get("v")
        if t is None or v is None:
            continue
        fv = float(v)
        if not math.isfinite(fv):
            continue
        idx.append(pd.Timestamp(t).normalize())
        vals.append(fv)
    if not idx:
        return pd.Series(dtype="float64")
    s = pd.Series(vals, index=pd.DatetimeIndex(idx)).sort_index()
    return s[~s.index.duplicated(keep="last")]


def _to_index100(curve: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """곡선을 자기 첫 값 기준 100 으로 재기준한 [{t, v}] 를 만든다(첫 값>0 일 때만)."""
    if not curve:
        return []
    base = None
    for pt in curve:
        v = pt.get("v")
        if v is not None and float(v) > 0:
            base = float(v)
            break
    if base is None:
        return []
    return [
        {"t": pt["t"], "v": float(pt["v"]) / base * 100.0}
        for pt in curve
        if pt.get("v") is not None
    ]


def reconstruct_realized_curve(
    executions: list[dict[str, Any]],
    close_panel: pd.DataFrame,
    *,
    initial_capital: float,
    display_from: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """체결 이력으로 일별 실측 자산가치(NAV) 곡선을 재구성한다(순수 함수).

    :param executions: 정규화된 체결 리스트(진짜 첫 체결부터 빠짐없이 — display_from 으로
        걸러내지 말 것, §5). 각 dict 키:
        symbol(str), side('buy'|'sell'), qty(float>0), price(float>0),
        fee(float>=0), date(pd.Timestamp|date, 체결일 KST).
    :param close_panel: 일봉 종가 패널(columns=종목, index=DatetimeIndex). price_ticks 종가.
        진짜 첫 체결일부터의 시세를 포함해야 재생이 정확하다.
    :param initial_capital: 진짜 첫 체결일 시점 현금(원). NAV 절대 레벨의 기준.
    :param display_from: 지정 시 이 날짜(포함) 이후만 반환 곡선에 담는다(조회 창 필터, §5).
        재생 자체(현금·보유수량 누적)는 언제나 executions 전체로 하므로, display_from 이전
        보유가 있어도 NAV 레벨이 어긋나지 않는다. None(기본)이면 첫 체결일부터 전부 반환.
    :return: (realized_curve=[{t,v}], notes) — t 는 날짜 ISO, v 는 원화 NAV.
        notes 는 데이터 한계 경고(예: 종가 누락 종목) 리스트.
    """
    notes: list[str] = []
    if not executions:
        return [], notes

    # 체결을 날짜순 정렬(안정 정렬로 같은 날 원 순서 보존).
    execs = sorted(executions, key=lambda e: pd.Timestamp(e["date"]).normalize())
    exec_dates = [pd.Timestamp(e["date"]).normalize() for e in execs]
    first_day = exec_dates[0]
    last_day = exec_dates[-1]
    display_from_ts = pd.Timestamp(display_from).normalize() if display_from is not None else None

    # 종가 패널 준비: 날짜 정규화·정렬. 종목별 종가가 아예 없으면 경고.
    panel = close_panel.copy() if not close_panel.empty else pd.DataFrame()
    if not panel.empty:
        panel.index = pd.DatetimeIndex(panel.index).normalize()
        panel = panel.sort_index()
        panel = panel[~panel.index.duplicated(keep="last")]

    symbols = sorted({e["symbol"] for e in execs})
    missing = [s for s in symbols if panel.empty or s not in panel.columns]
    if missing:
        notes.append(
            f"종가 데이터 없음(0 으로 평가): {', '.join(missing)} — NAV 가 과소평가될 수 있습니다."
        )

    # 거래일 인덱스: 종가 패널의 날짜 ∪ 체결일. 첫 체결일 이후 ~ 마지막 종가일/마지막 체결일.
    price_dates = list(panel.index) if not panel.empty else []
    all_dates = sorted(set(price_dates) | set(exec_dates))
    end_day = max([last_day] + price_dates) if price_dates else last_day
    days = [d for d in all_dates if first_day <= d <= end_day]
    if not days:
        days = [first_day]

    # 종가 조회용 패널을 거래일 인덱스에 맞춰 재색인·이월(ffill 후 bfill 로 선두 결측 보정).
    if not panel.empty:
        panel = panel.reindex(pd.DatetimeIndex(days)).ffill().bfill()

    qty: dict[str, float] = defaultdict(float)
    cash = float(initial_capital)
    curve: list[dict[str, Any]] = []
    i = 0
    n = len(execs)

    for d in days:
        # 그날(포함) 이전까지의 체결을 모두 반영(장중 체결 → 종가 평가는 반영 후).
        while i < n and exec_dates[i] <= d:
            e = execs[i]
            i += 1
            q = float(e["qty"])
            px = float(e["price"])
            fee = float(e.get("fee", 0.0) or 0.0)
            notional = q * px
            if str(e["side"]) == "buy":
                cash -= notional + fee
                qty[e["symbol"]] += q
            else:
                cash += notional - fee
                qty[e["symbol"]] -= q

        holdings = 0.0
        for sym, q in qty.items():
            if abs(q) < _QTY_EPS:
                continue
            if panel.empty or sym not in panel.columns:
                continue
            px = panel.at[d, sym]
            if pd.notna(px):
                holdings += q * float(px)

        if display_from_ts is None or d >= display_from_ts:
            curve.append({"t": d.date().isoformat(), "v": cash + holdings})

    return curve, notes


def replay_cash_balance(executions: list[dict[str, Any]], initial_capital: float) -> float:
    """체결 전체를 재생해 '지금' 시점의 현금 잔고를 계산한다(§11).

    reconstruct_realized_curve 와 동일한 현금 갱신 규칙(매수: -notional-fee, 매도:
    +notional-fee)을 쓰되, 일별 NAV 곡선 없이 최종 현금만 필요할 때 쓰는 경량 버전이다
    (engine.rebalance_runner._live_equity 의 라이브 자산가치 근사). 현금은 각 체결의
    현금흐름 델타를 그대로 누적한 합이므로 재생 순서(날짜 정렬)는 결과에 영향이 없다.

    과거 라운드트립의 확정 실현손익이 이 현금 잔고에 자연히 반영된다는 점이 핵심이다
    (매도로 원가보다 비싸게/싸게 팔았으면 그만큼 현금이 더/덜 들어온다) — 시가평가만
    추적하던 기존 근사(capital + 미실현손익)의 한계를 해소한다.
    """
    cash = float(initial_capital)
    for e in executions:
        q = float(e["qty"])
        px = float(e["price"])
        fee = float(e.get("fee", 0.0) or 0.0)
        notional = q * px
        if str(e["side"]) == "buy":
            cash -= notional + fee
        else:
            cash += notional - fee
    return cash


def _daily_returns(s: pd.Series) -> pd.Series:
    """양(>0) 값 구간의 일수익률(pct_change). 첫 NaN 은 제거한다."""
    r = s.pct_change()
    return r.iloc[1:]


def compute_tracking(
    realized_curve: list[dict[str, Any]],
    backtest_curve: list[dict[str, Any]],
) -> dict[str, Any]:
    """실측·백테스트 곡선을 비교해 정규화 인덱스·요약 지표를 산출한다(순수 함수).

    :param realized_curve: 실측 NAV 곡선 [{t, v}](reconstruct_realized_curve 결과).
    :param backtest_curve: 백테스트 equity_curve [{t, v}].
    :return: realized_index/backtest_index(100 기준) + metrics dict.
    """
    realized_index = _to_index100(realized_curve)
    backtest_index = _to_index100(backtest_curve)

    r_s = _curve_to_series(realized_curve)
    b_s = _curve_to_series(backtest_curve)

    metrics: dict[str, Any] = {
        "tracking_error": None,
        "cumulative_divergence_pct": None,
        "realized_return_pct": None,
        "backtest_return_pct": None,
        "correlation": None,
        "n_common_days": 0,
    }

    if r_s.empty or b_s.empty:
        return {
            "realized_index": realized_index,
            "backtest_index": backtest_index,
            "metrics": metrics,
        }

    # 공통 거래일로 정렬해 일수익률을 비교한다.
    common = r_s.index.intersection(b_s.index)
    metrics["n_common_days"] = int(len(common))
    if len(common) >= 2:
        rc = r_s.loc[common].sort_index()
        bc = b_s.loc[common].sort_index()
        r0, r1 = float(rc.iloc[0]), float(rc.iloc[-1])
        b0, b1 = float(bc.iloc[0]), float(bc.iloc[-1])
        if r0 > 0 and b0 > 0:
            r_ret = r1 / r0 - 1.0
            b_ret = b1 / b0 - 1.0
            metrics["realized_return_pct"] = r_ret * 100.0
            metrics["backtest_return_pct"] = b_ret * 100.0
            metrics["cumulative_divergence_pct"] = (
                (1.0 + r_ret) / (1.0 + b_ret) - 1.0
            ) * 100.0

        # 일수익률 차이의 연율화 표준편차(트래킹 에러) + 상관.
        r_ret_s = _daily_returns(rc)
        b_ret_s = _daily_returns(bc)
        diff = (r_ret_s - b_ret_s).dropna()
        if len(diff) >= 2:
            te = float(np.std(diff.to_numpy(), ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
            metrics["tracking_error"] = te
        aligned = pd.concat([r_ret_s, b_ret_s], axis=1).dropna()
        if len(aligned) >= 2:
            col_r = aligned.iloc[:, 0].to_numpy()
            col_b = aligned.iloc[:, 1].to_numpy()
            if np.std(col_r) > 0 and np.std(col_b) > 0:
                metrics["correlation"] = float(np.corrcoef(col_r, col_b)[0, 1])

    return {
        "realized_index": realized_index,
        "backtest_index": backtest_index,
        "metrics": metrics,
    }
