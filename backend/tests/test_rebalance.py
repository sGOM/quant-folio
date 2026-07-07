"""리밸런싱 코어 순수함수 검증 — 목표비중·주문생성·발화시점."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.services.backtest.portfolio import (
    _dynamic_universe,
    _regime_on_flags,
    _targets_at,
    run_rebalance_backtest,
)
from engine import rebalance, rebalance_runner
from engine.rebalance import (
    compute_rebalance_orders,
    compute_target_weights,
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
    d = panel.index[-1]
    cfg = {
        "universe": ["A", "B", "C"],  # 무시됨
        "selection": {"method": "score", "top_n": 2},
    }
    targets = _targets_at(d, panel, cfg, None, pool_provider=lambda ts: ["A", "B"])
    assert "C" not in targets
    assert set(targets) <= {"A", "B"}


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

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ex=None):
        self.store[k] = v


async def _make_runner(monkeypatch, *, risk_off, prev_regime, holdings, due):
    """레짐 결정 로직만 검증하기 위해 I/O 를 모두 대체한 러너를 만든다."""
    redis = _FakeRedis()
    if prev_regime is not None:
        redis.store["rebalance:regime:1"] = "1" if prev_regime else "0"
    r = RebalanceRunner(strategy_id=1, redis=redis)
    r._cfg = {
        "universe": ["A", "B"],
        "regime_filter": {"enabled": True},
        "cadence": "monthly",
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

    await r._maybe_rebalance()
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
