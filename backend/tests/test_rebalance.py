"""리밸런싱 코어 순수함수 검증 — 목표비중·주문생성·발화시점."""
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.services.backtest.portfolio import (
    _apply_risk_caps,
    _cap_position_weights,
    _dynamic_universe,
    _factor_attribution,
    _portfolio_vol_ann,
    _regime_on_flags,
    _score_factor_frame,
    _targets_at,
    run_rebalance_backtest,
)
from app.schemas.strategy import RebalanceConfig
from engine import rebalance, rebalance_runner
from engine.rebalance import (
    INVERSE_VOL_MAX_WEIGHT,
    INVERSE_VOL_MIN_WEIGHT,
    cap_normalize_weights,
    compute_rebalance_orders,
    compute_target_weights,
    inverse_vol_weights,
    is_rebalance_due,
)
from engine.rebalance_runner import RebalanceRunner

KST = timezone(timedelta(hours=9))


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype="float64")


# ───────────────────── compute_target_weights ─────────────────────


def test_momentum_selects_top_n_equal_weight():
    cfg = {
        "universe": ["A", "B", "C"],
        "selection": {"method": "momentum", "lookback": 2, "top_n": 2},
    }
    history = {
        "A": _series([100, 100, 130]),  # +30%
        "B": _series([100, 100, 110]),  # +10%
        "C": _series([100, 100, 90]),   # -10%
        "X": _series([100, 100, 200]),  # universe 밖 — 무시
    }
    weights = compute_target_weights(history, cfg)
    assert set(weights) == {"A", "B"}
    assert weights["A"] == pytest.approx(0.5)
    assert weights["B"] == pytest.approx(0.5)


def test_momentum_excludes_insufficient_data():
    cfg = {
        "universe": ["A", "B"],
        "selection": {"method": "momentum", "lookback": 5, "top_n": 2},
    }
    history = {"A": _series([1, 2, 3, 4, 5, 6]), "B": _series([1, 2])}  # B 데이터 부족
    weights = compute_target_weights(history, cfg)
    assert set(weights) == {"A"}
    assert weights["A"] == pytest.approx(1.0)


def test_all_method_equal_weight():
    cfg = {"universe": ["A", "B", "C"], "selection": {"method": "all"}}
    history = {s: _series([1, 2, 3]) for s in ["A", "B", "C"]}
    weights = compute_target_weights(history, cfg)
    assert set(weights) == {"A", "B", "C"}
    assert all(w == pytest.approx(1 / 3) for w in weights.values())


def test_score_method_selects_top_n_equal_weight():
    cfg = {
        "universe": ["A", "B", "C", "D"],
        "selection": {"method": "score", "top_n": 2},
        "weighting": "equal",
    }
    scores = {"A": 1.5, "B": -0.2, "C": 0.8, "D": float("nan")}
    weights = compute_target_weights({}, cfg, scores=scores)
    assert set(weights) == {"A", "C"}
    assert weights["A"] == pytest.approx(0.5)
    assert weights["C"] == pytest.approx(0.5)


def test_score_method_excludes_missing_scores():
    cfg = {
        "universe": ["A", "B"],
        "selection": {"method": "score", "top_n": 2},
    }
    weights = compute_target_weights({}, cfg, scores={"A": 0.1})
    assert set(weights) == {"A"}


def test_score_weighting_rank_based_favors_top_and_stays_positive():
    cfg = {
        "universe": ["A", "B", "C"],
        "selection": {"method": "score", "top_n": 3},
        "weighting": "score",
    }
    # 음수 점수가 섞여도 순위 기반이라 항상 양수 비중.
    scores = {"A": 2.0, "B": -5.0, "C": 0.1}
    weights = compute_target_weights({}, cfg, scores=scores)
    assert set(weights) == {"A", "B", "C"}
    assert all(w > 0 for w in weights.values())
    assert weights["A"] > weights["C"] > weights["B"]
    assert sum(weights.values()) == pytest.approx(1.0)


def test_score_weighting_ignored_when_method_not_score():
    # weighting="score" 는 스키마 레벨에서 method="score" 로만 제한하지만, 코어 함수는
    # 방어적으로 momentum 등 다른 method 에서는 동일비중으로 폴백한다.
    cfg = {
        "universe": ["A", "B"],
        "selection": {"method": "all"},
        "weighting": "score",
    }
    history = {s: _series([1, 2, 3]) for s in ["A", "B"]}
    weights = compute_target_weights(history, cfg)
    assert weights == {"A": pytest.approx(0.5), "B": pytest.approx(0.5)}


# ───────────────────── weighting="inverse_vol"(인버스-변동성) ─────────────────────


def _walk_series(seed: int, n: int, daily_std: float, drift: float = 0.0) -> pd.Series:
    """일간 로그수익률 표준편차를 daily_std 로 통제한 결정론적 가격 시리즈를 만든다."""
    rng = np.random.default_rng(seed)
    log_rets = rng.normal(drift, daily_std, n)
    prices = 100.0 * np.exp(np.cumsum(log_rets))
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    return pd.Series(prices, index=idx, dtype="float64")


def test_cap_normalize_sums_to_one_and_respects_bounds():
    # LOW 는 압도적으로 큰 raw 비중(캡 상한에 걸림), 나머지는 raw 비중이 거의 0(하한에 걸림).
    raw = {"LOW": 1000.0, **{f"S{i}": 0.001 for i in range(9)}}
    weights = cap_normalize_weights(raw, lo=0.005, hi=0.15)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["LOW"] == pytest.approx(INVERSE_VOL_MAX_WEIGHT)
    for i in range(9):
        assert weights[f"S{i}"] >= INVERSE_VOL_MIN_WEIGHT - 1e-9


def test_cap_normalize_infeasible_caps_falls_back_to_equal():
    # n=2, hi=0.15 이면 hi*n=0.3<1 → 캡을 지키며 합=1 을 만들 수 없어 균등비중 폴백.
    weights = cap_normalize_weights({"A": 10.0, "B": 1.0}, lo=0.005, hi=0.15)
    assert weights == {"A": pytest.approx(0.5), "B": pytest.approx(0.5)}


def test_inverse_vol_weights_favors_low_volatility():
    # LOW 는 변동성이 작아 1/vol 이 커야 하므로 다른 종목보다 더 큰 비중을 받는다.
    vols = {"LOW": 0.10, "HIGH": 0.60, **{f"S{i}": 0.30 for i in range(8)}}
    weights = inverse_vol_weights(vols, list(vols))
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["LOW"] > weights["HIGH"]
    assert weights["LOW"] > weights["S0"] > weights["HIGH"]
    for w in weights.values():
        assert INVERSE_VOL_MIN_WEIGHT - 1e-9 <= w <= INVERSE_VOL_MAX_WEIGHT + 1e-9


def test_inverse_vol_weights_excludes_nan_zero_negative_vol():
    # 유효 종목을 7개(A,E,F,G,H,I,J) 확보해 캡이 실현 가능하도록 한다(n<7 이면 hi*n<1
    # 이 되어 균등비중 폴백으로 가려지므로, '낮은 변동성이 더 큰 비중'을 제대로 검증하려면
    # 충분한 표본이 필요하다).
    vols = {
        "A": 0.2, "B": float("nan"), "C": 0.0, "D": -0.1, "E": 0.4,
        "F": 0.25, "G": 0.3, "H": 0.35, "I": 0.45, "J": 0.5,
    }
    selected = list(vols)
    weights = inverse_vol_weights(vols, selected)
    # 무효 종목(B/C/D)은 비중 0(제외), 유효 종목만 배분하되 합은 여전히 1.0.
    assert weights["B"] == 0.0
    assert weights["C"] == 0.0
    assert weights["D"] == 0.0
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["A"] > weights["J"] > 0  # A(vol=0.2) < J(vol=0.5) → A 가 더 큰 비중


def test_inverse_vol_weights_falls_back_to_equal_when_no_valid_vol():
    weights = inverse_vol_weights({"A": None, "B": float("nan"), "C": 0.0}, ["A", "B", "C"])
    assert weights == {"A": pytest.approx(1 / 3), "B": pytest.approx(1 / 3), "C": pytest.approx(1 / 3)}


def test_inverse_vol_weights_empty_selection_returns_empty():
    assert inverse_vol_weights({}, []) == {}


def test_compute_target_weights_inverse_vol_from_price_history():
    # 8종목(캡 실현 가능한 최소 규모) 중 LOW 가 가장 낮은 변동성 → 가장 큰 비중.
    universe = ["LOW"] + [f"S{i}" for i in range(7)]
    history = {"LOW": _walk_series(seed=0, n=300, daily_std=0.003)}
    for i in range(7):
        history[f"S{i}"] = _walk_series(seed=i + 1, n=300, daily_std=0.02)
    cfg = {
        "universe": universe,
        "selection": {"method": "all"},
        "weighting": "inverse_vol",
    }
    weights = compute_target_weights(history, cfg)
    assert set(weights) == set(universe)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["LOW"] == max(weights.values())
    for w in weights.values():
        assert INVERSE_VOL_MIN_WEIGHT - 1e-9 <= w <= INVERSE_VOL_MAX_WEIGHT + 1e-9


def test_compute_target_weights_inverse_vol_defends_nan_and_short_history():
    # SHORT 는 vol_ann 계산에 필요한 최소 이력(20봉)에 못 미침 → NaN → 제외(비중 0).
    universe = ["A", "SHORT"] + [f"S{i}" for i in range(6)]
    history = {"A": _walk_series(seed=0, n=300, daily_std=0.01), "SHORT": _series([100, 101])}
    for i in range(6):
        history[f"S{i}"] = _walk_series(seed=i + 10, n=300, daily_std=0.01)
    cfg = {
        "universe": universe,
        "selection": {"method": "all"},
        "weighting": "inverse_vol",
    }
    weights = compute_target_weights(history, cfg)
    assert weights["SHORT"] == 0.0
    assert sum(weights.values()) == pytest.approx(1.0)


def test_backtest_and_live_paths_agree_on_inverse_vol_weights():
    """백테스트(method="score", _score_factor_frame.vol_ann 재사용)와 실거래
    (method="all", price_history 직접 산출)가 동일 가격 이력에 동일 비중을 산출해야
    한다(엔진 핵심 원칙 — 백테스트/실거래 정합성)."""
    codes = ["A", "B", "C", "D", "E", "F", "G", "H"]
    panel = pd.DataFrame({
        c: _walk_series(seed=i, n=300, daily_std=0.005 + 0.003 * i)
        for i, c in enumerate(codes)
    })
    d = panel.index[-1]

    # 실거래 경로: method="all" 선정 + price_history 로부터 직접 변동성 산출(vols 미주입).
    live_history = {c: panel[c].dropna() for c in codes}
    live_cfg = {"universe": codes, "selection": {"method": "all"}, "weighting": "inverse_vol"}
    live_weights = compute_target_weights(live_history, live_cfg)

    # 백테스트 경로: method="score"(전 종목 동일점수로 top_n=전체 선정) +
    # _score_factor_frame 이 산출한 vol_ann 을 재사용해 주입.
    fac = _score_factor_frame(panel.loc[:d], codes)
    bt_vols = {code: float(v) for code, v in fac["vol_ann"].items() if not math.isnan(v)}
    bt_cfg = {
        "universe": codes,
        "selection": {"method": "score", "top_n": len(codes)},
        "weighting": "inverse_vol",
    }
    bt_scores = {c: 1.0 for c in codes}
    bt_weights = compute_target_weights({}, bt_cfg, scores=bt_scores, vols=bt_vols)

    assert set(live_weights) == set(bt_weights) == set(codes)
    for c in codes:
        assert live_weights[c] == pytest.approx(bt_weights[c], abs=1e-9)
    assert sum(live_weights.values()) == pytest.approx(1.0)
    assert sum(bt_weights.values()) == pytest.approx(1.0)


# ───────────────────── 스키마: weighting="inverse_vol" 허용 범위 ─────────────────────


def test_schema_allows_inverse_vol_for_any_selection_method():
    from app.schemas.strategy import RebalanceConfig

    for method in ("momentum", "score", "all"):
        v = RebalanceConfig.model_validate({
            "type": "rebalance",
            "universe": ["005930", "000660", "035420"],
            "selection": {"method": method, "top_n": 2},
            "weighting": "inverse_vol",
        })
        assert v.weighting == "inverse_vol"


def test_schema_allows_inverse_vol_for_custom_but_not_score():
    from app.schemas.strategy import RebalanceConfig

    rules = _sma_cross_rules()
    v = RebalanceConfig.model_validate({
        "type": "rebalance",
        "universe": ["005930", "000660"],
        "selection": {"method": "custom", **rules},
        "weighting": "inverse_vol",
    })
    assert v.weighting == "inverse_vol"

    with pytest.raises(ValueError):
        RebalanceConfig.model_validate({
            "type": "rebalance",
            "universe": ["005930", "000660"],
            "selection": {"method": "custom", **rules},
            "weighting": "score",
        })


# ───────────────────── 스키마: drift_band 미체결 가드 ─────────────────────


def test_schema_rejects_oversized_drift_band_equal():
    """equal 비중 20종목(각 5%)에 drift_band 0.10 이면 아무 것도 체결 안 됨 → 거부."""
    from app.schemas.strategy import RebalanceConfig

    with pytest.raises(ValueError):
        RebalanceConfig.model_validate({
            "type": "rebalance",
            "selection": {"method": "score", "top_n": 20,
                          "universe_rule": {"source": "KOSPI200", "pick": 40}},
            "weighting": "equal",
            "drift_band_pct": 0.10,  # >= 1/20 = 0.05
        })


def test_schema_accepts_small_drift_band_equal():
    """equal 20종목 + drift_band 0.02(<0.05)는 허용(id=24 형)."""
    from app.schemas.strategy import RebalanceConfig

    v = RebalanceConfig.model_validate({
        "type": "rebalance",
        "selection": {"method": "score", "top_n": 20,
                      "universe_rule": {"source": "KOSPI200", "pick": 40}},
        "weighting": "equal",
        "drift_band_pct": 0.02,
    })
    assert v.drift_band_pct == 0.02


def test_schema_score_rank_allows_moderate_drift_band():
    """순위가중은 상위 집중 의도 — top_n 20 에서 floor=2/21≈0.095, id=23 의 0.05 허용."""
    from app.schemas.strategy import RebalanceConfig

    v = RebalanceConfig.model_validate({
        "type": "rebalance",
        "selection": {"method": "score", "top_n": 20,
                      "universe_rule": {"source": "KOSPI200", "pick": 40}},
        "weighting": "score",
        "drift_band_pct": 0.05,
    })
    assert v.drift_band_pct == 0.05

    # 그러나 0.10 (>2/21) 이면 최상위 종목도 못 사므로 거부.
    with pytest.raises(ValueError):
        RebalanceConfig.model_validate({
            "type": "rebalance",
            "selection": {"method": "score", "top_n": 20,
                          "universe_rule": {"source": "KOSPI200", "pick": 40}},
            "weighting": "score",
            "drift_band_pct": 0.10,
        })


# ───────────────────── 동적 유니버스(_dynamic_universe) ─────────────────────


def _panel(cols: dict[str, list[float]]) -> pd.DataFrame:
    n = max(len(v) for v in cols.values())
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    return pd.DataFrame({k: pd.Series(v, index=idx[: len(v)]) for k, v in cols.items()})


def test_dynamic_universe_ranks_by_relative_strength_and_picks_top():
    # lookback=2 상대강도: WIN=+100%, MID=+20%, LAG=-10% → pick=2 는 WIN,MID.
    panel = _panel({
        "WIN": [10, 10, 20],
        "MID": [10, 10, 12],
        "LAG": [10, 10, 9],
    })
    d = panel.index[-1]
    picked = _dynamic_universe(panel.loc[:d], ["WIN", "MID", "LAG"], {"lookback": 2, "pick": 2})
    assert picked == ["WIN", "MID"]


def test_dynamic_universe_excludes_insufficient_history():
    # NEW 는 lookback+1 개 미만 → 제외. 이력 충분한 OLD 만 선정된다.
    panel = _panel({
        "OLD": [10, 11, 12, 13, 15],
        "NEW": [float("nan"), float("nan"), float("nan"), 100, 101],
    })
    d = panel.index[-1]
    picked = _dynamic_universe(panel.loc[:d], ["OLD", "NEW"], {"lookback": 3, "pick": 5})
    assert picked == ["OLD"]


def test_dynamic_universe_is_look_ahead_safe():
    # 미래에 급등하는 종목이라도, 리밸런싱 시점(중간일) 이전까지는 저조하면 선정 안 됨.
    panel = _panel({
        "EARLY": [10, 12, 14, 14, 14],   # 초반 강세
        "LATE":  [10, 10, 10, 30, 60],   # 후반 급등(미래)
    })
    mid = panel.index[2]  # 3번째 거래일 기준
    picked = _dynamic_universe(panel.loc[:mid], ["EARLY", "LATE"], {"lookback": 2, "pick": 1})
    assert picked == ["EARLY"]  # 미래의 LATE 급등을 참조하지 않는다


def test_targets_at_applies_dynamic_universe_before_scoring():
    # universe_rule 지정 시 후보풀에서 상대강도 상위 pick 만 스코어링 대상이 된다.
    panel = _panel({
        "A": [10, 10, 10, 10, 20],   # 강세
        "B": [10, 10, 10, 10, 15],   # 중간
        "C": [10, 10, 10, 10, 9],    # 약세 → 후보 탈락
    })
    d = panel.index[-1]
    cfg = {
        "universe": ["A", "B", "C"],
        "selection": {
            "method": "score",
            "top_n": 2,
            "universe_rule": {"type": "momentum", "lookback": 4, "pick": 2},
        },
    }
    targets = _targets_at(d, panel, cfg, None)
    assert set(targets) <= {"A", "B"}  # C 는 동적 유니버스에서 배제
    assert "C" not in targets


def test_targets_at_pool_provider_supplies_pointintime_universe():
    # pool_provider 가 그 시점 후보풀을 공급한다. config.universe 는 무시되고
    # 제공된 풀(A,B) 안에서만 선정 → 풀 밖 C 는 절대 편입되지 않는다(생존편향 제거).
    panel = _panel({
        "A": [10, 10, 10, 10, 20],
        "B": [10, 10, 10, 10, 15],
        "C": [10, 10, 10, 10, 99],   # 최강이지만 그 시점 풀에 없음
    })
    # score 팩터(모멘텀 1·3·6개월·52주고가·변동성)가 모두 계산되도록 260거래일 패널.
    import numpy as np
    idx = pd.date_range("2022-01-03", periods=260, freq="B")
    long_panel = pd.DataFrame({
        "A": pd.Series(np.linspace(100, 200, 260), index=idx),  # 강세
        "B": pd.Series(np.linspace(100, 150, 260), index=idx),  # 중간
        "C": pd.Series(np.linspace(100, 400, 260), index=idx),  # 최강이나 풀 밖
    })
    d = long_panel.index[-1]
    # config.universe 를 비워도 pool_provider 가 공급한 풀로 선정돼야 한다
    # (compute_target_weights 가 config.universe 로 재필터하지 않음을 검증).
    cfg = {
        "universe": [],  # 비어 있음 — pool_provider 가 대체
        "selection": {
            "method": "score", "top_n": 2,
            # 가격 팩터만(밸류/퀄리티/성장 데이터 없음 → 0 가중, 점수 NaN 오염 방지)
            "factor_weights": {"momentum": 0.7, "value": 0.0, "lowvol": 0.3,
                               "quality": 0.0, "growth": 0.0},
        },
    }
    targets = _targets_at(d, long_panel, cfg, None, pool_provider=lambda ts: ["A", "B"])
    assert "C" not in targets            # 풀 밖 최강 종목은 절대 편입 안 됨
    assert set(targets) == {"A", "B"}    # 풀(A,B)이 그대로 선정(빈 결과 아님)


# ───────────────────── 스키마: PIT 지수 소스(universe_rule.source) ─────────────────────


def test_config_allows_empty_universe_when_index_source():
    from app.schemas.strategy import RebalanceConfig
    cfg = {
        "type": "rebalance", "universe": [],
        "selection": {"method": "score", "top_n": 10,
                      "universe_rule": {"source": "KOSPI200", "pick": 20}},
    }
    v = RebalanceConfig.model_validate(cfg)
    assert v.selection.universe_rule.source == "KOSPI200"
    assert v.universe == []


def test_config_rejects_empty_universe_when_fixed():
    from app.schemas.strategy import RebalanceConfig
    with pytest.raises(ValueError):
        RebalanceConfig.model_validate({"type": "rebalance", "universe": [],
                                        "selection": {"method": "momentum", "top_n": 3}})


def test_config_index_source_skips_pick_vs_universe_check():
    from app.schemas.strategy import RebalanceConfig
    # pick=50 > len(universe)=0 이지만 지수 소스라 통과. 단 top_n<=pick 은 유지.
    v = RebalanceConfig.model_validate({
        "type": "rebalance", "universe": [],
        "selection": {"method": "score", "top_n": 10,
                      "universe_rule": {"source": "KOSPI200", "pick": 50}},
    })
    assert v.selection.universe_rule.pick == 50
    with pytest.raises(ValueError):  # top_n > pick 은 여전히 거부
        RebalanceConfig.model_validate({
            "type": "rebalance", "universe": [],
            "selection": {"method": "score", "top_n": 60,
                          "universe_rule": {"source": "KOSPI200", "pick": 50}},
        })


# ───────────────────── method="custom"(규칙 기반 편입/청산) ─────────────────────


def _sma_cross_rules() -> dict:
    """SMA5>SMA20 진입 / SMA5<SMA20 청산 규칙(AND 그룹)."""
    return {
        "entry": {
            "combinator": "and",
            "children": [{"left": {"kind": "sma", "period": 5}, "op": ">", "right": {"kind": "sma", "period": 20}}],
        },
        "exit": {
            "combinator": "and",
            "children": [{"left": {"kind": "sma", "period": 5}, "op": "<", "right": {"kind": "sma", "period": 20}}],
        },
    }


def _ramp(down_to_up: bool, n: int = 80) -> pd.Series:
    """V자(하락→상승, 골든크로스) 또는 역V자(상승→하락, 데드크로스) 가격 시리즈."""
    import numpy as np

    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    half = n // 2
    if down_to_up:
        vals = np.concatenate([np.linspace(200, 120, half), np.linspace(120, 260, n - half)])
    else:
        vals = np.concatenate([np.linspace(120, 240, half), np.linspace(240, 100, n - half)])
    return pd.Series(vals, index=idx, dtype="float64")


def test_custom_selects_symbols_currently_in_position_equal_weight():
    cfg = {
        "universe": ["A", "B", "C"],
        "selection": {"method": "custom", **_sma_cross_rules()},
        "weighting": "equal",
    }
    history = {
        "A": _ramp(down_to_up=True),   # 후반 골든크로스 유지 → 편입
        "B": _ramp(down_to_up=False),  # 후반 데드크로스 → 미편입
        "C": _ramp(down_to_up=True),   # 편입
    }
    weights = compute_target_weights(history, cfg)
    assert set(weights) == {"A", "C"}
    assert weights["A"] == pytest.approx(0.5)
    assert weights["C"] == pytest.approx(0.5)


def test_custom_returns_empty_when_none_in_position():
    cfg = {
        "universe": ["A", "B"],
        "selection": {"method": "custom", **_sma_cross_rules()},
        "weighting": "equal",
    }
    history = {"A": _ramp(down_to_up=False), "B": _ramp(down_to_up=False)}
    assert compute_target_weights(history, cfg) == {}


def test_custom_excludes_insufficient_data():
    # SMA20 규칙에 필요한 최소 봉 수(21)에 못 미치는 종목은 제외된다.
    cfg = {
        "universe": ["A", "B"],
        "selection": {"method": "custom", **_sma_cross_rules()},
        "weighting": "equal",
    }
    history = {"A": _ramp(down_to_up=True), "B": _series([1, 2, 3])}
    weights = compute_target_weights(history, cfg)
    assert set(weights) == {"A"}


# ───────────────────── compute_rebalance_orders ─────────────────────


def test_orders_from_empty_positions():
    orders = compute_rebalance_orders(
        targets={"A": 0.5, "B": 0.5},
        positions={},
        prices={"A": 100.0, "B": 200.0},
        capital=1000.0,
        drift_band=0.0,
    )
    assert ("A", "buy", 5) in orders   # floor(0.5*1000/100)
    assert ("B", "buy", 2) in orders   # floor(0.5*1000/200)
    assert all(side == "buy" for _, side, _ in orders)


def test_drift_band_skips_within_tolerance():
    # A 가 이미 정확히 목표 비중(0.5) → 매매 없음
    orders = compute_rebalance_orders(
        targets={"A": 0.5},
        positions={"A": 5},
        prices={"A": 100.0},
        capital=1000.0,
        drift_band=0.05,
    )
    assert orders == []


def test_dropped_symbol_fully_sold_and_sells_precede_buys():
    orders = compute_rebalance_orders(
        targets={"A": 1.0},          # B 는 선정 제외 → 목표 0
        positions={"B": 5},
        prices={"A": 100.0, "B": 100.0},
        capital=1000.0,
        drift_band=0.0,
    )
    assert orders[0] == ("B", "sell", 5)   # 매도가 먼저
    assert ("A", "buy", 10) in orders


def test_missing_price_symbol_skipped():
    orders = compute_rebalance_orders(
        targets={"A": 1.0},
        positions={},
        prices={},  # 현재가 없음
        capital=1000.0,
        drift_band=0.0,
    )
    assert orders == []


# ───────────────────── is_rebalance_due ─────────────────────


@pytest.fixture
def market_open(monkeypatch):
    """is_market_open 을 True 로 고정해 시간·주기 로직만 검증한다."""
    monkeypatch.setattr(rebalance, "is_market_open", lambda now=None: True)


def test_not_due_when_market_closed(monkeypatch):
    monkeypatch.setattr(rebalance, "is_market_open", lambda now=None: False)
    cfg = {"cadence": "daily", "rebalance_time": "09:00"}
    now = datetime(2026, 6, 24, 14, 0, tzinfo=KST)
    assert is_rebalance_due(cfg, None, now) is False


def test_not_due_before_time(market_open):
    cfg = {"cadence": "daily", "rebalance_time": "14:30"}
    now = datetime(2026, 6, 24, 14, 0, tzinfo=KST)
    assert is_rebalance_due(cfg, None, now) is False


def test_daily_due_after_time(market_open):
    cfg = {"cadence": "daily", "rebalance_time": "14:30"}
    now = datetime(2026, 6, 24, 14, 30, tzinfo=KST)
    assert is_rebalance_due(cfg, None, now) is True


def test_daily_not_due_same_day_twice(market_open):
    cfg = {"cadence": "daily", "rebalance_time": "14:30"}
    last = datetime(2026, 6, 24, 14, 31, tzinfo=KST)
    now = datetime(2026, 6, 24, 15, 0, tzinfo=KST)
    assert is_rebalance_due(cfg, last, now) is False


def test_weekly_waits_for_target_weekday(market_open):
    now = datetime(2026, 6, 24, 14, 30, tzinfo=KST)  # weekday 계산은 표준 라이브러리
    target = (now.weekday() + 1) % 5  # 오늘보다 뒤 요일로 설정
    cfg = {"cadence": "weekly", "rebalance_time": "14:30", "rebalance_weekday": target}
    if target > now.weekday():
        assert is_rebalance_due(cfg, None, now) is False


def test_monthly_due_on_new_month(market_open):
    cfg = {"cadence": "monthly", "rebalance_time": "14:30", "rebalance_dom": 1}
    last = datetime(2026, 5, 4, 14, 30, tzinfo=KST)
    now = datetime(2026, 6, 24, 14, 30, tzinfo=KST)
    assert is_rebalance_due(cfg, last, now) is True


def test_monthly_not_due_before_dom(market_open):
    cfg = {"cadence": "monthly", "rebalance_time": "14:30", "rebalance_dom": 15}
    now = datetime(2026, 6, 10, 14, 30, tzinfo=KST)
    assert is_rebalance_due(cfg, None, now) is False


# ───────── 백테스트: 레짐 risk-off→risk-on 회복 시 즉시 재진입 ─────────


def _reentry_cfg() -> dict:
    return {
        "universe": ["A", "B"],
        "selection": {"method": "all"},
        "cadence": "monthly",
        "rebalance_dom": 1,
        "capital": 1_000_000,
        "drift_band_pct": 0.0,
        # 재진입 '타이밍'(청산·회복 당일 체결)을 결정적으로 검증하기 위해 당일 종가 체결을
        # 쓴다(기본 next_close 는 체결이 익일로 밀려 day0/day14 단정과 어긋난다).
        "fill_mode": "same_close",
        "regime_filter": {"enabled": True, "ma_period": 5, "index": "KOSPI"},
    }


def test_regime_reentry_on_recovery_non_rebal_day():
    """레짐 청산 후 회복된 날(비-리밸일)에 즉시 재매수가 발생해야 한다."""
    dates = pd.bdate_range("2024-01-01", periods=20)
    panel = pd.DataFrame({"A": 100.0, "B": 100.0}, index=dates)
    # 기준지수: 0~9 안정(risk-on) → 10~13 급락(risk-off, 청산) → 14~ 급등(risk-on, 회복)
    idx_vals = [100.0] * 10 + [50.0] * 4 + [200.0] * 6
    regime = pd.Series(idx_vals, index=dates)

    res = run_rebalance_backtest(
        panel, _reentry_cfg(), dates[0], dates[-1], regime_series=regime
    )

    exits = [m for m in res["markers"] if m["type"] == "regime_exit"]
    assert exits, "레짐 위험회피 국면에서 청산 마커가 있어야 한다"
    exit_t = exits[0]["t"]

    buys = [t for t in res["trades"] if t["side"] == "buy"]
    buy_dates = sorted({t["t"] for t in buys})
    # 초기 매수(day0=리밸일) + 회복일 재매수 → 서로 다른 매수일 2개 이상
    assert len(buy_dates) >= 2, f"재진입 매수가 없다: {buy_dates}"
    # 청산 이후 시점의 재매수가 존재해야 한다(재진입).
    reentry_buys = [t for t in buys if t["t"] > exit_t]
    assert reentry_buys, "레짐 회복 후 즉시 재진입 매수가 있어야 한다"
    # 재진입일은 회복 첫날(dates[14]), 즉 정기 리밸런싱일(dates[0])이 아니어야 한다.
    reentry_t = min(t["t"] for t in reentry_buys)
    assert reentry_t == pd.Timestamp(dates[14]).isoformat()
    assert reentry_t != pd.Timestamp(dates[0]).isoformat()


def test_no_reentry_without_regime_recovery():
    """레짐 회복이 없으면(계속 risk-on) 재진입 경로가 추가 매수를 만들지 않는다."""
    dates = pd.bdate_range("2024-01-01", periods=20)
    panel = pd.DataFrame({"A": 100.0, "B": 100.0}, index=dates)
    regime = pd.Series([100.0] * 20, index=dates)  # 내내 risk-on
    res = run_rebalance_backtest(
        panel, _reentry_cfg(), dates[0], dates[-1], regime_series=regime
    )
    assert not [m for m in res["markers"] if m["type"] == "regime_exit"]
    buy_dates = sorted({t["t"] for t in res["trades"] if t["side"] == "buy"})
    # 정기 리밸런싱(첫 거래일) 1회만 매수 — 밴드 0 이지만 가격 불변이라 이후 재매매 없음.
    assert buy_dates == [pd.Timestamp(dates[0]).isoformat()]


# ───────── 실거래 러너: 레짐 전환을 cadence 와 분리(백테스트 parity) ─────────


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []  # (channel, data) 발행 기록

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.store:
            return None
        self.store[k] = v
        return True

    async def delete(self, k):
        self.store.pop(k, None)

    async def publish(self, channel, data):
        self.published.append((channel, data))
        return 1

    def alerts(self) -> list[dict]:
        """발행된 이벤트 중 type=="alert" 만 파싱해 반환(공용 채널만 집계, 중복 방지)."""
        import json as _json

        from app.core.channels import ENGINE_EVENTS_CHANNEL

        out = []
        for ch, data in self.published:
            if ch != ENGINE_EVENTS_CHANNEL:
                continue
            try:
                payload = _json.loads(data)
            except (ValueError, TypeError):
                continue
            if payload.get("type") == "alert":
                out.append(payload)
        return out


async def _make_runner(
    monkeypatch, *, risk_off, prev_regime, holdings, due,
    initial_fill=False, last_ran=False,
):
    """레짐 결정 로직만 검증하기 위해 I/O 를 모두 대체한 러너를 만든다."""
    redis = _FakeRedis()
    if prev_regime is not None:
        redis.store["rebalance:regime:1"] = "1" if prev_regime else "0"
    if last_ran:  # 마지막 실행일 기록 존재(=첫 실행 아님) → 부트스트랩 차단
        redis.store["rebalance:last:1"] = "2024-01-01T14:30:00+09:00"
    r = RebalanceRunner(strategy_id=1, redis=redis)
    r._cfg = {
        "universe": ["A", "B"],
        "regime_filter": {"enabled": True},
        "cadence": "monthly",
        "initial_fill_immediate": initial_fill,
    }

    calls: list[dict] = []

    async def fake_rebalance_once(now, risk_off=None, bar_tag="rebal"):
        calls.append({"risk_off": risk_off, "bar_tag": bar_tag})

    set_last: list = []

    async def fake_set_last(dt):
        set_last.append(dt)

    async def fake_is_risk_off():
        return risk_off

    async def fake_has_holdings():
        return holdings

    monkeypatch.setattr(rebalance_runner, "is_market_open", lambda now=None: True)
    monkeypatch.setattr(rebalance_runner, "is_rebalance_due", lambda *a, **k: due)
    monkeypatch.setattr(r, "_rebalance_once", fake_rebalance_once)
    monkeypatch.setattr(r, "_set_last", fake_set_last)
    monkeypatch.setattr(r, "_is_risk_off", fake_is_risk_off)
    monkeypatch.setattr(r, "_has_holdings", fake_has_holdings)

    await r._tick_once()
    return calls, set_last, redis


async def test_runner_liquidates_immediately_on_risk_off(monkeypatch):
    calls, set_last, redis = await _make_runner(
        monkeypatch, risk_off=True, prev_regime=False, holdings=True, due=False
    )
    assert calls == [{"risk_off": True, "bar_tag": "regime"}]
    assert set_last == []  # 청산은 월간 스케줄을 소비하지 않는다
    assert redis.store["rebalance:regime:1"] == "1"


async def test_runner_reenters_on_recovery_without_cadence(monkeypatch):
    # 직전 risk-off, 지금 risk-on, 보유 없음, cadence 미도래 → 즉시 재진입.
    calls, set_last, _ = await _make_runner(
        monkeypatch, risk_off=False, prev_regime=True, holdings=False, due=False
    )
    assert calls == [{"risk_off": False, "bar_tag": "regime"}]
    assert set_last == []  # 재진입은 cadence 스케줄을 소비하지 않는다


async def test_runner_no_reentry_when_already_holding(monkeypatch):
    calls, set_last, _ = await _make_runner(
        monkeypatch, risk_off=False, prev_regime=True, holdings=True, due=False
    )
    assert calls == []  # 이미 보유 중이면 재진입하지 않음
    assert set_last == []


async def test_runner_regular_cadence_consumes_schedule(monkeypatch):
    calls, set_last, _ = await _make_runner(
        monkeypatch, risk_off=False, prev_regime=False, holdings=True, due=True
    )
    assert calls == [{"risk_off": False, "bar_tag": "rebal"}]
    assert len(set_last) == 1  # 정기 발화만 마지막 실행일을 소비


async def test_runner_bootstrap_fills_immediately_on_cold_start(monkeypatch):
    # initial_fill_immediate=true, 첫 실행(last 없음)·보유 없음·발화일 미도래 → 즉시 발화.
    calls, set_last, _ = await _make_runner(
        monkeypatch, risk_off=False, prev_regime=None, holdings=False, due=False,
        initial_fill=True,
    )
    assert calls == [{"risk_off": False, "bar_tag": "rebal"}]
    assert len(set_last) == 1  # 부트스트랩은 이번 주기 정규 리밸런싱을 대체(스케줄 소비)


async def test_runner_bootstrap_disabled_by_default(monkeypatch):
    # 옵션 미설정이면 발화일 미도래 시 아무 것도 하지 않음(기존 동작 보존).
    calls, set_last, _ = await _make_runner(
        monkeypatch, risk_off=False, prev_regime=None, holdings=False, due=False,
        initial_fill=False,
    )
    assert calls == []
    assert set_last == []


async def test_runner_bootstrap_skips_when_holdings_exist(monkeypatch):
    # 이미 보유가 있으면 첫 실행이 아니라고 보고 부트스트랩하지 않는다.
    calls, set_last, _ = await _make_runner(
        monkeypatch, risk_off=False, prev_regime=None, holdings=True, due=False,
        initial_fill=True,
    )
    assert calls == []
    assert set_last == []


async def test_runner_bootstrap_skips_when_already_ran(monkeypatch):
    # 마지막 실행일 기록이 있으면(재기동 등) 부트스트랩하지 않는다.
    calls, set_last, _ = await _make_runner(
        monkeypatch, risk_off=False, prev_regime=None, holdings=False, due=False,
        initial_fill=True, last_ran=True,
    )
    assert calls == []
    assert set_last == []


async def test_runner_bootstrap_yields_to_risk_off(monkeypatch):
    # 콜드 스타트라도 레짐 위험회피면 매수하지 않고 청산 경로만 탄다(현금 유지).
    calls, set_last, _ = await _make_runner(
        monkeypatch, risk_off=True, prev_regime=None, holdings=False, due=False,
        initial_fill=True,
    )
    assert calls == [{"risk_off": True, "bar_tag": "regime"}]
    assert set_last == []


# ───────── 레짐 히스테리시스(비대칭 밴드) — 백테스트 _regime_on_flags ─────────


def _regime_index(vals: list[float]) -> tuple[pd.DatetimeIndex, pd.Series]:
    idx = pd.date_range("2024-01-01", periods=len(vals), freq="D")
    return idx, pd.Series(vals, index=idx, dtype="float64")


def test_regime_hysteresis_reentry_buffer_suppresses_whipsaw():
    """MA 를 살짝 넘었다 빠지는 톱니에서 reentry_buffer 가 가짜 반등 재진입을 억제한다."""
    # 20일 안정(MA≈100) → 2일 얕은 하락(청산) → 9일 톱니(MA 살짝 상회) → 확실한 회복.
    vals = [100.0] * 20 + [95.0] * 2 + [101.0] * 9 + [110.0] * 10
    idx, series = _regime_index(vals)
    on0 = _regime_on_flags(idx, series, 20, exit_buffer=0.0, reentry_buffer=0.0)
    on_buf = _regime_on_flags(idx, series, 20, exit_buffer=0.0, reentry_buffer=0.03)

    # 얕은 하락에서 두 경우 모두 청산(off)된다.
    assert not bool(on0.iloc[21])
    assert not bool(on_buf.iloc[21])
    # 톱니(101, MA 를 근소하게 상회): 무버퍼는 즉시 재진입, 3% 버퍼는 재진입 억제.
    assert on0.iloc[22:31].any()          # 무버퍼는 살짝만 넘어도 재진입
    assert not on_buf.iloc[22:31].any()   # 버퍼는 얕은 반등을 걸러 off 유지
    # 밴드 밖으로 확실히 회복(110)하면 버퍼가 있어도 재진입.
    assert bool(on_buf.iloc[-1])
    assert bool(on0.iloc[-1])


def test_regime_hysteresis_exit_buffer_holds_shallow_dip():
    """MA 를 살짝 하회하는 얕은 조정은 exit_buffer 로 청산을 유예하고, 급락엔 청산한다."""
    vals = [100.0] * 20 + [98.0] * 9 + [80.0] * 8
    idx, series = _regime_index(vals)
    on0 = _regime_on_flags(idx, series, 20, exit_buffer=0.0, reentry_buffer=0.0)
    on_buf = _regime_on_flags(idx, series, 20, exit_buffer=0.05, reentry_buffer=0.0)

    # 얕은 조정(98, MA 근소 하회): 무버퍼는 청산, 5% 버퍼는 보유 유지.
    assert not on0.iloc[20:29].all()      # 무버퍼는 98<MA 로 청산 발생
    assert bool(on_buf.iloc[20:29].all())  # 버퍼는 얕은 조정을 버틴다
    # 확실한 급락(80)에서는 둘 다 청산.
    assert not bool(on0.iloc[-1])
    assert not bool(on_buf.iloc[-1])


def test_regime_hysteresis_zero_buffer_matches_stateless():
    """buffer=0.0 이면 기존 무상태 rs>=ma 판정과 완전히 동일(회귀 보장)."""
    import numpy as np

    rng = np.random.default_rng(0)
    vals = list(100.0 + np.cumsum(rng.normal(0.0, 2.0, 60)))
    idx, series = _regime_index(vals)
    on = _regime_on_flags(idx, series, 10, exit_buffer=0.0, reentry_buffer=0.0)

    # 무상태 참조 구현: rs>=ma, MA 미확정(초기) 구간은 True.
    rs = series.reindex(idx).ffill()
    ma = rs.rolling(10, min_periods=max(5, 10 // 2)).mean()
    ref = (rs >= ma).where(ma.notna(), other=True).fillna(True).astype(bool)
    pd.testing.assert_series_equal(on, ref, check_names=False)


# ───────── 레짐 히스테리시스 — 실거래 러너 _is_risk_off(직전 상태 밴드) ─────────


async def _risk_off_with(
    monkeypatch, *, close_vals, prev_off, exit_buffer=0.0, reentry_buffer=0.0
):
    """지수 조회를 대체해 _is_risk_off 의 밴드 판정만 검증한다."""
    redis = _FakeRedis()
    if prev_off is not None:
        redis.store["rebalance:regime:1"] = "1" if prev_off else "0"
    r = RebalanceRunner(strategy_id=1, redis=redis)
    r._cfg = {
        "regime_filter": {
            "enabled": True,
            "ma_period": 5,
            "exit_buffer_pct": exit_buffer,
            "reentry_buffer_pct": reentry_buffer,
        }
    }
    idx = pd.date_range("2024-01-01", periods=len(close_vals), freq="D")
    df = pd.DataFrame({"close": close_vals}, index=idx)
    monkeypatch.setattr(rebalance_runner, "_fetch_index_ohlcv", lambda *a, **k: df)
    return await r._is_risk_off()


async def test_is_risk_off_reentry_buffer_suppresses(monkeypatch):
    # 직전 off, 지수(101)가 MA(99.8)를 근소 상회 → 3% 버퍼면 재진입 억제(risk_off 유지).
    off = await _risk_off_with(
        monkeypatch, close_vals=[98, 99, 100, 101, 101],
        prev_off=True, reentry_buffer=0.03,
    )
    assert off is True
    # 무버퍼면 MA 상회 즉시 재진입 → risk_off False.
    off0 = await _risk_off_with(
        monkeypatch, close_vals=[98, 99, 100, 101, 101],
        prev_off=True, reentry_buffer=0.0,
    )
    assert off0 is False
    # 밴드 밖 확실한 회복(110)이면 버퍼가 있어도 재진입.
    off_recover = await _risk_off_with(
        monkeypatch, close_vals=[98, 99, 100, 101, 110],
        prev_off=True, reentry_buffer=0.03,
    )
    assert off_recover is False


async def test_is_risk_off_exit_buffer_holds(monkeypatch):
    # 직전 on, 지수(99)가 MA(100.2)를 근소 하회 → 5% 버퍼면 청산 유예(risk_off False).
    off = await _risk_off_with(
        monkeypatch, close_vals=[102, 101, 100, 99, 99],
        prev_off=False, exit_buffer=0.05,
    )
    assert off is False
    # 무버퍼면 MA 하회 즉시 청산 → risk_off True.
    off0 = await _risk_off_with(
        monkeypatch, close_vals=[102, 101, 100, 99, 99],
        prev_off=False, exit_buffer=0.0,
    )
    assert off0 is True


async def test_is_risk_off_no_prev_state_uses_stateless(monkeypatch):
    # 최초(직전 상태 없음): rs>=ma → on(off False), rs<ma → off True.
    assert await _risk_off_with(
        monkeypatch, close_vals=[100, 100, 100, 100, 101], prev_off=None,
        reentry_buffer=0.03, exit_buffer=0.05,
    ) is False
    assert await _risk_off_with(
        monkeypatch, close_vals=[100, 100, 100, 100, 90], prev_off=None,
        reentry_buffer=0.03, exit_buffer=0.05,
    ) is True


# ───────── 실거래 러너 시점별(PIT) 유니버스 해석 · 동적 축소 배선 ─────────


class _FakeDbCtx:
    """AsyncSessionLocal() 대체 — 실제 DB 없이 async with 를 통과시킨다."""

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False


async def test_resolve_universe_fixed_returns_config_universe():
    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._cfg = {"universe": ["005930", "000660"], "selection": {}}
    assert await r._resolve_universe(date(2025, 6, 30)) == ["005930", "000660"]


async def test_resolve_universe_pit_source_uses_index_members(monkeypatch):
    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._cfg = {"universe": [], "selection": {"universe_rule": {"source": "KOSPI200"}}}
    import app.services.data.krx_index as ki

    monkeypatch.setattr(ki, "index_members", lambda as_of, index="KOSPI200": ["A", "B", "C"])
    assert await r._resolve_universe(date(2025, 6, 30)) == ["A", "B", "C"]


async def test_resolve_universe_pit_empty_falls_back_to_config(monkeypatch):
    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._cfg = {"universe": ["005930"], "selection": {"universe_rule": {"source": "KOSPI200"}}}
    import app.services.data.krx_index as ki

    monkeypatch.setattr(ki, "index_members", lambda as_of, index="KOSPI200": [])
    assert await r._resolve_universe(date(2025, 6, 30)) == ["005930"]  # 공백 → 폴백


async def test_resolve_universe_pit_error_falls_back_to_config(monkeypatch):
    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._cfg = {"universe": ["005930"], "selection": {"universe_rule": {"source": "KOSPI200"}}}
    import app.services.data.krx_index as ki

    def _boom(as_of, index="KOSPI200"):
        raise RuntimeError("인증 실패")

    monkeypatch.setattr(ki, "index_members", _boom)
    assert await r._resolve_universe(date(2025, 6, 30)) == ["005930"]  # 예외 → 폴백


async def test_rebalance_once_pit_narrows_and_scores_injected_universe(monkeypatch):
    """score+PIT+동적룰: 지수 멤버십 → 상대강도 상위 pick 축소 → 그 유니버스로 스코어링."""
    from decimal import Decimal

    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._user_id = 1
    r._cfg = {
        "universe": [],
        "capital": 10_000_000,
        "cadence": "quarterly",
        "selection": {
            "method": "score",
            "top_n": 2,
            "factor_weights": {"value": 1.0},
            "universe_rule": {
                "source": "KOSPI200", "type": "momentum", "lookback": 2, "pick": 2,
            },
        },
    }

    # PIT 후보풀 4종목.
    monkeypatch.setattr(r, "_resolve_universe", lambda as_of: _async(["A", "B", "C", "D"]))

    # 상대강도(lookback=2) 랭킹: C(2.0) > B(0.5) > A(0.2) > D(-0.2) → pick 2 = [C, B].
    hist = {
        "A": _series([10, 11, 12]),
        "B": _series([10, 11, 15]),
        "C": _series([10, 20, 30]),
        "D": _series([10, 9, 8]),
    }
    monkeypatch.setattr(r, "_seed_history", lambda pool, min_bars=0: _async(hist))

    captured: dict = {}

    def fake_scores(universe, as_of, factor_weights, neutralize="none"):
        captured["universe"] = list(universe)
        return {sym: 1.0 for sym in universe}

    monkeypatch.setattr(rebalance_runner, "compute_universe_scores", fake_scores)
    monkeypatch.setattr(rebalance_runner, "AsyncSessionLocal", lambda: _FakeDbCtx())
    monkeypatch.setattr(r, "_holdings", lambda db, pool: _async({}))
    monkeypatch.setattr(
        r, "_quotes",
        lambda syms: _async({s: Decimal("10000") for s in syms}),
    )
    orders_seen: list = []
    monkeypatch.setattr(
        r, "_execute_orders",
        lambda orders, prices, positions, bar_ts, *a: _async(orders_seen.extend(orders)),
    )

    await r._rebalance_once(datetime(2025, 4, 1, 14, 30, tzinfo=KST), risk_off=False)

    # 스코어링은 상대강도로 축소된 [C, B] 에만 수행돼야 한다(전체 4종목 아님).
    assert set(captured["universe"]) == {"C", "B"}
    # 목표 2종목(C·B) 신규 매수 주문이 생성된다.
    assert {sym for sym, side, qty in orders_seen} == {"C", "B"}
    assert all(side == "buy" for _, side, _ in orders_seen)


async def test_preview_returns_orders_without_executing(monkeypatch):
    """preview 는 실제 주문을 내지 않고 산출 주문·목표·보유를 dict 로 돌려준다."""
    from decimal import Decimal

    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._user_id = 1
    r._cfg = {
        "universe": [],
        "capital": 10_000_000,
        "selection": {
            "method": "score", "top_n": 2, "factor_weights": {"value": 1.0},
            "universe_rule": {"source": "KOSPI200", "type": "momentum", "lookback": 2, "pick": 2},
        },
    }
    monkeypatch.setattr(r, "_is_risk_off", lambda: _async(False))
    monkeypatch.setattr(r, "_resolve_universe", lambda as_of: _async(["A", "B", "C", "D"]))
    hist = {
        "A": _series([10, 11, 12]), "B": _series([10, 11, 15]),
        "C": _series([10, 20, 30]), "D": _series([10, 9, 8]),
    }
    monkeypatch.setattr(r, "_seed_history", lambda pool, min_bars=0: _async(hist))
    monkeypatch.setattr(
        rebalance_runner, "compute_universe_scores",
        lambda universe, as_of, fw, neutralize="none": {sym: 1.0 for sym in universe},
    )
    monkeypatch.setattr(rebalance_runner, "AsyncSessionLocal", lambda: _FakeDbCtx())
    monkeypatch.setattr(r, "_holdings", lambda db, pool: _async({}))
    monkeypatch.setattr(
        r, "_quotes", lambda syms: _async({s: Decimal("10000") for s in syms})
    )
    executed: list = []
    monkeypatch.setattr(
        r, "_execute_orders",
        lambda *a, **k: _async(executed.append(True)),
    )

    out = await r.preview()

    assert executed == []  # 미체결(주문 실행 안 함)
    assert out["risk_off"] is False
    assert set(out["targets"]) == {"C", "B"}  # 상대강도 축소 후 스코어 선정
    assert {sym for sym, side, qty in out["orders"]} == {"C", "B"}


def _async(value):
    """monkeypatch 용 코루틴 팩토리 — 지정 값을 반환하는 완료된 코루틴."""
    async def _coro():
        return value

    return _coro()


# ───────────────────── 리스크 레이어(P1-2) ─────────────────────


def test_cap_position_weights_redistributes_excess():
    """단일 종목 상한 초과분을 상한 미만 종목에 비례 재분배(합 보존)."""
    capped = _cap_position_weights({"A": 0.6, "B": 0.2, "C": 0.2}, 0.4)
    assert abs(capped["A"] - 0.4) < 1e-9
    assert abs(capped["B"] - 0.3) < 1e-9 and abs(capped["C"] - 0.3) < 1e-9
    assert abs(sum(capped.values()) - 1.0) < 1e-9


def test_cap_position_weights_excess_to_cash_when_no_room():
    """모든 종목이 상한이면 초과분은 현금(합<1)."""
    capped = _cap_position_weights({"A": 0.5, "B": 0.5}, 0.3)
    assert all(v <= 0.3 + 1e-9 for v in capped.values())
    assert sum(capped.values()) < 1.0 - 1e-9


def test_portfolio_vol_ann_orders_by_risk():
    """공분산 기반 연율 변동성이 고변동 종목에서 더 크다."""
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2023-01-01", periods=120)
    hist = pd.DataFrame({
        "LOW": 100 * np.exp(np.cumsum(rng.normal(0, 0.005, len(idx)))),
        "HIGH": 100 * np.exp(np.cumsum(rng.normal(0, 0.04, len(idx)))),
    }, index=idx)
    assert _portfolio_vol_ann(hist, {"HIGH": 1.0}, 60) > _portfolio_vol_ann(hist, {"LOW": 1.0}, 60)


def test_apply_risk_caps_vol_target_deleverages_only():
    """변동성 타겟팅은 고변동 포트를 디레버리징하고 저변동 포트는 확대하지 않는다."""
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2023-01-01", periods=120)
    hist = pd.DataFrame({
        "LOW": 100 * np.exp(np.cumsum(rng.normal(0, 0.005, len(idx)))),
        "HIGH": 100 * np.exp(np.cumsum(rng.normal(0, 0.04, len(idx)))),
    }, index=idx)
    risk = {"target_vol": 0.10, "vol_lookback": 60, "max_leverage": 1.0}
    assert sum(_apply_risk_caps({"HIGH": 1.0}, hist, risk).values()) < 1.0 - 1e-6
    assert abs(sum(_apply_risk_caps({"LOW": 1.0}, hist, risk).values()) - 1.0) < 1e-6


def _kill_cfg(risk: dict | None) -> dict:
    return {
        "universe": ["A", "B"],
        "selection": {"method": "all"},
        "cadence": "monthly",
        "rebalance_dom": 1,
        "capital": 1_000_000,
        "drift_band_pct": 0.0,
        "fill_mode": "same_close",
        "risk_layer": risk,
    }


def test_mdd_kill_switch_fires_and_reports():
    """고점 대비 낙폭이 임계를 넘으면 킬스위치가 발동해 현금화하고 num_kills 로 노출된다."""
    dates = pd.bdate_range("2024-01-01", periods=30)
    # A·B 동일비중(50/50)에서 포트폴리오 낙폭이 25% 를 넘도록 A 를 -60% 급락시킨다
    # (포트 ≈ 0.5×(-0.6) = -30%) 후 반등 — 킬스위치 발동을 유도.
    a = [100.0] * 2 + [40.0] * 3 + [120.0] * 25
    panel = pd.DataFrame({"A": a, "B": [100.0] * 30}, index=dates)
    res = run_rebalance_backtest(panel, _kill_cfg({"mdd_kill_pct": 0.25, "mdd_rearm_days": 3}),
                                 dates[0], dates[-1])
    assert res["num_kills"] >= 1
    assert any(m["type"] == "mdd_exit" for m in res["markers"])


def test_mdd_kill_switch_inert_when_threshold_deep():
    """임계가 자연 낙폭보다 깊으면 킬스위치는 발동하지 않고 결과에 영향이 없다."""
    dates = pd.bdate_range("2024-01-01", periods=30)
    a = [100.0] * 2 + [90.0] * 3 + [110.0] * 25  # 최대 낙폭 ~10%
    panel = pd.DataFrame({"A": a, "B": [100.0] * 30}, index=dates)
    base = run_rebalance_backtest(panel, _kill_cfg(None), dates[0], dates[-1])
    deep = run_rebalance_backtest(panel, _kill_cfg({"mdd_kill_pct": 0.50}), dates[0], dates[-1])
    assert deep["num_kills"] == 0
    assert abs(deep["total_return"] - base["total_return"]) < 1e-9


def test_max_position_pct_must_exceed_drift_band():
    """집중 한도가 drift_band 이하이면 진입이 갇히므로 스키마가 거부한다."""
    with pytest.raises(ValueError):
        RebalanceConfig(
            universe=["005930", "000660", "035420"],
            selection={"method": "momentum", "top_n": 3, "lookback": 60},
            drift_band_pct=0.05,
            risk_layer={"max_position_pct": 0.05},
        )


# ───────────────────── 성과귀속·팩터 IC/IR(P1-1) ─────────────────────


def _attr_snapshots(alpha, n_rebal=12, gap=21, noise_score=0.3, seed=0):
    """팩터점수가 종목 alpha 를 반영하고, 가격도 alpha 방향으로 흐르는 합성 스냅샷·패널."""
    rng = np.random.default_rng(seed)
    syms = [f"S{i:02d}" for i in range(len(alpha))]
    dates = pd.bdate_range("2022-01-03", periods=n_rebal * gap + 5)
    prices = {s: [100.0] for s in syms}
    for _di in range(1, len(dates)):
        for j, s in enumerate(syms):
            prices[s].append(prices[s][-1] * (1 + alpha[j] * 0.002 + rng.normal(0, 0.01)))
    panel = pd.DataFrame(prices, index=dates)
    snaps = []
    for k in [i * gap for i in range(n_rebal)]:
        sc = pd.Series(alpha + rng.normal(0, noise_score, len(alpha)), index=syms)
        snaps.append((dates[k], pd.DataFrame({"score_momentum": sc, "score": sc})))
    return snaps, panel


def test_factor_attribution_detects_predictive_factor():
    """가격을 예측하는 팩터는 IC>0·IR>0·롱숏수익>0·적중률>0.5."""
    rng = np.random.default_rng(0)
    snaps, panel = _attr_snapshots(rng.normal(0, 1, 30))
    attr = _factor_attribution(snaps, panel)
    m = attr["score_momentum"]
    assert m["ic_mean"] > 0.1 and m["ic_ir"] > 0
    assert m["ls_return"] > 0 and m["ic_hit"] > 0.5


def test_factor_attribution_noise_factor_near_zero():
    """가격과 무관한 팩터는 IC≈0."""
    rng = np.random.default_rng(1)
    syms = [f"S{i:02d}" for i in range(30)]
    dates = pd.bdate_range("2022-01-03", periods=12 * 21 + 5)
    panel = pd.DataFrame(
        {s: 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(dates)))) for s in syms}, index=dates)
    snaps = [(dates[i * 21], pd.DataFrame({"score": pd.Series(rng.normal(0, 1, 30), index=syms)}))
             for i in range(12)]
    attr = _factor_attribution(snaps, panel)
    assert abs(attr["score"]["ic_mean"]) < 0.2


def test_factor_attribution_needs_min_intervals():
    """스냅샷이 3구간 미만이면 빈 결과(IR 불안정 방지)."""
    rng = np.random.default_rng(0)
    snaps, panel = _attr_snapshots(rng.normal(0, 1, 30), n_rebal=2)
    assert _factor_attribution(snaps, panel) == {}


def test_rebalance_backtest_exposes_factor_ic_for_score_method():
    """score 전략 백테스트는 factor_ic 를 노출하고, momentum 전략은 빈 dict."""
    dates = pd.bdate_range("2022-01-03", periods=400)
    rng = np.random.default_rng(3)
    panel = pd.DataFrame(
        {f"S{i:02d}": 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, len(dates)))) for i in range(12)},
        index=dates)
    score_cfg = {
        "universe": list(panel.columns),
        "selection": {"method": "score", "top_n": 4},
        "weighting": "equal", "cadence": "monthly", "rebalance_dom": 1,
        "drift_band_pct": 0.02, "fill_mode": "same_close",
    }
    res = run_rebalance_backtest(panel, score_cfg, dates[0], dates[-1])
    assert "factor_ic" in res and isinstance(res["factor_ic"], dict) and res["factor_ic"]
    mom_cfg = {**score_cfg, "selection": {"method": "momentum", "top_n": 4, "lookback": 60}}
    res2 = run_rebalance_backtest(panel, mom_cfg, dates[0], dates[-1])
    assert res2["factor_ic"] == {}


# ───────── 실거래 러너: MDD 킬스위치(백테스트 parity) ─────────


class _MddFakeDb:
    """AsyncSessionLocal() 대체 — scalars 로 주입한 포지션 리스트를 돌려준다."""

    def __init__(self, positions):
        self._positions = positions

    async def scalars(self, stmt):
        return list(self._positions)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pos:
    def __init__(self, symbol, qty, avg_price):
        from decimal import Decimal

        self.symbol = symbol
        self.qty = Decimal(str(qty))
        self.avg_price = Decimal(str(avg_price))


async def test_live_equity_capital_plus_unrealized_pnl(monkeypatch):
    """라이브 자산가치 = 배정자본 + 보유 종목 미실현손익(Σ 수량×(현재가−평단))."""
    from decimal import Decimal

    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._user_id = 1
    r._cfg = {"capital": 10_000_000}
    monkeypatch.setattr(r, "_resolve_universe", lambda as_of: _async(["A", "B"]))
    positions = [_Pos("A", 10, 1000), _Pos("B", 5, 2000)]
    monkeypatch.setattr(rebalance_runner, "AsyncSessionLocal", lambda: _MddFakeDb(positions))
    monkeypatch.setattr(
        r, "_quotes", lambda syms: _async({"A": Decimal("1200"), "B": Decimal("1800")})
    )
    # 10M + 10×(1200−1000) + 5×(1800−2000) = 10M + 2000 − 1000 = 10,001,000
    assert await r._live_equity() == 10_001_000.0


async def test_live_equity_no_holdings_returns_capital(monkeypatch):
    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._user_id = 1
    r._cfg = {"capital": 7_000_000}
    monkeypatch.setattr(r, "_resolve_universe", lambda as_of: _async(["A"]))
    monkeypatch.setattr(rebalance_runner, "AsyncSessionLocal", lambda: _MddFakeDb([]))
    assert await r._live_equity() == 7_000_000.0


async def test_live_equity_quote_failure_is_conservative(monkeypatch):
    """시세 조회 실패 종목은 손익 0(평단 평가)으로 보수 처리 — 데이터 결손이 오발동을 유발하지 않는다."""
    from decimal import Decimal

    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._user_id = 1
    r._cfg = {"capital": 10_000_000}
    monkeypatch.setattr(r, "_resolve_universe", lambda as_of: _async(["A", "B"]))
    positions = [_Pos("A", 10, 1000), _Pos("B", 5, 2000)]
    monkeypatch.setattr(rebalance_runner, "AsyncSessionLocal", lambda: _MddFakeDb(positions))
    # B 시세 누락 → B 는 손익 0, A 만 반영: 10M + 10×200 = 10,002,000
    monkeypatch.setattr(r, "_quotes", lambda syms: _async({"A": Decimal("1200")}))
    assert await r._live_equity() == 10_002_000.0


async def _mdd_runner(monkeypatch, *, mdd_kill_pct=0.2, rearm_days=5):
    """MDD 킬스위치 상태기계만 검증하기 위해 자산가치·거래일 판정을 대체한 러너."""
    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._cfg = {"risk_layer": {"mdd_kill_pct": mdd_kill_pct, "mdd_rearm_days": rearm_days}}
    holder = {"eq": 10_000_000.0, "now": datetime(2024, 1, 2, 14, 30, tzinfo=KST)}
    monkeypatch.setattr(r, "_live_equity", lambda: _async(holder["eq"]))
    monkeypatch.setattr(rebalance_runner, "now_kst", lambda: holder["now"])
    # 테스트에서는 pykrx 조회 없이 주말 여부만으로 영업일 판정(월~금).
    monkeypatch.setattr(rebalance_runner, "is_business_day", lambda d: d.weekday() < 5)
    return r, holder


async def test_mdd_kill_lifecycle_trigger_cooldown_rearm(monkeypatch):
    """고점 추적 → 낙폭 임계 초과 발동 → 쿨다운 유지 → rearm_days 경과 재가동(고점 리셋)."""
    r, holder = await _mdd_runner(monkeypatch, mdd_kill_pct=0.2, rearm_days=5)

    async def step(eq, d):
        holder["eq"] = eq
        holder["now"] = datetime(d.year, d.month, d.day, 14, 30, tzinfo=KST)
        return await r._evaluate_mdd_kill(0.2, 5)

    # 1) 초기: 고점 10M, 미발동.
    assert await step(10_000_000, date(2024, 1, 2)) is False
    # 2) 상승 11M: 고점 갱신, 미발동.
    assert await step(11_000_000, date(2024, 1, 3)) is False
    # 3) 급락 8.7M(고점 11M 대비 −20.9% ≤ −20%): 발동.
    assert await step(8_700_000, date(2024, 1, 8)) is True  # 월요일
    state = await r._get_mdd_state()
    assert state["killed"] is True and state["kill_date"] == "2024-01-08"
    # 4) 2 영업일 경과(01-10): 자산가치 회복해도 쿨다운 미경과 → 발동 유지.
    assert await step(11_000_000, date(2024, 1, 10)) is True
    # 5) 5 영업일 경과(01-15 월): 재가동(해제) + 고점 기준선을 현재 자산가치로 리셋.
    assert await step(11_000_000, date(2024, 1, 15)) is False
    state = await r._get_mdd_state()
    assert state["killed"] is False and state["hwm"] == 11_000_000
    assert state["kill_date"] is None


async def test_mdd_kill_publishes_alert_once_on_trigger(monkeypatch):
    """MDD 킬스위치 발동 전환 시 critical 알림을 1회 발행한다(발동 지속 중 재발송 없음)."""
    r, holder = await _mdd_runner(monkeypatch, mdd_kill_pct=0.2, rearm_days=5)
    r._user_id = 11

    async def step(eq, d):
        holder["eq"] = eq
        holder["now"] = datetime(d.year, d.month, d.day, 14, 30, tzinfo=KST)
        return await r._evaluate_mdd_kill(0.2, 5)

    await step(11_000_000, date(2024, 1, 2))  # 고점 11M
    await step(8_700_000, date(2024, 1, 8))   # 고점 대비 −20.9% ≤ −20% → 발동
    alerts = r.redis.alerts()
    assert len(alerts) == 1
    assert alerts[0]["code"] == "mdd_kill"
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["strategy_id"] == 1
    assert alerts[0]["user_id"] == 11

    # 발동 유지 구간(쿨다운) — 재평가해도 재발송하지 않는다.
    await step(8_700_000, date(2024, 1, 9))
    assert len(r.redis.alerts()) == 1


async def test_mdd_kill_inert_when_threshold_deep(monkeypatch):
    """임계가 자연 낙폭보다 깊으면 발동하지 않는다(회귀 안전)."""
    r, holder = await _mdd_runner(monkeypatch, mdd_kill_pct=0.5, rearm_days=5)

    async def step(eq, d):
        holder["eq"] = eq
        holder["now"] = datetime(d.year, d.month, d.day, 14, 30, tzinfo=KST)
        return await r._evaluate_mdd_kill(0.5, 5)

    assert await step(10_000_000, date(2024, 1, 2)) is False
    assert await step(9_000_000, date(2024, 1, 3)) is False  # −10% < −50% 임계
    assert await step(8_500_000, date(2024, 1, 4)) is False


async def test_mdd_state_lost_reinitializes_without_false_trigger(monkeypatch):
    """Redis 상태 유실 시 고점이 현재 자산가치로 재설정되어 오발동하지 않는다(안전측)."""
    r, holder = await _mdd_runner(monkeypatch, mdd_kill_pct=0.2, rearm_days=5)
    # 상태 없음(유실) + 자산가치가 낮아도, 그 값이 새 고점이 되므로 낙폭 0 → 미발동.
    holder["eq"] = 5_000_000.0
    holder["now"] = datetime(2024, 1, 8, 14, 30, tzinfo=KST)
    assert await r._evaluate_mdd_kill(0.2, 5) is False
    state = await r._get_mdd_state()
    assert state["hwm"] == 5_000_000.0 and state["killed"] is False


async def test_tick_mdd_killed_liquidates_and_skips_regime(monkeypatch):
    """킬스위치 발동 시 레짐/cadence 를 건너뛰고 즉시 mdd 태그로 전량 청산·조기 반환한다."""
    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._cfg = {
        "universe": ["A", "B"],
        "regime_filter": {"enabled": True},  # 레짐이 켜져 있어도 MDD 가 우선함을 검증
        "risk_layer": {"mdd_kill_pct": 0.2, "mdd_rearm_days": 5},
        "cadence": "monthly",
    }
    monkeypatch.setattr(rebalance_runner, "is_market_open", lambda now=None: True)
    monkeypatch.setattr(r, "_evaluate_mdd_kill", lambda pct, days: _async(True))

    calls: list = []

    async def fake_rebalance_once(now, risk_off=None, bar_tag="rebal", liq_kind="regime"):
        calls.append({"risk_off": risk_off, "bar_tag": bar_tag, "liq_kind": liq_kind})

    regime_calls: list = []

    async def fake_is_risk_off():
        regime_calls.append(True)
        return False

    monkeypatch.setattr(r, "_rebalance_once", fake_rebalance_once)
    monkeypatch.setattr(r, "_is_risk_off", fake_is_risk_off)

    await r._tick_once()

    assert calls == [{"risk_off": True, "bar_tag": "mdd", "liq_kind": "mdd"}]
    assert regime_calls == []  # MDD 조기 반환 → 레짐 평가로 진행하지 않음


async def test_tick_mdd_not_killed_proceeds_to_cadence(monkeypatch):
    """킬스위치 미발동이면 기존 정기 리밸런싱 경로가 정상 동작한다(회귀 보존)."""
    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    r._cfg = {
        "universe": ["A", "B"],
        "risk_layer": {"mdd_kill_pct": 0.2, "mdd_rearm_days": 5},
        "cadence": "monthly",
    }
    monkeypatch.setattr(rebalance_runner, "is_market_open", lambda now=None: True)
    monkeypatch.setattr(rebalance_runner, "is_rebalance_due", lambda *a, **k: True)
    monkeypatch.setattr(r, "_evaluate_mdd_kill", lambda pct, days: _async(False))

    calls: list = []

    async def fake_rebalance_once(now, risk_off=None, bar_tag="rebal", liq_kind="regime"):
        calls.append({"risk_off": risk_off, "bar_tag": bar_tag, "liq_kind": liq_kind})

    set_last: list = []
    monkeypatch.setattr(r, "_rebalance_once", fake_rebalance_once)
    monkeypatch.setattr(r, "_set_last", lambda dt: _async(set_last.append(dt)))

    await r._tick_once()

    # 정기 발화: risk_off False·bar_tag "rebal"·기본 liq_kind, 마지막 실행일 소비.
    assert calls == [{"risk_off": False, "bar_tag": "rebal", "liq_kind": "regime"}]
    assert len(set_last) == 1


async def test_mdd_sell_reason_labels_kill(monkeypatch):
    """MDD 청산 매도 사유가 킬스위치로 표기된다(레짐 현금화와 구분)."""
    r = RebalanceRunner(strategy_id=1, redis=_FakeRedis())
    kill = r._sell_reason("A", {}, risk_off=True, sell_qty=3, liq_kind="mdd")
    assert "MDD 킬스위치" in kill
    regime = r._sell_reason("A", {}, risk_off=True, sell_qty=3, liq_kind="regime")
    assert "레짐 위험회피" in regime
