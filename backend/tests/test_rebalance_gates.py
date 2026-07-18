"""품질 점수 하한(min_score)·변동성 적격 게이트(vol_gate) 확장 검증.

- 확장 1: min_score 필터(현금 대기)
- 확장 2: vol_gate(절대 변동성 적격 게이트)
- 하위호환: 새 필드 미지정 시 기존과 동일 선정
- 미래참조 부재: 체결일(d 이후) 봉을 바꿔도 직전 시점(d) 판단 불변
"""
import numpy as np
import pandas as pd
import pytest

from app.schemas.strategy import RebalanceConfig, VolGate
from app.services.backtest.portfolio import _targets_at
from engine.rebalance import (
    _realized_vol_ann,
    compute_target_weights,
    passes_vol_gate,
)


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype="float64")


def _prices_from_logrets(start: float, logrets: list[float]) -> pd.Series:
    """시작가와 로그수익률 리스트로 종가 Series 를 만든다(len = 1 + len(logrets))."""
    px = [start]
    for r in logrets:
        px.append(px[-1] * float(np.exp(r)))
    return _series(px)


# ─────────────────────── 확장 1: min_score ───────────────────────


def _score_cfg(min_score=..., top_n=5):
    sel = {"method": "score", "top_n": top_n, "weighting": "equal"}
    cfg = {"universe": ["A", "B", "C"], "selection": sel, "weighting": "equal"}
    if min_score is not ...:
        sel["min_score"] = min_score
    return cfg


def test_min_score_filters_below_threshold():
    scores = {"A": 2.0, "B": 0.5, "C": -1.0}
    weights = compute_target_weights({}, _score_cfg(min_score=0.6), scores=scores)
    # 0.6 미만(B=0.5, C=-1.0) 제외 → A 만 선정.
    assert set(weights) == {"A"}
    assert weights["A"] == pytest.approx(1.0)


def test_min_score_keeps_ties_at_threshold():
    # 임계값과 정확히 같은 점수는 통과(>= 비교).
    scores = {"A": 0.6, "B": 0.6, "C": 0.59}
    weights = compute_target_weights({}, _score_cfg(min_score=0.6), scores=scores)
    assert set(weights) == {"A", "B"}


def test_min_score_all_below_returns_cash():
    scores = {"A": 0.1, "B": 0.2, "C": -0.5}
    weights = compute_target_weights({}, _score_cfg(min_score=5.0), scores=scores)
    assert weights == {}  # 전원 미달 → 전량 현금


def test_min_score_none_equals_no_filter():
    scores = {"A": 2.0, "B": 0.5, "C": -1.0}
    base = compute_target_weights({}, _score_cfg(), scores=scores)          # 키 없음
    explicit_none = compute_target_weights({}, _score_cfg(min_score=None), scores=scores)
    assert base == explicit_none
    assert set(base) == {"A", "B", "C"}  # 필터 없으면 전부 선정(top_n=5)


def test_min_score_applied_before_top_n_truncation():
    # top_n 절단 '전에' 필터가 적용되는지: 상위 2개 중 1개가 미달이면 그 다음 통과
    # 종목이 채워지는 게 아니라, 통과 종목만 남은 뒤 상위 top_n 을 취한다.
    scores = {"A": 3.0, "B": 0.4, "C": 2.0}
    cfg = _score_cfg(min_score=1.0, top_n=2)
    weights = compute_target_weights({}, cfg, scores=scores)
    assert set(weights) == {"A", "C"}  # B(0.4) 탈락, A·C 통과


# ─────────────────────── 확장 2: vol_gate ───────────────────────
#
# 최근 5봉 변동성이 장기(10봉) 대비 높고 순매수(우상향)인 스파이크 종목.
_SPIKE_LOGRETS = [
    0.001, -0.001, 0.001, -0.001, 0.001,   # 과거 5봉: 저변동
    0.04, -0.02, 0.05, -0.01, 0.05,        # 최근 5봉: 고변동·순상승
]
_GATE = {"spike_lookback": 5, "base_lookback": 10}


def test_passes_vol_gate_accepts_uptrend_spike():
    c = _prices_from_logrets(100.0, _SPIKE_LOGRETS)
    rv_s = _realized_vol_ann(c, 5)
    rv_l = _realized_vol_ann(c, 10)
    ratio = rv_s / rv_l
    assert ratio > 1.0  # 스파이크(단기>장기)
    gate = {**_GATE, "spike_min": ratio * 0.99, "cap": rv_s * 1.01,
            "require_uptrend": True}
    assert passes_vol_gate(c, gate) is True


def test_vol_gate_spike_min_boundary():
    c = _prices_from_logrets(100.0, _SPIKE_LOGRETS)
    ratio = _realized_vol_ann(c, 5) / _realized_vol_ann(c, 10)
    base = {**_GATE, "cap": 5.0, "require_uptrend": False}
    assert passes_vol_gate(c, {**base, "spike_min": ratio * 0.999}) is True
    assert passes_vol_gate(c, {**base, "spike_min": ratio * 1.001}) is False


def test_vol_gate_cap_boundary():
    c = _prices_from_logrets(100.0, _SPIKE_LOGRETS)
    rv_s = _realized_vol_ann(c, 5)
    base = {**_GATE, "spike_min": 0.001, "require_uptrend": False}
    assert passes_vol_gate(c, {**base, "cap": rv_s * 1.001}) is True
    assert passes_vol_gate(c, {**base, "cap": rv_s * 0.999}) is False


def test_vol_gate_spike_max_boundary():
    # 역방향(calm gate): 비율이 spike_max 를 넘으면 불통과, 미설정(None)이면 무제한.
    c = _prices_from_logrets(100.0, _SPIKE_LOGRETS)
    ratio = _realized_vol_ann(c, 5) / _realized_vol_ann(c, 10)
    base = {**_GATE, "spike_min": 0.001, "cap": 5.0, "require_uptrend": False}
    assert passes_vol_gate(c, {**base, "spike_max": ratio * 1.001}) is True
    assert passes_vol_gate(c, {**base, "spike_max": ratio * 0.999}) is False
    assert passes_vol_gate(c, {**base, "spike_max": None}) is True


def test_vol_gate_spike_max_schema_validates():
    # spike_max < spike_min 은 거부, spike_max >= spike_min 은 허용.
    with pytest.raises(ValueError):
        VolGate(spike_min=1.0, spike_max=0.5)
    g = VolGate(spike_min=0.01, spike_max=0.9)
    assert g.spike_max == 0.9
    assert VolGate().spike_max is None  # 기본 None = 기존 동작


def test_vol_gate_uptrend_requirement():
    # 최근 구간이 순하락(우하향)이라 close <= SMA(spike_lookback) → uptrend 불통과.
    down = [
        0.001, -0.001, 0.001, -0.001, 0.001,
        -0.04, 0.02, -0.05, 0.01, -0.05,
    ]
    c = _prices_from_logrets(100.0, down)
    sma5 = float(c.tail(5).mean())
    assert float(c.iloc[-1]) <= sma5  # 우하향 확인
    base = {**_GATE, "spike_min": 0.001, "cap": 5.0}
    assert passes_vol_gate(c, {**base, "require_uptrend": True}) is False
    assert passes_vol_gate(c, {**base, "require_uptrend": False}) is True


def test_vol_gate_insufficient_data_excluded():
    # base_lookback+1 봉 미달 → 보수적으로 불통과.
    short = _prices_from_logrets(100.0, _SPIKE_LOGRETS[:6])  # 7봉 < 11
    gate = {**_GATE, "spike_min": 0.001, "cap": 5.0, "require_uptrend": False}
    assert passes_vol_gate(short, gate) is False
    assert passes_vol_gate(None, gate) is False


def test_vol_gate_in_selection_filters_and_all_fail_is_cash():
    pass_series = _prices_from_logrets(100.0, _SPIKE_LOGRETS)
    flat_series = _series([100.0] * 11)  # 무변동 → 스파이크 조건 미달
    history = {"A": pass_series, "B": flat_series}
    gate = {**_GATE, "spike_min": 1.05, "cap": 5.0, "require_uptrend": False}
    cfg = {
        "universe": ["A", "B"],
        "selection": {"method": "score", "top_n": 5, "vol_gate": gate},
        "weighting": "equal",
    }
    scores = {"A": 1.0, "B": 2.0}  # B 점수가 더 높지만 게이트 불통과
    weights = compute_target_weights(history, cfg, scores=scores)
    assert set(weights) == {"A"}

    # 게이트를 아무도 통과 못하면 전량 현금.
    cfg_hard = {**cfg, "selection": {**cfg["selection"],
                                     "vol_gate": {**gate, "cap": 0.0001}}}
    assert compute_target_weights(history, cfg_hard, scores=scores) == {}


def test_vol_gate_combines_with_min_score():
    # 게이트 통과 → min_score 필터 → top_n. B 는 게이트 불통과, A 는 min_score 미달.
    pass_hi = _prices_from_logrets(100.0, _SPIKE_LOGRETS)
    pass_lo = _prices_from_logrets(100.0, _SPIKE_LOGRETS)
    flat = _series([100.0] * 11)
    history = {"A": pass_lo, "B": flat, "C": pass_hi}
    gate = {**_GATE, "spike_min": 1.05, "cap": 5.0, "require_uptrend": False}
    cfg = {
        "universe": ["A", "B", "C"],
        "selection": {"method": "score", "top_n": 5,
                      "vol_gate": gate, "min_score": 1.0},
        "weighting": "equal",
    }
    scores = {"A": 0.5, "B": 3.0, "C": 2.0}
    weights = compute_target_weights(history, cfg, scores=scores)
    assert set(weights) == {"C"}  # A: min_score 미달, B: 게이트 불통과


# ─────────────────────── 하위호환 회귀 ───────────────────────


def test_backward_compat_score_selection_unchanged():
    # 새 필드 미지정 score cfg 는, price_history 를 넘기든 말든(게이트 미적용) 결과 동일.
    scores = {"A": 1.5, "B": -0.2, "C": 0.8, "D": float("nan")}
    cfg = {
        "universe": ["A", "B", "C", "D"],
        "selection": {"method": "score", "top_n": 2},
        "weighting": "equal",
    }
    # 게이트가 있었다면 불통과였을 종가를 넘겨도, vol_gate 미지정이라 무시된다.
    noise_history = {s: _series([100.0] * 11) for s in ["A", "B", "C", "D"]}
    baseline = compute_target_weights({}, cfg, scores=scores)
    with_history = compute_target_weights(noise_history, cfg, scores=scores)
    assert baseline == with_history == {"A": pytest.approx(0.5), "C": pytest.approx(0.5)}


def test_backward_compat_explicit_none_matches_absent():
    scores = {"A": 1.5, "B": 0.8, "C": 0.3}
    absent = {
        "universe": ["A", "B", "C"],
        "selection": {"method": "score", "top_n": 5},
        "weighting": "equal",
    }
    with_none = {
        "universe": ["A", "B", "C"],
        "selection": {"method": "score", "top_n": 5,
                      "min_score": None, "vol_gate": None},
        "weighting": "equal",
    }
    assert compute_target_weights({}, absent, scores=scores) == \
        compute_target_weights({}, with_none, scores=scores)


def test_backward_compat_non_score_methods_ignore_new_fields():
    # momentum 경로는 min_score/vol_gate 를 아예 참조하지 않는다.
    cfg = {
        "universe": ["A", "B", "C"],
        "selection": {"method": "momentum", "lookback": 2, "top_n": 2,
                      "min_score": 999.0,
                      "vol_gate": {"spike_lookback": 5, "base_lookback": 10,
                                   "cap": 0.0001}},
    }
    history = {
        "A": _series([100, 100, 130]),
        "B": _series([100, 100, 110]),
        "C": _series([100, 100, 90]),
    }
    weights = compute_target_weights(history, cfg)
    assert set(weights) == {"A", "B"}  # min_score/vol_gate 무시(모멘텀 상위 2)


def test_schema_accepts_new_fields_and_validates():
    cfg = RebalanceConfig(
        universe=["005930", "000660"],
        selection={
            "method": "score", "top_n": 2, "min_score": 0.5,
            "vol_gate": {"spike_lookback": 20, "base_lookback": 252,
                         "spike_min": 1.4, "cap": 0.9, "require_uptrend": True},
        },
    )
    assert cfg.selection.min_score == 0.5
    assert cfg.selection.vol_gate.spike_min == 1.4
    # base_lookback < spike_lookback 은 거부.
    with pytest.raises(ValueError):
        VolGate(spike_lookback=100, base_lookback=20)
    # 기본값(둘 다 None)도 유효.
    plain = RebalanceConfig(
        universe=["005930", "000660", "035420", "051910", "005380"]
    )
    assert plain.selection.min_score is None
    assert plain.selection.vol_gate is None


# ─────────────────────── 미래참조 부재 ───────────────────────


def _long_uptrend_panel(n=160):
    """가격 팩터(mom_6m 등)와 vol_gate 가 모두 산출되도록 충분히 긴 상승 패널."""
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    rng = np.random.default_rng(42)
    cols = {}
    for k, drift in (("A", 0.0015), ("B", 0.0010), ("C", 0.0008)):
        rets = drift + rng.normal(0, 0.01, n)
        cols[k] = pd.Series(100.0 * np.exp(np.cumsum(rets)), index=idx)
    return pd.DataFrame(cols)


def _lookahead_cfg():
    return {
        "universe": ["A", "B", "C"],
        "selection": {
            "method": "score", "top_n": 2,
            "factor_weights": {"momentum": 0.7, "value": 0.0, "lowvol": 0.3,
                               "quality": 0.0, "growth": 0.0},
            # 게이트를 실제로 통과시켜 선정이 비지 않게(느슨한 임계) 하되, 코드 경로는 탄다.
            "vol_gate": {"spike_lookback": 10, "base_lookback": 60,
                         "spike_min": 0.0001, "cap": 100.0, "require_uptrend": False},
            "min_score": -100.0,
        },
    }


def test_vol_gate_no_lookahead_future_bars_do_not_change_decision():
    panel = _long_uptrend_panel(160)
    d = panel.index[140]  # 판단 시점(직전 확정 종가). 체결은 d+1 이후.
    cfg = _lookahead_cfg()

    before = _targets_at(d, panel, cfg, None)
    assert before  # 게이트 통과·비지 않은 목표(look-ahead 검증이 유의미하도록)

    # d 이후(체결일 포함 미래) 봉을 극단값으로 교란.
    mutated = panel.copy()
    mutated.loc[mutated.index > d] = mutated.loc[mutated.index > d] * 100.0
    after = _targets_at(d, mutated, cfg, None)

    assert before == after  # 미래 봉 변경이 d 시점 판단에 전혀 영향 없음

    # 특히 바로 다음 봉(d+1, 체결일)만 바꿔도 불변.
    nxt = panel.index[141]
    single = panel.copy()
    single.loc[nxt] = single.loc[nxt] * 0.01
    assert _targets_at(d, single, cfg, None) == before
