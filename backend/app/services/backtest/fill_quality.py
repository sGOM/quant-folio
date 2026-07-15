"""실거래–백테스트 체결 정합 실측 (P2-3).

라이브 매매의 실제 체결(orders/executions)을, 백테스트 체결모형이 가정하는 값과
비교해 "가정이 현실을 얼마나 잘 근사하는가"를 bp 단위로 계량한다. 백테스트 코어
(_fill_at 등)는 건드리지 않는 별도 분석 파이프라인이다.

────────────────────────────────────────────────────────────────────────
배경(코드 사실)
────────────────────────────────────────────────────────────────────────
- 백테스트 체결모형(app/services/backtest/portfolio.py, fill_mode="next_close" 기본):
  신호일 t 의 결정을 t+1 종가에 체결한다. 매수 = C_{t+1}·(1+s), 매도 = C_{t+1}·(1−s),
  s = slippage_bps/1e4(기본 5bp). slippage_vol_scale>0 이면 종목별 변동성으로 조정
  (slippage._vol_slippage_map). 세금(매도 20bp)·수수료(1.5bp)는 체결가와 별도로
  차감되므로 이 정합 비교에서는 제외한다(체결가 자체만 비교).
- 라이브 체결(engine/executor.py): 시장가로 신호 당일(t) 장중 즉시 체결. Order.price 에
  신호 시점 결정가, Order.created_at 에 주문 시각, Execution.filled_price/filled_qty/
  executed_at 에 실제 체결(부분체결 시 다건)이 남는다.

────────────────────────────────────────────────────────────────────────
3분해 지표 (부호 규약: d=+1 매수 / −1 매도, g(P,R)=d·(P−R)/R·1e4, 비용=양수)
────────────────────────────────────────────────────────────────────────
- M1 실행 슬리피지(실행 품질) g_exec = g(P_live, R_arrival)
    P_live      = 주문별 체결수량가중평균 filled_price
    R_arrival   = Order.price(신호 결정가). 없으면 신호일 종가 C_t 로 폴백(arrival_fallback 태깅)
    → 백테스트의 5bp 가정을 직접 검증하는 값(주문 결정가 대비 실제 체결의 미끄러짐).
- M2 시점 규약 표류 g_time = g(C_t, C_{t+1})
    라이브가 체결한 당일(t) 종가 vs 백테스트가 가정하는 t+1 종가 사이의 시장 이동을
    매매 방향으로 부호화. next_close 규약이 만드는 계통적 시점차의 크기.
- M3 총 정합 괴리 g_total = g(P_live, P_bt),  P_bt = C_{t+1}·(1 + d·s)
    s 는 _vol_slippage_map 을 백테스트와 동일 공식으로 그 자리에서 재호출해 산출.
    검산 항등식(1차 근사): g_total ≈ g_exec − s_bp + g_time.

────────────────────────────────────────────────────────────────────────
집계·판정
────────────────────────────────────────────────────────────────────────
- 대상: 전략 리밸런싱 주문만(strategy_id IS NOT NULL), REJECTED·미체결 제외.
- M1/M2/M3 각각 평균·표준편차·중앙값·p90·p95, 매수/매도 방향 분리 집계.
- 연환산 드래그 = 연 회전율 × 편도 평균(M1) (bp/yr). 백테스트 slippage 가정과의 차이가 핵심 요약치.
- M1 판정(편도 평균 μ, 표준편차 σ, 가정 a=slippage_bps):
    INSUFFICIENT(N<min_sample) / GREEN(μ≤5bp, σ≤2a) / AMBER(5<μ≤15bp) / RED(μ>15bp 또는 σ>3a)
- M3 판정(연환산 드래그 차이 Δ, %/yr):
    INSUFFICIENT(회전율 미상 또는 N<min_sample) / GREEN(Δ≤0.5) / AMBER(0.5<Δ≤1.5) / RED(Δ>1.5)
- 표본 부족(방향당 N<min_sample, 기본 30)은 등급 대신 "판정 보류(INSUFFICIENT)".
- 리밸런싱일 클러스터(같은 날 매매는 공통 시장충격 공유 → 독립 아님)는 표준 std 로 근사하고
  cluster-robust SE 는 생략한다(n_clusters=고유 체결일 수 를 함께 보고, 클러스터 상관 미보정).
- 종가는 백테스트 패널과 동일 소스(load_ohlcv → pykrx/FDR 일봉 수정종가)로 조회해
  _vol_slippage_map 재호출과 일관되게 맞춘다. price_ticks 의 장중 raw 는 종가로 쓰지 않는다.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from app.services.backtest.slippage import _vol_slippage_map

# 백테스트 기본 슬리피지 가정(bp). config 에 slippage_bps 가 있으면 그 값을 쓴다.
DEFAULT_SLIP_BPS = 5.0
DEFAULT_MIN_SAMPLE = 30


def _g(p: float, r: float, d: int) -> float:
    """방향 부호화 상대차(bp). d=+1 매수·−1 매도. 양수 = 매매에 불리(비용)."""
    return d * (p - r) / r * 1e4


def _stats(values: list[float]) -> dict[str, Any]:
    """분포 요약(개수·평균·표준편차·중앙값·p90·p95). 표본이 없으면 None 필드."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "std": None, "median": None, "p90": None, "p95": None}
    arr = np.asarray(values, dtype="float64")
    return {
        "n": n,
        "mean": float(arr.mean()),
        # 표본 표준편차(ddof=1). n=1 이면 정의되지 않으므로 0.0.
        "std": float(arr.std(ddof=1)) if n >= 2 else 0.0,
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
        "p95": float(np.percentile(arr, 95)),
    }


def _grade_m1(mean: float | None, std: float | None, n: int,
              assumption: float, min_sample: int) -> str:
    """M1(실행 슬리피지) 등급. mean/std 는 편도(one-way) bp."""
    if n < min_sample or mean is None:
        return "INSUFFICIENT"
    sd = std or 0.0
    if mean > 15.0 or sd > 3.0 * assumption:
        return "RED"
    if mean <= 5.0 and sd <= 2.0 * assumption:
        return "GREEN"
    return "AMBER"


def _grade_m3(drag_diff_pct: float | None, n: int, min_sample: int) -> str:
    """M3(총 정합) 등급 — 연환산 드래그 차이(%/yr) 기준. 회전율 미상이면 INSUFFICIENT."""
    if drag_diff_pct is None or n < min_sample:
        return "INSUFFICIENT"
    if drag_diff_pct <= 0.5:
        return "GREEN"
    if drag_diff_pct <= 1.5:
        return "AMBER"
    return "RED"


def _panel_index_pos(panel: pd.DataFrame, day: pd.Timestamp) -> int | None:
    """패널에서 체결일 t 의 위치를 찾는다. 정확 일치 우선, 없으면 t 이하 최근 거래일."""
    idx = panel.index
    if day in idx:
        return int(idx.get_loc(day))
    pos = int(idx.searchsorted(day, side="right")) - 1
    if pos < 0:
        return None
    return pos


def compute_fill_quality(
    orders: list[dict[str, Any]],
    close_panel: pd.DataFrame,
    *,
    slip_base: float,
    slip_vol_scale: float,
    backtest_slip_bps: float = DEFAULT_SLIP_BPS,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    annual_turnover: float | None = None,
) -> dict[str, Any]:
    """3분해(M1/M2/M3) 정합 지표를 계산한다(순수 함수).

    :param orders: 정규화된 체결 주문 리스트. 각 dict 키:
        order_id(int), symbol(str), side('buy'|'sell'), decision_price(float|None),
        fill_date(pd.Timestamp|date, 라이브 체결일 t), p_live(float, 수량가중평균 체결가),
        filled_qty(float)
    :param close_panel: 일봉 종가 패널(columns=종목, index=정규화 DatetimeIndex). 백테스트
        패널과 동일 소스여야 _vol_slippage_map 재호출이 일관된다.
    :param slip_base: 백테스트 슬리피지 분수(slippage_bps/1e4). P_bt 산출·s 재호출에 사용.
    :param slip_vol_scale: 백테스트 slippage_vol_scale(0 이면 고정 slip_base).
    :param backtest_slip_bps: 백테스트가 가정한 편도 슬리피지(bp) — M1 판정·드래그 차이 기준.
    :param min_sample: 방향당 표본 하한(미만이면 판정 보류).
    :param annual_turnover: 편도 연 회전율(예: 4.0=400%/yr). None 이면 M3 드래그 판정 보류.
    :return: per_order 상세 + M1/M2/M3 통계·등급 dict.
    """
    panel = close_panel
    if not isinstance(panel.index, pd.DatetimeIndex):
        panel = panel.copy()
        panel.index = pd.DatetimeIndex(panel.index)
    panel = panel.sort_index()

    per_order: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    m1: list[float] = []
    m2: list[float] = []
    m3: list[float] = []
    m1_by = {"buy": [], "sell": []}
    m2_by = {"buy": [], "sell": []}
    m3_by = {"buy": [], "sell": []}
    residuals: list[float] = []
    fill_dates: set[pd.Timestamp] = set()
    n_arrival_fallback = 0

    # 체결일(클러스터)별로 묶는다. _vol_slippage_map 의 종목별 s 는 그날 바스켓의
    # 크로스섹션 중앙값에 의존하므로(백테스트는 목표+보유 전체 바스켓으로 호출), 같은
    # 날 체결 종목들을 한 번에 넘겨 백테스트 공식을 최대한 재현한다. 다만 여기 바스켓은
    # '체결된 종목'뿐이라(백테스트는 목표+보유 전체) 중앙값이 다를 수 있다 — 근사치.
    groups: dict[pd.Timestamp, list[dict[str, Any]]] = defaultdict(list)
    for o in orders:
        groups[pd.Timestamp(o["fill_date"]).normalize()].append(o)

    for t in sorted(groups):
        day_orders = groups[t]
        pos = _panel_index_pos(panel, t)
        if pos is None or pos + 1 >= len(panel.index):
            for o in day_orders:
                skipped.append({"order_id": o.get("order_id"), "reason": "no_next_close"})
            continue
        t1 = panel.index[pos + 1]
        # 그날 체결 종목 전체로 슬리피지 맵을 한 번 산출(백테스트 바스켓 호출 재현).
        day_symbols = [o["symbol"] for o in day_orders if o["symbol"] in panel.columns]
        smap = _vol_slippage_map(panel.loc[:t1], day_symbols, slip_base, slip_vol_scale)

        for o in day_orders:
            symbol = o["symbol"]
            side = o["side"]
            d = 1 if side == "buy" else -1
            p_live = float(o["p_live"])

            if symbol not in panel.columns:
                skipped.append({"order_id": o.get("order_id"), "reason": "no_price_symbol"})
                continue
            col = panel[symbol]
            c_t = col.iloc[pos]
            c_t1 = col.iloc[pos + 1]
            if not (pd.notna(c_t) and pd.notna(c_t1) and c_t > 0 and c_t1 > 0 and p_live > 0):
                skipped.append({"order_id": o.get("order_id"), "reason": "invalid_price"})
                continue
            c_t = float(c_t)
            c_t1 = float(c_t1)

            # R_arrival: 결정가 우선, 없으면 신호일 종가 C_t 폴백(태깅).
            dp = o.get("decision_price")
            if dp is not None and float(dp) > 0:
                r_arrival = float(dp)
                arrival_fallback = False
            else:
                r_arrival = c_t
                arrival_fallback = True
                n_arrival_fallback += 1

            s = smap.get(symbol, slip_base)
            s_bp = s * 1e4
            p_bt = c_t1 * (1.0 + d * s)

            g_exec = _g(p_live, r_arrival, d)   # M1
            g_time = _g(c_t, c_t1, d)           # M2
            g_total = _g(p_live, p_bt, d)       # M3
            residual = g_total - (g_exec - s_bp + g_time)  # 검산 잔차

            m1.append(g_exec)
            m2.append(g_time)
            m3.append(g_total)
            m1_by[side].append(g_exec)
            m2_by[side].append(g_time)
            m3_by[side].append(g_total)
            residuals.append(residual)
            fill_dates.add(t)

            per_order.append({
                "order_id": o.get("order_id"),
                "symbol": symbol,
                "side": side,
                "fill_date": t.date().isoformat(),
                "next_close_date": pd.Timestamp(t1).date().isoformat(),
                "p_live": p_live,
                "r_arrival": r_arrival,
                "arrival_fallback": arrival_fallback,
                "c_t": c_t,
                "c_t1": c_t1,
                "s_bp": s_bp,
                "p_bt": p_bt,
                "m1_exec_bp": g_exec,
                "m2_time_bp": g_time,
                "m3_total_bp": g_total,
                "checksum_residual_bp": residual,
            })

    n = len(m1)
    m1_all = _stats(m1)
    m3_all = _stats(m3)

    # 연환산 드래그: 회전율 × 편도 평균(M1). 차이 = 측정 M1 − 백테스트 가정.
    drag_abs_pct = drag_diff_pct = None
    if annual_turnover is not None and m1_all["mean"] is not None:
        drag_abs_pct = annual_turnover * m1_all["mean"] / 1e4 * 100.0
        drag_diff_pct = annual_turnover * (m1_all["mean"] - backtest_slip_bps) / 1e4 * 100.0

    def _metric_block(all_vals, by_vals):
        return {
            "all": _stats(all_vals),
            "buy": _stats(by_vals["buy"]),
            "sell": _stats(by_vals["sell"]),
        }

    return {
        "sample": {
            "n_orders": n,
            "n_skipped": len(skipped),
            "n_clusters": len(fill_dates),
            "n_arrival_fallback": n_arrival_fallback,
            "min_sample": min_sample,
            "note": "클러스터(동일 체결일) 상관 미보정 — 표준 std 근사.",
        },
        "assumptions": {
            "backtest_slip_bps": backtest_slip_bps,
            "slip_base_bps": slip_base * 1e4,
            "slip_vol_scale": slip_vol_scale,
            "annual_turnover": annual_turnover,
        },
        "m1_exec": _metric_block(m1, m1_by),      # 실행 슬리피지
        "m2_time": _metric_block(m2, m2_by),      # 시점 규약 표류
        "m3_total": _metric_block(m3, m3_by),     # 총 정합 괴리
        "checksum": {
            "mean_abs_residual_bp": (
                float(np.mean(np.abs(residuals))) if residuals else None
            ),
            "note": "g_total ≈ g_exec − s_bp + g_time (1차 근사 항등식)",
        },
        "annualized_drag": {
            "drag_abs_pct_per_yr": drag_abs_pct,
            "drag_diff_pct_per_yr": drag_diff_pct,
        },
        "grades": {
            "m1_exec": _grade_m1(
                m1_all["mean"], m1_all["std"], n, backtest_slip_bps, min_sample
            ),
            "m3_total": _grade_m3(drag_diff_pct, n, min_sample),
        },
        "per_order": per_order,
        "skipped": skipped,
    }
