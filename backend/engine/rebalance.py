"""리밸런싱 코어 로직 — 순수함수(테스트 용이).

- compute_target_weights: 선정 규칙(모멘텀 상위 N / 전체)으로 목표 비중 산정
- compute_rebalance_orders: 목표 비중 vs 현재 보유로 드리프트 밴드 초과분만 주문 생성
- is_rebalance_due: cadence(일/주/월)·실행시각·영업일/장중 여부로 발화 시점 판정

엔진의 RebalanceRunner 가 이 함수들을 조합해 KIS 주문을 실행한다. I/O 는 일절
수행하지 않아 단위 테스트가 쉽다.
"""
from __future__ import annotations

import calendar
import math
from datetime import datetime

import pandas as pd

from app.services.market import is_market_open

#: inverse_vol 비중의 종목당 하한/상한 캡(과소·과집중 방지). 저변동 종목이라도 하한
#: 미만으로는, 고변동 종목이라도 상한 초과로는 배분하지 않는다. 캡이 종목수와
#: 근본적으로 안 맞으면(예: top_n 이 너무 작아 상한×N<1) 균등비중으로 자동 폴백한다
#: (cap_normalize_weights 참고). drift_band 를 크게 잡으면 하한 근처 비중 종목이
#: 영원히 미체결될 수 있으니(engine.rebalance.compute_rebalance_orders 의
#: abs(dev)<=drift_band 스킵) inverse_vol 전략은 drift_band 를 작게 설정할 것.
INVERSE_VOL_MIN_WEIGHT = 0.005
INVERSE_VOL_MAX_WEIGHT = 0.15


def _vol_ann_from_series(series: pd.Series | None) -> float | None:
    """종가 Series 로 연율 변동성을 계산한다(app.services.metrics._compute_vol_ann 과 동일 정의).

    지연 임포트: 이 모듈은 app.services.backtest.portfolio 가 최상위에서 임포트하므로,
    여기서 app.services.metrics 를 최상위 임포트하면 순환 임포트가 된다
    (portfolio → engine.rebalance → app.services.metrics → app.services.backtest.signals).
    """
    from app.services.metrics import _compute_vol_ann  # 지연(순환 회피)

    if series is None:
        return None
    return _compute_vol_ann(series.dropna().tail(253))


def cap_normalize_weights(
    raw_weights: dict[str, float],
    lo: float = INVERSE_VOL_MIN_WEIGHT,
    hi: float = INVERSE_VOL_MAX_WEIGHT,
) -> dict[str, float]:
    """비중을 정규화하고 [lo, hi] 캡을 적용한다(water-filling 반복). 합은 항상 1.0.

    상한에 걸리는 종목은 hi 로 고정하고 잔여분을 나머지 종목에 비례 재분배하는 과정을
    수렴할 때까지 반복한다(하한도 동일). 캡이 종목 수와 근본적으로 맞지 않으면
    (lo*n>1 또는 hi*n<1, 예: top_n 이 너무 작음) 캡을 지키면서 합=1 을 만들 수 없으므로
    균등비중으로 폴백한다.
    """
    n = len(raw_weights)
    if n == 0:
        return {}
    if n == 1:
        return {next(iter(raw_weights)): 1.0}
    if lo * n > 1.0 + 1e-9 or hi * n < 1.0 - 1e-9:
        w = 1.0 / n
        return {k: w for k in raw_weights}

    total = sum(raw_weights.values())
    if total <= 0:
        w = 1.0 / n
        return {k: w for k in raw_weights}

    weights = {k: v / total for k, v in raw_weights.items()}
    fixed: dict[str, float] = {}
    free = dict(weights)
    # 매 라운드 상한 위반자를 '먼저' hi 로 고정해 잔여질량을 덜어낸 뒤 재분배한다(하한
    # 위반자는 그 다음 라운드에). 상하한을 동시에 고정하면(같은 라운드), 아직 상한
    # 위반자의 초과분이 빠지지 않은 상태의 비율로 하한 위반 여부를 오판해(예: 소수
    # 종목이 전체 raw 비중을 독식) 재분배 기회를 잃고 합<1 이 될 수 있다.
    for _ in range(n + 2):
        if not free:
            break
        remaining = 1.0 - sum(fixed.values())
        free_total = sum(free.values())
        if free_total <= 0:
            share = remaining / len(free)
            fixed.update({k: share for k in free})
            free = {}
            break
        scaled = {k: v / free_total * remaining for k, v in free.items()}
        over = {k for k, v in scaled.items() if v > hi + 1e-12}
        under = {k for k, v in scaled.items() if v < lo - 1e-12}
        if not over and not under:
            fixed.update(scaled)
            free = {}
            break
        violated = over or under  # 상한 위반을 우선 고정, 없으면 하한 위반을 고정
        bound = hi if over else lo
        for k in violated:
            fixed[k] = bound
            free.pop(k, None)
    if free:  # 안전망: 드물게 수렴 안 하면 남은 자유 종목을 균등 배분
        remaining = 1.0 - sum(fixed.values())
        share = remaining / len(free)
        fixed.update({k: share for k in free})
    return fixed


def inverse_vol_weights(
    vols: dict[str, float | None],
    selected: list[str],
    lo: float = INVERSE_VOL_MIN_WEIGHT,
    hi: float = INVERSE_VOL_MAX_WEIGHT,
) -> dict[str, float]:
    """선정 종목에 인버스-변동성(1/vol) 비중을 배분한다(risk-parity 근사).

    vol 이 없거나(None/NaN) 0 이하인 종목은 인버스볼 계산에서 제외한다(0 나눔 방지·
    음수 변동성 방어). 유효 종목이 하나도 없으면 selected 전체에 동일비중을 배분한다
    (equal 폴백).

    :param vols: 종목코드 → 연율 변동성(vol_ann). selected 의 부분집합이어도 된다.
    :param selected: 이미 선정된(selection.method 무관) 종목 목록.
    :return: selected 전체를 키로 갖는 비중 dict(합=1.0). 무효 vol 종목은 0.0.
    """
    if not selected:
        return {}
    valid: dict[str, float] = {}
    for sym in selected:
        v = vols.get(sym)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(v) or v <= 0:
            continue
        valid[sym] = v
    if not valid:
        w = 1.0 / len(selected)
        return {sym: w for sym in selected}
    raw = {sym: 1.0 / v for sym, v in valid.items()}
    weighted = cap_normalize_weights(raw, lo, hi)
    return {sym: weighted.get(sym, 0.0) for sym in selected}


def compute_target_weights(
    price_history: dict[str, pd.Series],
    cfg: dict,
    scores: dict[str, float] | None = None,
    vols: dict[str, float] | None = None,
) -> dict[str, float]:
    """선정 규칙에 따라 (선정 종목 → 목표 비중) 을 반환한다.

    :param price_history: 종목코드 → 종가 Series(시간 오름차순). momentum/all/custom 선정과
        weighting="inverse_vol" 의 변동성 산출(vols 미주입 시)에 사용된다. method="score"
        에서는 선정에 참조하지 않는다(점수만으로 선정).
    :param cfg: 전략 config(selection.method/lookback/top_n/factor_weights, weighting).
    :param scores: method="score" 일 때 사용할 (종목코드 → 종합점수) dict. 호출자
        (RebalanceRunner/백테스트)가 미래참조 없는 as_of(직전 확정 영업일) 기준으로
        app.services.metrics.compute_universe_scores 등을 통해 미리 계산해 주입한다.
        None/미포함 종목은 선정 후보에서 제외된다.
    :param vols: weighting="inverse_vol" 일 때 사용할 (종목코드 → 연율 변동성) dict.
        호출자가 이미 계산해 둔 값(예: 백테스트의 _score_factor_frame.vol_ann)이 있으면
        재계산을 피하기 위해 주입한다. None 이면 price_history 로부터 산출한다
        (app.services.metrics._compute_vol_ann 과 동일 정의 — 실거래/백테스트 정합성).
    :return: 선정 종목의 목표 비중 dict. weighting="equal"(기본)이면 동일비중,
        weighting="score"(method="score" 전용)이면 순위 기반 선형 가중(_rank_weighted),
        weighting="inverse_vol"이면 인버스-변동성 비중(상하한 캡 적용, 모든 method 허용).
        미선정 종목은 키에 없음(=목표 0).
    """
    selection = cfg.get("selection", {})
    method = selection.get("method", "momentum")
    lookback = int(selection.get("lookback", 120))
    top_n = int(selection.get("top_n", 5))
    weighting = cfg.get("weighting", "equal")

    universe = list(cfg.get("universe", []))
    ranked: list[tuple[str, float]] | None = None  # (symbol, score) 내림차순, score 방식 전용

    if method == "score":
        scores = scores or {}
        candidates = [
            (sym, scores[sym])
            for sym in universe
            if sym in scores and scores[sym] is not None and not math.isnan(scores[sym])
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)
        ranked = candidates[:top_n]
        selected = [sym for sym, _ in ranked]
    elif method == "custom":
        # 각 종목에 사용자 정의 진입/청산 규칙을 독립 적용해 '현재 진입 국면'인 종목만 선정.
        # 종가 시계열만 사용(리밸런싱 데이터 경로가 종가 패널이라 스키마가 종가지표로 제한).
        from app.services.backtest.signals import (  # 지연 임포트(모듈 로드 비용·순환 회피)
            _custom_min_periods,
            custom_position_state,
        )

        rule_cfg = {"entry": selection.get("entry"), "exit": selection.get("exit")}
        need = _custom_min_periods(rule_cfg)
        selected = []
        for sym in universe:
            series = price_history.get(sym)
            if not _has_data(series, need):
                continue
            state = custom_position_state(series.dropna(), rule_cfg)
            if len(state) and float(state.iloc[-1]) > 0:
                selected.append(sym)
    elif method == "all":
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

    if weighting == "score" and ranked is not None:
        return _rank_weighted(ranked)

    if weighting == "inverse_vol":
        if vols is None:
            vols = {sym: _vol_ann_from_series(price_history.get(sym)) for sym in selected}
        return inverse_vol_weights(vols, selected)

    weight = 1.0 / len(selected)
    return {sym: weight for sym in selected}


def _rank_weighted(ranked: list[tuple[str, float]]) -> dict[str, float]:
    """순위 기반 선형 가중치(1위가 최대, 꼴찌도 항상 양수).

    점수는 부호·스케일이 임의(z-score 합성이라 음수 가능)이므로 점수값 자체를
    비중으로 정규화하면 음수·0 근접값에서 불안정하다. 대신 순위만 사용해
    n위 종목에 (N-순위+1) 에 비례하는 비중을 부여한다(1위=N, 꼴찌=1, 합=N(N+1)/2).
    :param ranked: (symbol, score) 내림차순 정렬된 선정 종목 리스트.
    """
    n = len(ranked)
    total = n * (n + 1) / 2
    return {sym: (n - i) / total for i, (sym, _) in enumerate(ranked)}


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

        # 드리프트 밴드는 '이미 보유 중인 종목의 비중 미세조정'에만 적용한다.
        # 신규 편입(미보유→목표)과 전량 청산(보유→목표 0)은 목표 비중이 밴드보다
        # 작아도 반드시 체결해야 한다. (top_n 이 커서 등비중 목표가 drift_band 이하가
        # 되면 편입이 영구 차단되는 버그 방지 — 100% 현금 고착)
        is_new_entry = cur_qty <= 0 and target_w > 0.0
        is_full_exit = target_w <= 0.0 and cur_qty > 0
        if not (is_new_entry or is_full_exit) and abs(cur_w - target_w) <= drift_band:
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
    if cadence == "quarterly":
        return (dt.year, (dt.month - 1) // 3)  # (year, 분기 0~3)
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
        # 장은 평일(weekday 0~4)에만 열리므로 weekday=5(토)/6(일) 설정 시 is_market_open 이
        # 참인 시각엔 항상 now.weekday()<5 → 영구 미발화. 0~4 로 클램프한다.
        wd = min(max(int(cfg.get("rebalance_weekday") or 0), 0), 4)
        if now.weekday() < wd:
            return False
    elif cadence in ("monthly", "quarterly"):
        # dom 이 해당 월 일수를 넘으면(예: 31일 지정 → 4/6/9/11월은 최대 30일, 2월은 28·29일)
        # now.day 가 절대 dom 에 도달하지 못해 그 주기를 통째로 건너뛴다. 월 마지막 날로
        # 클램프해 최소한 월말에는 발화하도록 한다(1 미만도 1 로 하한).
        dom = min(max(int(cfg.get("rebalance_dom") or 1), 1),
                  calendar.monthrange(now.year, now.month)[1])
        if now.day < dom:
            return False

    return True
