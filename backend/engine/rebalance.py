"""리밸런싱 코어 로직 — 순수함수(테스트 용이).

- compute_target_weights: 선정 규칙(모멘텀 상위 N / 전체)으로 목표 비중 산정
- compute_rebalance_orders: 목표 비중 vs 현재 보유로 드리프트 밴드 초과분만 주문 생성
- is_rebalance_due: cadence(일/주/월)·실행시각·영업일/장중 여부로 발화 시점 판정

엔진의 RebalanceRunner 가 이 함수들을 조합해 KIS 주문을 실행한다. I/O 는 일절
수행하지 않아 단위 테스트가 쉽다.
"""
from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from app.services.market import is_market_open


def compute_target_weights(
    price_history: dict[str, pd.Series], cfg: dict
) -> dict[str, float]:
    """선정 규칙에 따라 (선정 종목 → 목표 비중) 을 반환한다.

    :param price_history: 종목코드 → 종가 Series(시간 오름차순). 데이터 부족 종목은 제외.
    :param cfg: 전략 config(selection.method/lookback/top_n, weighting).
    :return: 선정 종목의 목표 비중 dict(동일비중). 미선정 종목은 키에 없음(=목표 0).
    """
    selection = cfg.get("selection", {})
    method = selection.get("method", "momentum")
    lookback = int(selection.get("lookback", 120))
    top_n = int(selection.get("top_n", 5))

    universe = list(cfg.get("universe", []))

    if method == "all":
        selected = [s for s in universe if _has_data(price_history.get(s), 1)]
    else:  # momentum
        scored: list[tuple[str, float]] = []
        for sym in universe:
            series = price_history.get(sym)
            if not _has_data(series, lookback + 1):
                continue
            past = float(series.iloc[-(lookback + 1)])
            now = float(series.iloc[-1])
            if past <= 0:
                continue
            scored.append((sym, now / past - 1.0))
        scored.sort(key=lambda x: x[1], reverse=True)
        selected = [sym for sym, _ in scored[:top_n]]

    if not selected:
        return {}
    weight = 1.0 / len(selected)
    return {sym: weight for sym in selected}


def _has_data(series: pd.Series | None, need: int) -> bool:
    """Series 가 need 개 이상의 유효 데이터를 가지는지."""
    return series is not None and len(series.dropna()) >= need


def compute_rebalance_orders(
    targets: dict[str, float],
    positions: dict[str, float],
    prices: dict[str, float],
    capital: float,
    drift_band: float,
) -> list[tuple[str, str, int]]:
    """목표 비중과 현재 보유를 비교해 드리프트 밴드 초과 종목의 주문을 생성한다.

    현재 비중(보유가치/capital)과 목표 비중의 편차 절댓값이 drift_band 이하이면
    매매하지 않는다(수수료·세금 절감). 미선정(목표 0) 종목은 전량 매도 대상이다.

    :return: (symbol, side, qty) 리스트. 매도(sell) 를 매수(buy) 보다 앞에 둔다
        (현금을 먼저 확보해 매수 자금 부족을 줄임). qty 는 KRX 1주 단위.
    """
    if capital <= 0:
        return []

    sells: list[tuple[str, str, int]] = []
    buys: list[tuple[str, str, int]] = []

    for sym in set(targets) | set(positions):
        price = prices.get(sym)
        if price is None or price <= 0:
            continue  # 현재가 없으면 매매 불가(다음 주기로 미룸)

        target_w = targets.get(sym, 0.0)
        cur_qty = int(positions.get(sym, 0) or 0)
        cur_w = (cur_qty * price) / capital

        if abs(cur_w - target_w) <= drift_band:
            continue

        target_qty = math.floor(target_w * capital / price)
        delta = target_qty - cur_qty
        if delta == 0:
            continue
        if delta > 0:
            buys.append((sym, "buy", delta))
        else:
            sells.append((sym, "sell", -delta))

    return sells + buys


def parse_time(hhmm: str) -> tuple[int, int]:
    """'HH:MM' → (hour, minute). 형식 오류 시 (0,0) 으로 폴백하지 않고 예외."""
    hh, mm = hhmm.split(":")
    return int(hh), int(mm)


def _period_key(dt: datetime, cadence: str):
    """cadence 별 '같은 주기' 식별 키 — 동일 키면 이미 실행한 주기로 본다."""
    if cadence == "daily":
        return dt.date()
    if cadence == "weekly":
        iso = dt.isocalendar()
        return (iso[0], iso[1])  # (ISO year, ISO week)
    return (dt.year, dt.month)  # monthly


def is_rebalance_due(
    cfg: dict, last_dt: datetime | None, now: datetime
) -> bool:
    """지금 리밸런싱을 실행해야 하는지 판정한다.

    조건(모두 충족 시 True):
      1) 정규장 운영 중(영업일 + 09:00~15:30 KST) — is_market_open
      2) 현재 시각이 설정 실행시각(rebalance_time) 이상
      3) 이번 cadence 주기에 아직 실행하지 않음(last_dt 의 주기 ≠ now 의 주기)
      4) weekly: now.weekday() ≥ rebalance_weekday / monthly: now.day ≥ rebalance_dom
         (지정일이 휴장이면 같은 주기 내 다음 영업일에 자연 발화)

    :param last_dt: 마지막 리밸런싱 실행 시각(KST), 없으면 None
    :param now: 현재 시각(KST, tz-aware 권장)
    """
    cadence = cfg.get("cadence", "monthly")

    if not is_market_open(now):
        return False

    h, m = parse_time(cfg.get("rebalance_time", "14:30"))
    if (now.hour, now.minute) < (h, m):
        return False

    if last_dt is not None and _period_key(last_dt, cadence) == _period_key(now, cadence):
        return False

    if cadence == "weekly":
        if now.weekday() < int(cfg.get("rebalance_weekday") or 0):
            return False
    elif cadence == "monthly":
        if now.day < int(cfg.get("rebalance_dom") or 1):
            return False

    return True
