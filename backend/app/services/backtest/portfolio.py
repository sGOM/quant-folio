"""리밸런싱(다종목 포트폴리오) 백테스트 엔진.

단일종목 vectorbt 엔진(engine.py)과 달리, universe 를 주기적으로 재선정·리밸런싱하는
전략(RebalanceConfig)의 성과를 일별 시뮬레이션으로 계산한다.

설계 원칙
=========
- **실거래와 동일 로직 재사용**: 종목 선정·목표비중은 실거래 러너가 쓰는
  engine.rebalance.compute_target_weights 를, 종합점수는 metrics._compute_stock_scores 를
  그대로 재사용해 백테스트/실거래 정합성을 보장한다.
- **미래참조(look-ahead) 방지·체결 현실성(P0-1)**: 리밸런싱일 t 의 선정은 t 종가까지의
  데이터만 사용한다. 체결 시점은 fill_mode 로 정한다 —
    · next_close(기본): 익일 종가(t+1)에 체결해 당일 종가 미래참조를 제거한다(신호↔체결 분리).
    · same_close: 당일 종가(t) 즉시 체결(구 동작, 민감도 분석용 opt-in).
  레짐 청산·재진입 결정도 동일 규약으로 지연 체결된다.
- **거래비용·슬리피지**: 매수 레그는 위탁수수료(fees)+슬리피지, 매도 레그는 위탁수수료+
  증권거래세(fees+tax)+슬리피지를 거래대금에 비례해 차감한다. 슬리피지(slippage_bps)는
  편도 기준이며, slippage_vol_scale>0 이면 종목별 최근 변동성/중앙값 비로 조정한다. 드리프트
  밴드 내 종목은 매매하지 않아 회전율·비용을 줄인다.
- **현금화 오버레이(레짐 필터)**: regime_filter 가 켜져 있으면 기준지수가 이동평균 아래로
  내려간 국면(risk-off)에서는 즉시 보유를 청산해 현금화하고 신규 매수를 중단한다. 지수가
  이동평균 위로 회복(risk-off→risk-on 전환)하면 현금 상태에서 그날 즉시 신규 선정·매수로
  재진입한다(cadence 를 기다리지 않는다). V자 반등 초기 수익을 놓치지 않기 위함이다.
- **거래 로그**: 매수·매도 1건마다 체결가·거래대금과 '그 순간'의 포트폴리오 누적수익률,
  매도 시 해당 포지션의 보유수익률(원가 대비)을 trades 로 남겨 사후 전략 개선에 활용한다.
- **패닉 오버레이(P2)**: config.panic_overlay 가 켜져 있으면 regime_filter 위에 추가로
  Arm(자본항복 후보 감지)→Confirm(재진입 확인)→Fill(익일 종가 체결) 상태기계가 동작한다.
  `app.services.metrics.panic.compute_panic_series` 로 만든 KOSPI 종합지수 롤링 패닉
  지표가 warning/panic 라벨(또는 hard_trigger)을 찍은 날 Arm 되고, 이후 arm_window 거래일
  내 지수가 그날 종가를 상회+당일 등락률(+)로 반등하면 Confirm 되어 목표 노출을
  base_exposure→panic_exposure 로 확대(강제 재진입, regime_exit 보다 우선)한다.
  손절(knife_stop_pct, 자본항복일 저가 대비)·익절(profit_reclaim_pct, 낙폭 되돌림)·
  시간청산(hold_days) 중 먼저 도래하는 조건에 오버레이를 해제한다. 자세한 설계 의도는
  app.schemas.strategy.PanicOverlay 문서 참고.

체결 정밀도(P2-2)
=================
- **정수주(A-1)**: config.integer_shares(기본 True)가 켜져 있으면 매수·매도 거래대금을
  floor(금액/가격) 주 단위로 절사한다(실거래 compute_rebalance_orders 와 동일 규약).
  전량 청산(포지션 정리)은 절사하지 않고 보유 전량을 그대로 정리한다(ADV 캡으로 줄어들지
  않는 한 잔여주가 남지 않아야 하므로). 절사로 남는 소수점 금액은 자동으로 현금에 남는다
  (val/cost 는 정수주 가치에 정확히 맞춰 갱신되므로 별도 이월 부기가 필요 없다).
- **ADV 참여율 캡(A-2)**: config.adv_participation_cap(예 0.08)과 volume_panel 이 모두
  주어지면, 종목별 20일 ADV(거래대금, rolling·미래참조 없음)의 그 비율을 하루 매매 상한으로
  삼아 초과분을 절사한다(잔량은 다음 리밸런싱에서 재평가 — 이월 큐를 별도로 두지 않는
  단순화된 모델). volume_panel 미공급 시 설정돼 있어도 경고 로그만 남기고 미적용.
- **호가단위·상하한가(price_limit_model, opt-in, §4)**: config.price_limit_model=True 면
  체결가를 KRX 가격대별 호가단위로 라운딩하고, 전일종가 대비 ±30% 상하한가에 도달한
  방향의 주문(매수는 상한가, 매도는 하한가)을 그날 체결 불가로 막아 다음 리밸런싱으로
  이월한다. 기본 False — 기존 결과 재현성 보존(옵트인 재검증 대상, docs/improvements.md §4).

알려진 한계(실거래와의 차이)
--------------------------
- 목표비중은 '현재 포트폴리오 평가액'(복리) 기준으로 산정한다(실거래는 배정 capital 고정).
  백테스트는 복리 성장을 반영하는 것이 성과추정에 더 적절하다.
- 슬리피지는 slippage_bps(변동성 스케일 옵션)로 반영한다. 상하한가·호가단위는
  price_limit_model 옵트인으로만 반영된다(기본 미적용 시 슬리피지에 근사 흡수돼 있다고
  보수적으로 해석할 것).
- 회전율(turnover)·거래 로그의 거래대금은 기본적으로 ADV 캡·정수주 절사 '이전'(의도된
  드리프트 보정치) 기준이다 — 실제 체결액(ADV 캡·정수주 절사·상하한가 체결불가 반영
  이후)은 avg_turnover_actual 로 별도 노출한다(§7). 개별 거래 로그(trades[].amount)는
  애초부터 이 절사들을 모두 반영한 실체결액이다(집계 지표만 사전/사후 두 버전이 있는 것).
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

# 책임별로 분리한 하위 모듈의 헬퍼를 재노출한다(기존 import 경로 보존).
# rebalance_runner·검증 스크립트·테스트가 portfolio 에서 직접 import 하므로
# 여기서 되살려 두어야 한다.
from app.services._num import _safe, is_nan  # NaN/inf → None(JSON 안전)
from app.services.backtest.attribution import (
    _FACTOR_SCORE_COLS,
    _factor_attribution,
    _risk_adjusted_metrics,
)
from app.services.backtest.risk_caps import (
    _apply_risk_caps,
    _cap_position_weights,
    _cap_sector_weights,
    _portfolio_vol_ann,
)
from app.services.backtest.slippage import _vol_slippage_map

# 주의: app.services.metrics 는 app.services.backtest.signals 를 임포트하므로,
# 이 모듈에서 metrics 를 최상위 임포트하면 순환 임포트가 된다
# (backtest/__init__ → portfolio → metrics → backtest.signals). 따라서
# metrics 의 헬퍼는 사용 시점에 함수 내부에서 지연 임포트한다.

logger = logging.getLogger(__name__)

__all__ = [
    "run_rebalance_backtest",
    # 하위 모듈에서 재노출(호환 유지)
    "_apply_risk_caps",
    "_cap_position_weights",
    "_cap_sector_weights",
    "_portfolio_vol_ann",
    "_vol_slippage_map",
    "_factor_attribution",
    "_risk_adjusted_metrics",
    "_FACTOR_SCORE_COLS",
    "_safe",
]


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
        if cadence in ("monthly", "quarterly") and d.day < dom:
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


def _dynamic_universe(hist: pd.DataFrame, pool: list[str], rule: dict) -> list[str]:
    """리밸런싱일까지의 가격으로 후보풀에서 상대강도 상위 pick 종목을 고른다.

    미래참조 방지: hist(=panel.loc[:d]) 만 사용한다. 이력이 lookback 에 못 미치는
    종목은 자연히 제외된다(신규상장 등). 동점/무자료는 배제한다.
    """
    lookback = int(rule.get("lookback", 120))
    pick = int(rule.get("pick", 20))
    scores: dict[str, float] = {}
    for code in pool:
        if code not in hist.columns:
            continue
        c = hist[code].dropna()
        if len(c) < lookback + 1:
            continue
        p0 = float(c.iloc[-lookback - 1])
        p1 = float(c.iloc[-1])
        if p0 > 0:
            scores[code] = p1 / p0 - 1.0
    ranked = sorted(scores, key=lambda k: scores[k], reverse=True)
    return ranked[:pick]


def _scale_targets(weights: dict[str, float], exposure: float) -> dict[str, float]:
    """목표비중 딕셔너리를 총투자비중(exposure)으로 스케일한다(패닉 오버레이 P2).

    exposure<1.0 이면 나머지는 자연히 현금으로 남는다(_apply_rebalance 가 targets 에
    없는 비중을 현금으로 취급). exposure>=1.0(부동소수 오차 포함)이면 원본을 그대로 쓴다.
    """
    if not weights or exposure >= 1.0 - 1e-9:
        return weights
    exposure = max(0.0, exposure)
    return {s: w * exposure for s, w in weights.items()}


def _targets_at(
    d: pd.Timestamp,
    panel: pd.DataFrame,
    config: dict,
    fundamentals_provider,
    pool_provider=None,
    *,
    score_out: list | None = None,
    exposure: float = 1.0,
) -> dict[str, float]:
    """리밸런싱일 d 의 목표비중을 산정한다(d 종가까지의 데이터만 사용).

    :param pool_provider: 지정 시 (d)->list[code] 로 그 시점의 후보풀을 공급한다.
        시점별 지수 구성종목(예: KOSPI200 멤버십)을 넣으면 생존편향이 제거된다.
        None 이면 config.universe(고정)를 후보풀로 쓴다.
    :param score_out: method="score" 일 때, (d, 팩터점수 DataFrame)을 append 한다.
        성과귀속·팩터 IC/IR(P1-1) 산출용 스냅샷. None 이면 수집하지 않는다.
    :param exposure: 목표 총투자비중(0~1). 1.0(기본)이면 미적용. 패닉 오버레이(P2)의
        base_exposure/panic_exposure 스케일링에 사용한다(리스크 레이어 적용 이후 스케일).
    """
    from app.services.metrics import _compute_stock_scores  # 지연(순환 회피)

    hist = panel.loc[:d]
    selection = config.get("selection", {})
    # 후보풀: 시점별 provider 가 있으면 그 시점 멤버십, 없으면 config.universe(고정).
    if pool_provider is not None:
        universe = list(pool_provider(d) or [])
    else:
        universe = list(config.get("universe", []))
    # 동적 유니버스: 지정 시 후보풀을 d 시점 규칙으로 사전선정(상대강도 상위)한다.
    rule = selection.get("universe_rule")
    if rule:
        universe = _dynamic_universe(hist, universe, rule)
    method = selection.get("method", "momentum")
    # compute_target_weights 는 cfg["universe"] 로 후보를 필터하므로, 동적/시점별로
    # 해소된 실제 유니버스를 반영한 config 를 넘긴다(그러지 않으면 pool_provider·
    # universe_rule 로 고른 종목이 원래 config.universe 에 없다는 이유로 탈락한다).
    config = {**config, "universe": universe}

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
                            "op_growth", "net_growth", "turnaround", "market_cap", "sector"):
                    if col in fdf.columns:
                        fac[col] = fdf[col].reindex(fac.index)
        weights = config.get("selection", {}).get("factor_weights")
        neutralize = config.get("selection", {}).get("neutralize", "none")
        scored = _compute_stock_scores(fac, weights=weights, neutralize=neutralize)
        if score_out is not None:
            # 팩터 IC/IR·성과귀속(P1-1)용 스냅샷 — 팩터별 점수 컬럼만 보관(후보풀 전체 크로스섹션).
            cols = [c for c in _FACTOR_SCORE_COLS if c in scored.columns]
            score_out.append((d, scored[cols].copy()))
        scores = {
            code: float(v)
            for code, v in scored["score"].items()
            if not is_nan(v)
        }
        # weighting="inverse_vol" 용 변동성 — _score_factor_frame 이 이미 산출한 vol_ann
        # (app.services.metrics._compute_vol_ann 과 동일 정의)을 재사용해 실거래(vols=None →
        # price_history 로 재계산)와 동일 정의를 공유하면서 중복 계산을 피한다.
        vols = None
        if "vol_ann" in fac.columns:
            vols = {
                code: float(v)
                for code, v in fac["vol_ann"].items()
                if not is_nan(v)
            }
        # 변동성 적격 게이트(옵셔널)는 종가 시계열을 요구한다. 지정 시에만 as_of(d)
        # 까지의 종가 패널을 주입해 게이트가 실거래와 동일 판정을 하게 한다(미래참조
        # 없음: hist=panel.loc[:d]). 미지정이면 기존대로 빈 dict 를 넘겨 불필요한 구성
        # 비용을 피한다(선정 결과 불변).
        gate_history = {}
        if selection.get("vol_gate"):
            gate_history = {
                sym: hist[sym].dropna() for sym in universe if sym in hist.columns
            }
        weights = compute_target_weights(gate_history, config, scores=scores, vols=vols)
        weights = _apply_risk_caps(
            weights, hist, config.get("risk_layer") or {}, config.get("_sector_map")
        )
        return _scale_targets(weights, exposure)

    price_history = {
        sym: hist[sym].dropna() for sym in universe if sym in hist.columns
    }
    weights = compute_target_weights(price_history, config)
    weights = _apply_risk_caps(
        weights, hist, config.get("risk_layer") or {}, config.get("_sector_map")
    )
    return _scale_targets(weights, exposure)


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


# KRX 호가단위표(2023-01-25 개정, 코스피·코스닥 통일). (가격 미만 상한, 호가단위원) 오름차순.
_TICK_BANDS: list[tuple[float, float]] = [
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
    (float("inf"), 1_000),
]


def _krx_tick_size(price: float) -> float:
    """KRX 가격대별 호가단위(원)를 반환한다."""
    for upper, tick in _TICK_BANDS:
        if price < upper:
            return tick
    return _TICK_BANDS[-1][1]


def _round_to_tick(price: float | None) -> float | None:
    """가격을 그 가격대의 KRX 호가단위 배수로 반올림한다(price_limit_model opt-in 전용).

    None/NaN/0 이하는 그대로 통과시킨다(호출부가 결측 처리를 맡는다).
    """
    if price is None or not np.isfinite(price) or price <= 0:
        return price
    tick = _krx_tick_size(price)
    return round(price / tick) * tick


def _price_limit_band(prev_close: float) -> tuple[float, float]:
    """전일종가 기준 ±30% 상하한가를 호가단위로 라운딩해 (하한, 상한)을 반환한다."""
    lower = _round_to_tick(prev_close * 0.7) or 0.0
    upper = _round_to_tick(prev_close * 1.3) or 0.0
    return lower, upper


def _floor_to_shares(amt: float, sym: str, prices: pd.Series | None, capital: float) -> float:
    """정규화 거래대금을 KRX 정수주 단위로 절사한 정규화 거래대금으로 변환한다(A-1).

    실거래 engine.rebalance.compute_rebalance_orders 와 동일하게 floor(금액/가격) 주로
    맞춘다. 가격 정보가 없으면(호출부에서 이미 걸러지지만 방어적으로) 원본을 그대로 반환한다.
    """
    price = prices.get(sym) if prices is not None else None
    if price is None or pd.isna(price) or price <= 0 or capital <= 0:
        return amt
    qty = math.floor(amt * capital / float(price))
    if qty <= 0:
        return 0.0
    return qty * float(price) / capital


def _apply_adv_cap(
    amt: float, sym: str, adv_map: pd.Series | None, adv_cap_frac: float, capital: float
) -> float:
    """ADV(20일 평균 거래대금) 대비 참여율 상한으로 거래대금을 절사한다(A-2).

    adv_map 은 그 리밸런싱일까지의 데이터로 산출된 ADV(KRW) 이므로 미래참조가 없다.
    해당 종목의 ADV 를 확보하지 못하면(신규상장·결측) 캡을 적용하지 않는다.
    """
    if adv_map is None or capital <= 0:
        return amt
    adv_val = adv_map.get(sym) if hasattr(adv_map, "get") else None
    if adv_val is None or not np.isfinite(adv_val) or adv_val <= 0:
        return amt
    cap_norm = (adv_cap_frac * float(adv_val)) / capital
    return min(amt, cap_norm)


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
    slip_base: float = 0.0,
    slip_map: dict[str, float] | None = None,
    integer_shares: bool = True,
    adv_cap_frac: float = 0.0,
    adv_map: pd.Series | None = None,
    price_limit_model: bool = False,
    prev_prices: pd.Series | None = None,
) -> tuple[float, float, float]:
    """목표비중으로 리밸런싱하며 비용을 차감하고 거래 로그를 남긴다(매도 먼저 → 매수).

    :param val: 종목→평가액(정규화 단위). 함수가 제자리 수정한다.
    :param cost: 종목→원가 기준 누적 투자액(정규화 단위). 포지션 수익률 산출용. 제자리 수정.
    :param equity: 리밸런싱 직전 총자산(cash+보유평가액).
    :param liquidate: True 면 드리프트 밴드를 무시하고 보유 전량을 청산한다(레짐 현금화).
    :param reason: 거래 로그의 사유 태그.
    :param slip_base: 편도 슬리피지(분수). 매수는 +slip, 매도는 −slip 로 체결가에 가산.
    :param slip_map: 종목별 슬리피지(분수) 오버라이드. 없으면 slip_base 를 쓴다(변동성 스케일용).
    :param integer_shares: True(기본)면 체결 거래대금을 정수주 단위로 절사(A-1).
    :param adv_cap_frac: ADV 대비 1일 참여율 상한(0=미적용, A-2).
    :param adv_map: 종목→ADV(KRW) Series. adv_cap_frac>0 일 때만 사용.
    :param price_limit_model: True 면 체결가를 KRX 호가단위로 라운딩하고, 전일종가 대비
        ±30% 상하한가에 도달한 방향의 주문을 체결 불가(다음 리밸런싱 이월)로 막는다(opt-in,
        improvements.md §4). 기본 False — 기존 결과 재현성 보존.
    :param prev_prices: 전일(직전 거래일) 종가 Series. price_limit_model=True 일 때만 상하한가
        밴드 판정에 쓰인다. None 이면 상하한가 판정을 건너뛴다(호가단위 라운딩은 그대로 적용).
    :return: (리밸런싱 후 cash, 회전율 turnover=Σ|Δw|(의도된 드리프트 기준),
        executed_notional=ADV 캡·정수주 절사·상하한가 체결불가 반영 이후 실체결 거래대금 합
        (정규화 단위, §7)).
    """
    if equity <= 0:
        return 0.0, 0.0, 0.0

    def _slip(sym: str) -> float:
        if slip_map and sym in slip_map:
            return slip_map[sym]
        return slip_base

    # 체결가(fill_prices): price_limit_model 이 켜져 있으면 호가단위로 라운딩한 가격을
    # 정수주 환산·거래 로그에 쓴다. 마크투마켓(메인 루프)은 실제 종가를 그대로 쓰므로
    # 이 라운딩은 체결 정밀도에만 영향을 준다(평가액 로직은 불변).
    fill_prices = prices
    if price_limit_model and prices is not None:
        fill_prices = prices.map(lambda p: _round_to_tick(float(p)) if pd.notna(p) else p)

    def _limit_blocked(sym: str, side: str) -> bool:
        """전일종가 대비 상하한가 도달로 이 방향 주문이 체결 불가인지 판정한다."""
        if not price_limit_model or prev_prices is None or prices is None:
            return False
        cur_p = prices.get(sym)
        prev_p = prev_prices.get(sym)
        if cur_p is None or pd.isna(cur_p) or prev_p is None or pd.isna(prev_p) or float(prev_p) <= 0:
            return False
        lower, upper = _price_limit_band(float(prev_p))
        if side == "buy":
            return float(cur_p) >= upper - 1e-6
        return float(cur_p) <= lower + 1e-6

    executed_notional = 0.0
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
            cur_val = val.get(sym, 0.0)
            cur_w = cur_val / equity
            tgt_w = targets.get(sym, 0.0)
            dev = tgt_w - cur_w
            # 드리프트 밴드는 보유 종목의 비중 미세조정에만 적용. 신규 편입·전량 청산은
            # 목표 비중이 밴드 이하여도 반드시 체결(라이브 로직 compute_rebalance_orders 와 일치).
            is_new_entry = cur_val <= 1e-12 and tgt_w > 0.0
            is_full_exit = tgt_w <= 0.0 and cur_val > 1e-12
            if not (is_new_entry or is_full_exit) and abs(dev) <= drift_band:
                continue
            turnover += abs(dev)
            if dev < 0:
                sells.append((sym, -dev * equity))  # 매도 금액
            else:
                buys.append((sym, dev * equity))    # 매수 금액

    # ADV 참여율 캡(A-2) — 유동성 상한을 초과하는 목표 거래대금을 사전 절사.
    # turnover 는 위에서 이미 '의도된' 드리프트 기준으로 집계했으므로 그대로 둔다
    # (실제 체결액은 그 이하 — 사후 분석 시 참고).
    if adv_cap_frac > 0 and adv_map is not None:
        sells = [(sym, _apply_adv_cap(amt, sym, adv_map, adv_cap_frac, capital)) for sym, amt in sells]
        buys = [(sym, _apply_adv_cap(amt, sym, adv_map, adv_cap_frac, capital)) for sym, amt in buys]

    # 시작 현금 = equity - 보유평가액 합
    cash = equity - sum(val.values())

    # 매도 먼저 실행(현금 확보) — 포지션 원가 대비 수익률을 로그로 남긴다
    for sym, amt in sells:
        if _limit_blocked(sym, "sell"):
            continue  # 하한가 도달 — 매도 체결 불가(다음 리밸런싱으로 이월, §4)
        cur_val = val.get(sym, 0.0)
        # 전량 청산(ADV 캡으로 amt 가 이미 줄었다면 '전량'이 아니므로 절사 대상이 된다) 만
        # 정수주 절사를 건너뛴다 — 보유 전량을 정리하는 거래에 소수점 잔량을 남기지 않기 위함.
        is_full_exit = amt >= cur_val - 1e-9
        if integer_shares and not is_full_exit:
            amt = _floor_to_shares(amt, sym, fill_prices, capital)
        amt = min(amt, cur_val)
        if amt <= 1e-12:
            continue
        proceeds = amt * (1.0 - fees - tax - _slip(sym))
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
            _trade_rec(d, sym, "sell", amt, fill_prices, capital, port_return, pos_ret, reason)
        )
        executed_notional += amt

    # 매수: 정수주 절사(A-1) 먼저 적용한 뒤, 현금 부족 시 비례 축소(음수 현금 방지).
    # 절사 후 축소이므로 축소가 다시 소수점 금액을 만들 수 있으나, floor 로 만든 금액은
    # 이미 실제 정수주 가치이고 scale(<=1) 은 그 정수주 중 '몇 주를 못 산다'는 의미이므로
    # 축소된 buy_cost 가 cash 를 넘지 않게만 하고 별도 재절사는 하지 않는다(과최적화 방지 —
    # 근소한 초과 매수 방지가 목적이지 소수점 주수 재현이 목적이 아님).
    buys = [(sym, amt) for sym, amt in buys if not _limit_blocked(sym, "buy")]
    if integer_shares:
        buys = [(sym, _floor_to_shares(amt, sym, fill_prices, capital)) for sym, amt in buys]
    total_buy_cost = sum(amt * (1.0 + fees + _slip(sym)) for sym, amt in buys)
    scale = 1.0
    if total_buy_cost > cash and total_buy_cost > 0:
        scale = cash / total_buy_cost
    for sym, amt in buys:
        amt *= scale
        if amt <= 1e-12:
            continue
        buy_cost = amt * (1.0 + fees + _slip(sym))
        cash -= buy_cost
        val[sym] = val.get(sym, 0.0) + amt
        cost[sym] = cost.get(sym, 0.0) + amt  # 원가는 수수료 제외 매수액으로 적립
        trades.append(
            _trade_rec(d, sym, "buy", amt, fill_prices, capital, port_return, None, reason)
        )
        executed_notional += amt

    return cash, turnover, executed_notional


def run_rebalance_backtest(
    close_panel: pd.DataFrame,
    config: dict,
    sim_start,
    sim_end,
    fundamentals_provider=None,
    regime_series: pd.Series | None = None,
    pool_provider=None,
    benchmark_series: pd.Series | None = None,
    panic_series: pd.DataFrame | None = None,
    volume_panel: pd.DataFrame | None = None,
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
    :param pool_provider: 지정 시 (리밸런싱일 Timestamp)->list[code] 로 그 시점의 후보풀을
        공급한다. 시점별 지수 구성종목(KOSPI200 멤버십 등)을 넣으면 생존편향이 제거된다.
        None 이면 config.universe(고정 후보풀)를 쓴다. universe_rule 과 함께 쓰면
        (그 시점 멤버십 → 상대강도 상위 pick → 최종 top_n) 순으로 적용된다.
    :param benchmark_series: 벤치마크(예: KOSPI200) 종가 Series(워밍업 포함). 주면 벤치마크
        상대 지표(alpha·beta·IR·tracking_error·benchmark_return·excess_return)를 산출한다.
        None 이면 해당 지표는 None 으로 반환된다. Sharpe·Sortino 는 config.risk_free_rate(연)
        초과 기준으로 산출한다(기본 rf=0).
    :param panic_series: 패닉 오버레이(config.panic_overlay)용 롤링 패닉 지표(워밍업 포함).
        `app.services.metrics.panic.compute_panic_series` 로 만든 DataFrame(index=날짜,
        columns=[score, level, gated, hard_trigger, close, low, dd60])을 기대한다. None 이거나
        config.panic_overlay 가 비활성이면 오버레이 미적용(레짐 필터만 동작).
    :param volume_panel: close_panel 과 동일 shape(index=일자, columns=종목코드, 값=거래량(주)).
        config.adv_participation_cap(A-2)이 설정돼 있을 때만 사용해 20일 ADV(거래대금)를
        산출한다. None 이면 캡이 설정돼 있어도 미적용(경고 로그만).
    :return: {total_return, mdd, sharpe, sortino, cagr, alpha, beta, information_ratio,
        tracking_error, benchmark_return, excess_return, win_rate, num_trades,
        num_rebalances, num_kills, num_panic_events, factor_ic, avg_turnover,
        avg_turnover_actual, equity_curve, markers, holdings, trades} — JSON. avg_turnover_actual
        은 ADV 캡·정수주 절사·상하한가 체결불가(§4) 반영 이후 실체결 기준 평균 회전율(§7,
        avg_turnover 이하). num_kills 는 MDD 킬스위치(risk_layer) 발동 횟수.
        num_panic_events 는 패닉 오버레이 Confirm(재진입) 발동 횟수(P2). 리스크 레이어
        (집중 한도·변동성 타겟팅)는 목표비중에 반영되며 markers 에 'mdd_exit' 로 킬스위치
        청산이, 'panic_confirm'/'panic_scale_full'/'panic_exit_*' 로 오버레이 이벤트가
        남는다. factor_ic 는 method="score" 전략의 팩터별 IC·IR·롱숏수익(성과귀속, P1-1)
        이며 그 외 방식에선 {} 이다.
    """
    # 상장폐지(장기 결측) 처리: 무한 ffill 은 폐지 종목의 마지막 종가를 이후 전 구간
    # 동결시켜 폐지 손실이 평가액·매도에 전혀 반영되지 않아 성과를 상방 편향시킨다.
    # 짧은 결측(휴장·거래정지)만 메우도록 ffill 을 delisting_gap_days 로 제한한다.
    # 그 이상 결측이 나면 마크투마켓 루프에서 회수율(delist_recovery)만 현금화하고 포지션을
    # 소멸시켜 폐지 손실을 확정하되, **반드시 terminal gap(그 종목의 마지막 유효 관측 이후로
    # 두 번 다시 값이 없는 경우)일 때만** 소멸시킨다. 상장적격성 실질심사·개선기간처럼 수주~
    # 1년 거래정지 후 '재개'되는 interior gap 을 폐지로 오분류해 영구 write-off 하는 것을
    # 막기 위함이다(financial-expert 권고 1순위). last_valid 는 ffill 이전 원본 기준으로 잡는다.
    _norm = _normalize_index(close_panel)
    last_valid: dict[str, pd.Timestamp] = {
        str(c): lv for c, lv in _norm.apply(lambda s: s.last_valid_index()).items()
        if lv is not None
    }
    _delist_gap = int(config.get("delisting_gap_days", 10) or 0)
    # 회수율 기본값 0.9: pykrx 가격 패널의 종점(terminal) 가격은 이미 최종 회수가치를 담는다.
    # 실측(2026-07-11) — 자진/공개매수 상폐(쌍용C&E 003410)는 종점가=공개매수가(7,000원),
    # 부실/회생 상폐(한진해운 117930)는 종점가=정리매매 폭락 확정가(780→12원, -98%).
    # 즉 write-off 직전 val[sym] 은 이미 공정한 최종가로 mark-to-market 되어 있으므로, 여기에
    # 낮은 회수율을 곱하면 이미 올바른 값을 추가로 깎는 계통적 이중 페널티가 된다. 0.9 는
    # 마지막 관측일~write-off일 사이 1일 갭과 정리매매 유동성/스프레드를 반영한 소폭 haircut.
    # (정리매매 종가가 없는 데이터 소스를 쓰는 유니버스면 config 로 0.35~0.5 로 낮출 것.)
    delist_recovery = max(0.0, min(1.0, float(config.get("delisting_recovery", 0.9))))
    panel = _norm.ffill(limit=_delist_gap if _delist_gap > 0 else None)
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

    # 성과귀속·팩터 IC/IR(P1-1): method="score" 전략만 팩터별 점수 스냅샷을 수집한다.
    attribution = str(config.get("selection", {}).get("method")) == "score"
    score_snapshots: list | None = [] if attribution else None

    # 체결 현실성(P0-1): 체결 시점(fill_mode)·슬리피지.
    defer = str(config.get("fill_mode", "next_close")) == "next_close"
    slip_base = max(0.0, float(config.get("slippage_bps", 5.0) or 0.0) / 10_000.0)
    slip_vol_scale = max(0.0, float(config.get("slippage_vol_scale", 0.0) or 0.0))
    rf_annual = float(config.get("risk_free_rate", 0.0) or 0.0)  # 위험조정지표용 무위험수익률(연)

    # 체결 정밀도(P2-2): A-1 정수주, A-2 ADV 참여율 캡. 체결 모델 정밀화(§4, opt-in):
    # 호가단위 라운딩 + 상하한가 체결 불가. 기본 False — 기존 결과 재현성 보존.
    integer_shares = bool(config.get("integer_shares", True))
    adv_cap_frac = float(config.get("adv_participation_cap") or 0.0)
    price_limit_model = bool(config.get("price_limit_model", False))
    adv_frame: pd.DataFrame | None = None
    if adv_cap_frac > 0:
        if volume_panel is not None and not volume_panel.empty:
            vol_norm = _normalize_index(volume_panel)
            vol_norm = vol_norm.reindex(index=panel.index, columns=panel.columns).fillna(0.0)
            # 거래대금 = 거래량×종가, 20일 이동평균(미래참조 없음 — d 시점은 그날까지 값만 씀).
            adv_frame = (vol_norm * panel).rolling(20, min_periods=10).mean()
        else:
            logger.warning(
                "adv_participation_cap 설정됨이나 거래량 패널(volume_panel) 미확보 —"
                " ADV 캡을 적용하지 않는다."
            )

    # 리스크 레이어(P1-2) — MDD 킬스위치 파라미터(집중 한도·변동성 타겟팅은 _targets_at
    # 에서 목표비중에 반영된다). mdd_kill_pct 가 None 이면 킬스위치 비활성.
    risk_layer = config.get("risk_layer") or {}
    _mkp = risk_layer.get("mdd_kill_pct")
    mdd_kill_pct = float(_mkp) if _mkp else None
    mdd_rearm_days = int(risk_layer.get("mdd_rearm_days", 20) or 20)

    # 섹터 집중 한도(max_sector_pct)용 종목→업종 매핑을 백테스트 전체에서 1회만 조회해
    # config 에 실어 _targets_at → _apply_risk_caps 로 흘려보낸다(업종 분류는 사실상 정적).
    # 소스 미확보(빈 dict)면 _apply_risk_caps 가 섹터 캡을 조용히 미적용한다.
    #
    # PIT 한계(C-2, 부분 해소): 업종분류 스냅샷을 분기 1회 적재하기 시작했다(worker
    # "snapshot-sector-map-quarterly" → sector_map_snapshots 테이블, 자세한 내용은
    # app.services.data.krx_index.sector_map 문서 참고). as_of=오늘로 조회하면 스냅샷
    # 도입 이후 시점부터는 그 시점에 가장 가까운 과거 스냅샷(=PIT)을 쓴다. 다만 이 백테스트
    # 전체에 대해 매핑을 1회만 로드하는 구조라, 수년치 백테스트 구간 전체를 '오늘' 시점
    # 하나의 분류로 대표한다는 한계는 남는다(스냅샷이 아직 1건뿐이라면 이 시점부터는 오차가
    # 없지만, 여러 분기 스냅샷이 쌓인 뒤에도 백테스트 구간별로 그 시점 스냅샷을 골라 쓰려면
    # _apply_risk_caps 호출부를 리밸런싱일 단위로 리팩터링해야 한다 — 별도 과제). 스냅샷
    # 도입 **이전** 과거 구간(첫 배치 이전 시점)은 그 시점 분류를 보존한 데이터 소스가 아예
    # 없어 소급 적용이 여전히 불가능하며, 이 경우 현재 KRX 분류로 자동 폴백한다(약한
    # look-ahead 잔존). 섹터 '한도'(비중 상한) 용도라 왜곡은 제한적이다.

    if risk_layer.get("max_sector_pct") and not config.get("_sector_map"):
        from datetime import date as _date

        from app.services.data.krx_index import sector_map as _sector_map_fn

        smap = _sector_map_fn(as_of=_date.today())
        if smap:
            config = {**config, "_sector_map": smap}
            logger.info("섹터 집중 한도용 업종 매핑 로드: %d종목", len(smap))
        else:
            logger.warning("섹터 한도 설정됨(max_sector_pct)이나 업종 매핑 미확보 — 섹터 캡 미적용.")

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

    # 패닉 오버레이(P2, 대표안 A "패닉 재진입 가속기" / event_only=True 면 대조군 B).
    # Arm(자본항복 후보 감지) → Confirm(재진입 확인) → Fill(익일 종가 체결, 기존 defer
    # 규약 재사용) 상태기계. panic_series 를 패널 거래일에 정렬(ffill)해 지수 종가/저가·
    # 라벨을 매일 참조한다. 미래참조 없음: panic_series 자체가 각 날짜까지의 확정
    # 데이터로 산출되었고(compute_panic_series), 여기서는 그 시점 값만 읽는다.
    po = config.get("panic_overlay") or {}
    panic_ps: pd.DataFrame | None = None
    if po.get("enabled"):
        if panic_series is not None and not panic_series.empty:
            ps = panic_series.copy()
            pidx = ps.index
            if isinstance(pidx, pd.DatetimeIndex) and pidx.tz is not None:
                pidx = pidx.tz_localize(None)
            ps.index = pd.DatetimeIndex(pidx).normalize()
            ps = ps[~ps.index.duplicated(keep="last")].sort_index()
            ps = ps.reindex(panel.index).ffill()
            if not ps["close"].isna().all():
                panic_ps = ps
        if panic_ps is None:
            logger.warning(
                "패닉 오버레이가 켜져 있으나 유효한 패닉 시계열을 확보하지 못해 오버레이를 적용하지 않는다."
            )

    panic_on = panic_ps is not None
    _LEVEL_RANK = {"normal": 0, "caution": 1, "warning": 2, "panic": 3}
    arm_level = str(po.get("arm_level") or "warning")
    arm_rank = _LEVEL_RANK.get(arm_level, 2)
    arm_window = int(po.get("arm_window") or 5)
    hold_days_ov = int(po.get("hold_days") or 20)

    def _pof(key: str, default: float) -> float:
        # 0.0 이 유효값이므로 `or` 대신 None 여부로만 기본값 대체(단일 .get()).
        v = po.get(key)
        return float(v) if v is not None else default

    profit_reclaim_pct = _pof("profit_reclaim_pct", 0.5)
    knife_stop_pct = _pof("knife_stop_pct", 0.05)
    base_exposure_ov = _pof("base_exposure", 0.70)
    panic_exposure_ov = _pof("panic_exposure", 1.00)
    scale_in_confirm = _pof("scale_in_confirm", 0.5)
    ma_recovery_period = int(po.get("ma_recovery_period") or 20)
    event_only = bool(po.get("event_only", False))

    panic_ma: pd.Series | None = None
    panic_prev_close: pd.Series | None = None
    if panic_on:
        min_p = max(5, ma_recovery_period // 2)
        panic_ma = panic_ps["close"].rolling(ma_recovery_period, min_periods=min_p).mean()
        panic_prev_close = panic_ps["close"].shift(1)  # 전일 종가(루프마다 재계산 방지)

    # 상태기계: idle → armed → confirmed_half → confirmed_full (A) / confirmed (B, event_only)
    panic_state = "idle"
    arm_deadline_idx = -1        # armed 상태에서 Confirm 을 기다리는 마지막 sim_dates 인덱스
    capit_close: float | None = None   # 자본항복(Arm)일 지수 종가(Confirm 판정 기준)
    capit_low: float | None = None     # 자본항복일 지수 저가(손절 기준)
    capit_drop: float | None = None    # 자본항복일 낙폭(전일종가-당일종가, 양수=하락)
    confirm_idx = -1              # Confirm 발생 sim_dates 인덱스(hold_days 카운트 기준)
    num_panic_events = 0          # Confirm(재진입) 발동 횟수

    cash = 1.0            # 정규화 총자산(=capital 배율)
    val: dict[str, float] = {}    # 종목→평가액
    cost: dict[str, float] = {}   # 종목→원가 누적 투자액(포지션 수익률용)
    prev_prices: pd.Series | None = None
    prev_risk_off = False  # 직전일 레짐 상태(risk-off→risk-on 전환 즉시 재진입 판정용)
    hwm = 1.0              # 고점(High-Water Mark) — MDD 킬스위치 기준선
    killed = False         # 킬스위치 발동(현금 대피) 상태
    kill_idx = -1          # 발동 시점(sim_dates 인덱스, 쿨다운 계산용)
    num_kills = 0          # 킬스위치 발동 횟수

    equities_norm: list[float] = []   # 일별 종가 시점(리밸런싱 반영 후) 총자산
    equity_curve: list[dict] = []
    markers: list[dict] = []
    turnovers: list[float] = []
    turnovers_actual: list[float] = []  # 실체결(ADV 캡·정수주·상하한가 반영 이후) 거래대금(§7)
    trades: list[dict] = []

    # 결정(선정·청산)과 체결을 분리한다. next_close 면 어떤 날 d 에 내린 결정을 다음
    # 거래일 종가에 체결(pending 큐)하고, same_close 면 그날 종가에 즉시 체결한다.
    # 결정 근거는 언제나 결정일 d 까지의 데이터만 쓰므로 미래참조가 없다.
    pending: dict | None = None

    def _run_decision(
        decision: dict,
        prices: pd.Series,
        d: pd.Timestamp,
        cash: float,
        limit_ref_prices: pd.Series | None = None,
    ) -> float:
        """decision(청산/리밸런싱)을 d 종가로 체결한다(val/cost/trades 등 제자리 수정).

        :param limit_ref_prices: 상하한가 밴드 판정 기준이 되는 '체결일 직전 거래일' 종가
            (price_limit_model=True 일 때만 사용). fill_mode=next_close 면 결정일의 종가와
            같다(체결은 다음날이지만 밴드는 그 전날 종가 기준).
        """
        equity = cash + sum(val.values())
        adv_today = adv_frame.loc[d] if adv_frame is not None else None
        if decision["kind"] == "liquidate":
            if not val:
                return cash
            slip_map = _vol_slippage_map(panel.loc[:d], list(val), slip_base, slip_vol_scale)
            cash, turnover, exec_notional = _apply_rebalance(
                val, cost, {}, equity, prices, d, capital,
                drift_band, fees, tax, trades,
                liquidate=True, reason=decision["reason"],
                slip_base=slip_base, slip_map=slip_map,
                integer_shares=integer_shares, adv_cap_frac=adv_cap_frac, adv_map=adv_today,
                price_limit_model=price_limit_model, prev_prices=limit_ref_prices,
            )
            if turnover > 0:
                turnovers.append(turnover)
                mk = "mdd_exit" if decision["reason"] == "mdd_kill" else "regime_exit"
                markers.append({"t": d.isoformat(), "type": mk})
            if exec_notional > 0:
                turnovers_actual.append(exec_notional)
        else:  # rebalance
            # 체결일 가격이 없는 종목은 제외(매매 불가). next_close 면 체결일 = d.
            targets = {
                s: w for s, w in decision["targets"].items()
                if pd.notna(prices.get(s)) and float(prices.get(s) or 0) > 0
            }
            if targets:
                slip_map = _vol_slippage_map(
                    panel.loc[:d], list(set(targets) | set(val)), slip_base, slip_vol_scale
                )
                cash, turnover, exec_notional = _apply_rebalance(
                    val, cost, targets, equity, prices, d, capital,
                    drift_band, fees, tax, trades, reason=decision["reason"],
                    slip_base=slip_base, slip_map=slip_map,
                    integer_shares=integer_shares, adv_cap_frac=adv_cap_frac, adv_map=adv_today,
                    price_limit_model=price_limit_model, prev_prices=limit_ref_prices,
                )
                if turnover > 0:
                    turnovers.append(turnover)
                    markers.append({
                        "t": d.isoformat(),
                        "type": "rebalance",
                        "holdings": len(val),
                    })
                if exec_notional > 0:
                    turnovers_actual.append(exec_notional)
        return cash

    for i, d in enumerate(sim_dates):
        prices = panel.loc[d]
        # 상하한가 밴드 기준(직전 거래일 종가) — 아래에서 prev_prices 를 오늘자로 갱신하기
        # 전에 캡처해둔다. next_close 체결도 '체결일의 전날' 종가를 정확히 가리킨다.
        limit_ref_prices = prev_prices
        # 1) 당일 수익률 반영(보유 종목 평가액 갱신) + 상장폐지 손실 확정
        if prev_prices is not None and val:
            for sym in list(val):
                p0 = prev_prices.get(sym)
                p1 = prices.get(sym)
                if p1 is not None and pd.notna(p1) and p0 and p0 > 0 and pd.notna(p0):
                    val[sym] *= float(p1) / float(p0)
                elif (p1 is None or pd.isna(p1)):
                    lv = last_valid.get(sym)
                    if lv is not None and d > lv:
                        # terminal gap(마지막 유효 관측 이후 두 번 다시 값 없음) → 상장폐지 확정.
                        # 잔존 회수율만 현금화하고 포지션 소멸(폐지 손실 확정, 상방 편향 제거).
                        proceeds = val.pop(sym) * delist_recovery
                        cost.pop(sym, None)
                        cash += proceeds
                        markers.append({"t": d.isoformat(), "type": "delist", "symbol": sym})
                    # else: interior gap(거래정지 후 재개형) → write-off 하지 않고 평가액 동결(유지).
        prev_prices = prices

        # 2) 전일 결정의 지연 체결(next_close). same_close 면 pending 이 항상 비어 있다.
        if pending is not None:
            cash = _run_decision(pending, prices, d, cash, limit_ref_prices)
            pending = None

        # 3) MDD 킬스위치 상태 갱신(P1-2) — 마크투마켓·지연체결 반영 후 자산가치로 판정.
        #    고점(HWM) 추적 + 쿨다운 경과 시 재가동(기준선 리셋).
        cur_equity = cash + sum(val.values())
        just_rearmed = False
        if mdd_kill_pct is not None:
            if cur_equity > hwm:
                hwm = cur_equity
            if killed and (i - kill_idx) >= mdd_rearm_days:
                killed = False           # 쿨다운 경과 → 재가동, 고점 기준선 리셋
                hwm = cur_equity
                just_rearmed = True

        # 4) 당일 결정. 우선순위: 킬스위치 발동 → panic_override(패닉 오버레이) →
        #    (쿨다운 중 대기) → 레짐 청산 → 정기 리밸런싱/재진입. 미래참조 방지: 선정·
        #    목표비중·패닉 판정은 모두 d 까지의 데이터만 사용한다.
        risk_off = bool(regime_on is not None and not bool(regime_on.get(d, True)))
        decision: dict | None = None

        # 킬스위치는 절대 최우선(파국 백스톱) — 오늘 새로 발동하는지 미리 판정해 패닉
        # 오버레이보다 위에 둔다(오버레이 로직이 이 판정을 침범하지 않도록 가드).
        mdd_trigger_today = bool(
            mdd_kill_pct is not None and not killed
            and hwm > 0 and cur_equity / hwm - 1.0 <= -mdd_kill_pct
        )

        # ── 패닉 오버레이(P2) 상태 갱신(Arm→Confirm→Fill) ──
        # confirmed 상태(오버레이 활성)인 동안은 레짐 청산을 억제한다("레짐이 겁먹고
        # 나간 시점에 자본항복이 확인되면 강제 재진입/유지"). armed/idle 은 아직 오버레이가
        # 포지션을 만들지 않았으므로 레짐·정기 리밸런싱 로직을 그대로 따른다(아래 5번).
        if panic_on and not killed and not mdd_trigger_today:
            level_i = str(panic_ps["level"].get(d, "normal"))
            hard_i = bool(panic_ps["hard_trigger"].get(d, False))
            close_i = panic_ps["close"].get(d)
            low_i = panic_ps["low"].get(d)
            prev_close_i = panic_prev_close.get(d)
            ma_i = panic_ma.get(d) if panic_ma is not None else None
            has_close = close_i is not None and pd.notna(close_i)
            has_prev = prev_close_i is not None and pd.notna(prev_close_i)

            if panic_state == "idle":
                if has_close and (_LEVEL_RANK.get(level_i, 0) >= arm_rank or hard_i):
                    panic_state = "armed"
                    arm_deadline_idx = i + arm_window
                    capit_close = float(close_i)
                    capit_low = float(low_i) if low_i is not None and pd.notna(low_i) else capit_close
                    capit_drop = float(prev_close_i) - capit_close if has_prev else None
                    markers.append({"t": d.isoformat(), "type": "panic_arm", "level": level_i})

            elif panic_state == "armed":
                if i > arm_deadline_idx:
                    # Arm 이후 arm_window 내 미확인 → 포기(코어 방어 유지, "떨어지는 칼날" 회피).
                    panic_state = "idle"
                    capit_close = capit_low = capit_drop = None
                    markers.append({"t": d.isoformat(), "type": "panic_arm_timeout"})
                elif (
                    has_close and has_prev and capit_close is not None
                    and float(close_i) > capit_close and float(close_i) > float(prev_close_i)
                ):
                    # Confirm: 지수가 자본항복일 종가를 상회 + 당일 등락률 양(+).
                    # Fill 은 기존 defer(next_close) 규약을 그대로 재사용(아래 pending 이월).
                    exposure_fill = panic_exposure_ov if event_only else (
                        base_exposure_ov + scale_in_confirm * (panic_exposure_ov - base_exposure_ov)
                    )
                    targets = _targets_at(
                        d, panel, config, fundamentals_provider, pool_provider,
                        score_out=score_snapshots, exposure=exposure_fill,
                    )
                    decision = {"kind": "rebalance", "targets": targets, "reason": "panic_confirm"}
                    panic_state = "confirmed" if event_only else "confirmed_half"
                    confirm_idx = i
                    num_panic_events += 1
                    markers.append({"t": d.isoformat(), "type": "panic_confirm"})

            elif panic_state in ("confirmed_half", "confirmed_full", "confirmed"):
                exit_reason: str | None = None
                if (
                    has_close and capit_low is not None
                    and float(close_i) <= capit_low * (1.0 - knife_stop_pct)
                ):
                    exit_reason = "panic_exit_knife"  # 손절(필수) — 칼날 방어
                elif (
                    has_close and capit_close is not None
                    and capit_drop is not None and capit_drop > 0
                    and float(close_i) >= capit_close + profit_reclaim_pct * capit_drop
                ):
                    exit_reason = "panic_exit_profit"  # 익절 — 낙폭의 profit_reclaim_pct 되돌림
                elif confirm_idx >= 0 and (i - confirm_idx) >= hold_days_ov:
                    exit_reason = "panic_exit_hold"    # 시간청산

                if exit_reason is not None:
                    if event_only:
                        # B(순수 이벤트): 전량 현금화.
                        decision = {"kind": "liquidate", "reason": exit_reason}
                    else:
                        # A: 여전히 risk-off 면 regime 방어(현금화) 복귀, 아니면 base_exposure 로 복귀.
                        if risk_off:
                            decision = {"kind": "liquidate", "reason": exit_reason}
                        else:
                            targets = _targets_at(
                                d, panel, config, fundamentals_provider, pool_provider,
                                score_out=score_snapshots, exposure=base_exposure_ov,
                            )
                            decision = {"kind": "rebalance", "targets": targets, "reason": exit_reason}
                    panic_state = "idle"
                    capit_close = capit_low = capit_drop = None
                    confirm_idx = -1
                elif (
                    not event_only and panic_state == "confirmed_half"
                    and has_close and ma_i is not None and pd.notna(ma_i)
                    and float(close_i) > float(ma_i)
                ):
                    # A 잔여 스케일인: 지수 ma_recovery_period 이평 회복 시 리저브 나머지 배치.
                    targets = _targets_at(
                        d, panel, config, fundamentals_provider, pool_provider,
                        score_out=score_snapshots, exposure=panic_exposure_ov,
                    )
                    decision = {"kind": "rebalance", "targets": targets, "reason": "panic_scale_full"}
                    panic_state = "confirmed_full"
                    markers.append({"t": d.isoformat(), "type": "panic_scale_full"})

        if mdd_trigger_today:
            # 고점 대비 낙폭이 임계 초과 → 전량 청산·현금 대피(파국 백스톱). 패닉 오버레이가
            # 이미 만든 결정(위에서 mdd_trigger_today 가드로 원천 차단됨)보다 항상 우선한다.
            killed = True
            kill_idx = i
            num_kills += 1
            if panic_on and panic_state != "idle":
                # 킬스위치가 오버레이보다 우선 — 진행 중이던 Arm/Confirm 상태를 강제 리셋.
                panic_state = "idle"
                capit_close = capit_low = capit_drop = None
                confirm_idx = -1
            if val:
                decision = {"kind": "liquidate", "reason": "mdd_kill"}
        elif decision is not None:
            pass  # 패닉 오버레이가 이번 결정을 확정했다(킬스위치 다음 최우선, panic_override).
        elif killed:
            pass  # 쿨다운 중 — 신규 매수·재진입 없음(현금 대피 유지)
        elif panic_state in ("confirmed_half", "confirmed_full", "confirmed"):
            pass  # 오버레이 유지 중 — 레짐 청산·정기 리밸런싱 억제(강제 보유, panic_override)
        elif risk_off and val:
            decision = {"kind": "liquidate", "reason": "regime_exit"}
        elif not risk_off:
            # 정기 리밸런싱일이거나, 레짐 회복 재진입이거나, 킬스위치 재가동 직후(현금)면 진입.
            # event_only(B)는 상시 core 가 없으므로 정기 리밸런싱 자체를 스킵한다.
            regime_reentry = bool(regime_on is not None and prev_risk_off and not val)
            due = d in rebal_dates or regime_reentry or (just_rearmed and not val)
            if due and not (panic_on and event_only):
                targets = _targets_at(
                    d, panel, config, fundamentals_provider, pool_provider,
                    score_out=score_snapshots,
                    exposure=(base_exposure_ov if panic_on else 1.0),
                )
                decision = {"kind": "rebalance", "targets": targets, "reason": "rebalance"}

        if decision is not None:
            if defer:
                pending = decision      # 익일 종가 체결로 이월
            else:
                cash = _run_decision(decision, prices, d, cash, limit_ref_prices)

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

    # 위험조정·벤치마크 상대 지표. 벤치마크 일간수익을 패널 인덱스에서 산출해 sim_dates 로
    # 정렬(각 sim 일자의 '직전 거래일 대비' 수익). 포트 rets 와 길이·순서가 일치한다.
    bench_rets = None
    if benchmark_series is not None and not benchmark_series.empty:
        bs = benchmark_series.copy()
        bidx = bs.index
        if isinstance(bidx, pd.DatetimeIndex) and bidx.tz is not None:
            bidx = bidx.tz_localize(None)
        bs.index = pd.DatetimeIndex(bidx).normalize()
        bs = bs[~bs.index.duplicated(keep="last")].sort_index()
        bs = bs.reindex(panel.index).ffill()
        bench_rets = bs.pct_change().reindex(sim_dates).to_numpy(dtype=float)
    radj = _risk_adjusted_metrics(rets, bench_rets, rf_annual)
    sharpe = radj["sharpe"]

    # CAGR
    n_days = len(rets)
    cagr = None
    if n_days > 0 and series[-1] > 0:
        cagr = _safe(series[-1] ** (252.0 / n_days) - 1.0)

    holdings = {
        sym: round(float(v) / float(max(series[-1], 1e-12)), 4)
        for sym, v in sorted(val.items(), key=lambda kv: kv[1], reverse=True)
    }

    # 성과귀속·팩터 IC/IR(P1-1) — score 전략이면 팩터별 예측력·롱숏수익을 집계한다.
    factor_ic = _factor_attribution(score_snapshots, panel) if score_snapshots else {}

    logger.info(
        "리밸런싱 백테스트 완료 — 리밸런싱 %d회, 거래 %d건, 총수익률 %.4f, MDD %.4f%s",
        len(turnovers), len(trades), total_return or 0.0, mdd or 0.0,
        " (레짐 오버레이 적용)" if regime_on is not None else "",
    )

    return {
        "total_return": total_return,
        "mdd": mdd,
        "sharpe": sharpe,
        "sortino": radj["sortino"],
        "alpha": radj["alpha"],
        "beta": radj["beta"],
        "information_ratio": radj["information_ratio"],
        "tracking_error": radj["tracking_error"],
        "benchmark_return": radj["benchmark_return"],
        "excess_return": _safe(
            total_return - radj["benchmark_return"]
            if total_return is not None and radj["benchmark_return"] is not None
            else None
        ),
        "cagr": cagr,
        "win_rate": None,  # 포트폴리오 전략에는 종목단위 승률 개념이 맞지 않음
        "num_trades": len(trades),
        "num_rebalances": len(turnovers),
        "num_kills": num_kills,  # MDD 킬스위치 발동 횟수(P1-2)
        "num_panic_events": num_panic_events,  # 패닉 오버레이 Confirm(재진입) 발동 횟수(P2)
        "factor_ic": factor_ic,  # 팩터별 IC·IR·롱숏수익(P1-1, score 전략만; 그 외 {})
        "avg_turnover": _safe(np.mean(turnovers)) if turnovers else 0.0,
        # 실체결 기준(ADV 캡·정수주 절사·상하한가 체결불가 반영 이후) 평균 회전율(§7).
        # avg_turnover(의도된 드리프트 기준)보다 항상 작거나 같다 — 기존 필드 의미는 불변.
        "avg_turnover_actual": _safe(np.mean(turnovers_actual)) if turnovers_actual else 0.0,
        "equity_curve": equity_curve,
        "markers": markers,
        "holdings": holdings,
        "trades": trades,
    }
