"""패닉셀(투매) 지표 검증 — 시그널 수식·게이팅·라벨 판정.

핵심 설계 의도: 가격 급락 단독으로는 '패닉' 라벨을 못 달게 게이팅(가격축≥60 AND
(거래폭증 vr≥1.5 OR 브레드스축≥60))해 단순 조정 오탐을 막는다. 아래 시나리오는
그 게이팅과 하드트리거·라벨 임계가 의도대로 동작하는지 확인한다.
"""
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

import app.services.metrics.panic as P


# ─────────────────────────── 합성 데이터 빌더 ───────────────────────────


def _index_df(*, r1: float, r5: float, dd60: float, vr: float, clv: float = 0.1) -> pd.DataFrame:
    """지수 OHLCV 90봉을 만들어 마지막 봉이 목표 r1/r5/dd60/vr/clv를 만족시킨다."""
    n = 90
    rng = np.random.default_rng(0)
    close = 1000 * np.cumprod(1 + rng.normal(0, 0.008, n))  # 저변동 배경(std20용)
    close[-2] = 1000.0
    close[-1] = close[-2] * (1 + r1)
    close[-6] = close[-1] / (1 + r5)
    close[-30] = close[-1] / (1 + dd60)  # 60일 내 고점
    tv = np.full(n, 1e12)
    tv[-1] = 1e12 * vr
    hi = close.copy()
    lo = close.copy()
    lo[-1] = close[-1] - 100.0
    hi[-1] = lo[-1] + 100.0 / max(clv, 1e-6)  # clv=(c-lo)/(hi-lo)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": close, "high": hi, "low": lo, "close": close,
         "volume": np.ones(n), "trading_value": tv},
        index=idx,
    )


def _breadth_df(*, bdr: float, cr5: float, cr10: float, n: int = 1000) -> pd.DataFrame:
    """전 종목 등락률(%) 프레임. 하락/급락/폭락 비율을 목표치로 맞춘다."""
    chg = np.zeros(n)
    chg[: int(bdr * n)] = -1.0
    chg[: int(cr5 * n)] = -6.0
    chg[: int(cr10 * n)] = -12.0
    return pd.DataFrame({"등락률": chg, "거래량": np.ones(n)})


@pytest.fixture
def patch_fetch(monkeypatch):
    """지수·브레드스 조회를 합성 데이터로 대체하는 헬퍼를 돌려준다."""
    def _apply(index_df, breadth_df):
        monkeypatch.setattr(P, "_fetch_index_ohlcv", lambda *a, **k: index_df)
        monkeypatch.setattr(P, "_fetch_price_change", lambda *a, **k: breadth_df)
    return _apply


def _run(patch_fetch, index_df, breadth_df):
    patch_fetch(index_df, breadth_df)
    out = P.compute_panic("KOSPI", date(2024, 8, 5))
    assert len(out.items) == 1
    return out.items[0]


# ─────────────────────────── 시나리오 ───────────────────────────


def test_panic_day_flagged(patch_fetch):
    """급락+거래폭증+브레드스붕괴 → 패닉 라벨·게이팅·하드트리거."""
    it = _run(
        patch_fetch,
        _index_df(r1=-0.055, r5=-0.12, dd60=-0.13, vr=2.5, clv=0.05),
        _breadth_df(bdr=0.90, cr5=0.35, cr10=0.12),
    )
    assert it.level == "panic"
    assert it.gated is True
    assert it.hard_trigger is True
    assert it.score >= 60


def test_correction_not_panic(patch_fetch):
    """-3% 조정이나 거래·브레드스 약함 → 게이팅 미충족, 패닉 아님."""
    it = _run(
        patch_fetch,
        _index_df(r1=-0.030, r5=-0.05, dd60=-0.04, vr=1.1),
        _breadth_df(bdr=0.68, cr5=0.05, cr10=0.01),
    )
    assert it.gated is False
    assert it.level != "panic"


def test_quiet_drop_gated_out(patch_fetch):
    """큰 낙폭(-4.8%)이나 거래량·브레드스 미확인 → 게이팅이 패닉 승격 차단.

    가격축 서브스코어는 60 이상일 수 있으나, vr<1.5 이고 브레드스축<60 이므로
    게이팅 미충족 → 절대 패닉이 아니어야 한다(설계 핵심)."""
    it = _run(
        patch_fetch,
        _index_df(r1=-0.048, r5=-0.07, dd60=-0.06, vr=1.05),
        _breadth_df(bdr=0.60, cr5=0.03, cr10=0.0),
    )
    assert it.gated is False
    assert it.hard_trigger is False
    assert it.level != "panic"


def test_normal_day(patch_fetch):
    """평범한 등락일 → 정상."""
    it = _run(
        patch_fetch,
        _index_df(r1=-0.005, r5=0.01, dd60=-0.02, vr=1.0),
        _breadth_df(bdr=0.50, cr5=0.02, cr10=0.0),
    )
    assert it.level == "normal"
    assert it.score < P._SCORE_CAUTION


def test_hard_trigger_cr10(patch_fetch):
    """전 종목 10% 이상이 -10% 폭락 → 점수/게이팅 무관 하드트리거 패닉."""
    it = _run(
        patch_fetch,
        _index_df(r1=-0.02, r5=-0.03, dd60=-0.05, vr=1.2),
        _breadth_df(bdr=0.70, cr5=0.12, cr10=0.11),
    )
    assert it.hard_trigger is True
    assert it.level == "panic"


def test_breadth_fetch_failure_not_downgraded(patch_fetch):
    """브레드스 조회 실패여도 강한 지수 신호는 저평가되지 않는다(재정규화, 리뷰 High#1).

    r1=-5%(s1 100)·r5=-10%(s2 80)·vr=2.2(s4 100)로 가용 신호가 모두 강한데,
    고정 /100 정규화라면 브레드스 결측(가중 30)만큼 깎여 강등된다. 재정규화 후에는
    가용 신호의 가중평균이므로 점수가 높게(≥60) 유지돼야 한다."""
    it = _run(
        patch_fetch,
        _index_df(r1=-0.05, r5=-0.10, dd60=-0.12, vr=2.2),
        pd.DataFrame(),  # 브레드스 없음
    )
    assert it.universe == 0
    assert it.signals  # 지수 시그널은 채워짐
    assert it.score >= 60  # 재정규화로 저평가되지 않음
    assert it.breadth_sub is None  # 브레드스축은 결측


def test_hard_trigger_floors_score(patch_fetch):
    """하드트리거 발동 시 점수는 최소 경계선(60) 이상으로 하한이 걸린다(UI 일관성)."""
    # 가격은 얌전(r1=-2%)하지만 bdr>=0.92 & vr>=2.0 → 하드트리거
    it = _run(
        patch_fetch,
        _index_df(r1=-0.02, r5=-0.03, dd60=-0.05, vr=2.3),
        _breadth_df(bdr=0.93, cr5=0.10, cr10=0.05),
    )
    assert it.hard_trigger is True
    assert it.level == "panic"
    assert it.score >= P._SCORE_WARNING


def test_unavailable_market_reported(patch_fetch, monkeypatch):
    """지수 조회가 데이터 부족(None)이면 해당 시장이 unavailable에 명시된다."""
    monkeypatch.setattr(P, "_fetch_index_ohlcv", lambda *a, **k: None)
    monkeypatch.setattr(P, "_fetch_price_change", lambda *a, **k: pd.DataFrame())
    out = P.compute_panic("KOSPI", date(2024, 8, 5))
    assert out.items == []
    assert out.unavailable == ["KOSPI"]


# ─────────────────────────── 순수 헬퍼 ───────────────────────────


def test_ramp_bounds():
    # 하락 신호: warn=0점, panic=100점 (둘 다 음수, panic<warn)
    assert P._ramp(-0.025, -0.025, -0.045) == 0.0
    assert P._ramp(-0.045, -0.025, -0.045) == 100.0
    assert P._ramp(-0.10, -0.025, -0.045) == 100.0   # 초과 클램프
    assert P._ramp(0.0, -0.025, -0.045) == 0.0        # 미달 클램프
    assert abs(P._ramp(-0.035, -0.025, -0.045) - 50.0) < 1e-9
    assert P._ramp(None, -0.025, -0.045) is None


def test_wavg_skips_none():
    assert P._wavg([(20.0, 100.0), (15.0, None)]) == 100.0  # None 제외 정규화
    assert P._wavg([(20.0, None), (15.0, None)]) is None
    assert abs(P._wavg([(20.0, 100.0), (15.0, 0.0)]) - (2000.0 / 35.0)) < 1e-9


def test_max_opt():
    assert P._max_opt(None, 5.0) == 5.0
    assert P._max_opt(5.0, None) == 5.0
    assert P._max_opt(3.0, 7.0) == 7.0
    assert P._max_opt(None, None) is None


def test_cr5_row_shows_own_subscore_not_combined(patch_fetch):
    """cr5 표시 막대는 자기 램프(s6_cr5)여야 한다 — cr10 이 드라이버여도 오해 금지.

    cr5=0.15(warn 경계 → s6_cr5=0), cr10=0.075(램프 중간 → s6_cr10≈50) 이면
    종합점수 가중항은 s6=max=50 을 쓰지만, cr5 행 subscore 까지 50 을 쓰면
    -5% 값(경계)과 어긋나 오해를 준다. cr5 행은 0, cr10 행은 50 이어야 한다."""
    it = _run(
        patch_fetch,
        _index_df(r1=-0.02, r5=-0.03, dd60=-0.05, vr=1.2),
        _breadth_df(bdr=0.60, cr5=0.15, cr10=0.075),
    )
    by_key = {s.key: s for s in it.signals}
    assert by_key["cr5"].subscore == pytest.approx(0.0)
    assert by_key["cr10"].subscore == pytest.approx(50.0)


# ─────────────────────── 롤링 시계열 브레드스 캐시 ───────────────────────


def _flat_index_df(n: int = 40) -> pd.DataFrame:
    """평평한 지수 OHLCV n봉(브레드스 캐시 동작 검증용, 값 자체는 무의미)."""
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = np.full(n, 1000.0)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close,
         "volume": np.ones(n), "trading_value": np.full(n, 1e12)},
        index=idx,
    )


def test_series_failed_breadth_not_cached(tmp_path, monkeypatch):
    """브레드스 조회 실패(universe=0)는 캐시에 굳지 않아야 한다 — 굳으면 일시적 실패가
    그날 지표를 영구 결측으로 만들어 자가복구가 불가능해진다(리뷰 High)."""
    index_df = _flat_index_df(40)
    monkeypatch.setattr(P, "_fetch_index_ohlcv", lambda *a, **k: index_df)
    monkeypatch.setattr(P, "_fetch_price_change", lambda *a, **k: None)  # 항상 실패

    start, end = index_df.index[10].date(), index_df.index[20].date()
    P.compute_panic_series("KOSPI", start, end, cache_dir=str(tmp_path))

    cache_file = tmp_path / "panic_breadth_KOSPI.json"
    cached = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    assert cached == {}, "실패한 브레드스 조회는 캐시에 저장되지 않아야 한다"

    # 다음 실행에서 조회가 성공하면 그때 캐시에 저장된다(정상 표본만 영속화).
    def _ok(*a, **k):
        return pd.DataFrame({"등락률": [-1.0, -6.0, 1.0], "거래량": [1, 1, 1]})

    monkeypatch.setattr(P, "_fetch_price_change", _ok)
    P.compute_panic_series("KOSPI", start, end, cache_dir=str(tmp_path))
    cached2 = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    assert cached2, "유효한 브레드스는 캐시에 저장돼 재실행 시 재조회를 피한다"


def _walk_index_df(n: int = 150) -> pd.DataFrame:
    """변동 있는 지수 OHLCV n봉(랜덤워크). 윈도우 슬라이싱 동등성 검증용."""
    rng = np.random.default_rng(7)
    close = 1000 * np.cumprod(1 + rng.normal(0, 0.012, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    hi = close * 1.01
    lo = close * 0.99
    tv = np.full(n, 1e12) * (1 + rng.normal(0, 0.3, n)).clip(0.2, None)
    return pd.DataFrame(
        {"open": close, "high": hi, "low": lo, "close": close,
         "volume": np.ones(n), "trading_value": tv},
        index=idx,
    )


def test_series_lookback_window_matches_full_history(tmp_path, monkeypatch):
    """확장윈도우 슬라이싱(O(n))이 전체 이력 계산(O(n²))과 결과가 완전히 동일해야 한다.

    _index_signals 는 최장 60봉(dd60)만 참조하므로 마지막 _SERIES_LOOKBACK(65)봉만
    넘겨도 전체를 넘길 때와 동일하다. _SERIES_LOOKBACK 을 매우 크게(전체 이력) 바꾼
    실행과 비교해 이 동치가 유지되는지(윈도우가 값을 바꾸지 않는지) 못박는다."""
    df = _walk_index_df(150)
    monkeypatch.setattr(P, "_fetch_index_ohlcv", lambda *a, **k: df)
    # 브레드스는 두 실행에서 동일한 결정적 값(동등성 비교는 지수신호 윈도잉이 핵심).
    monkeypatch.setattr(
        P, "_fetch_price_change",
        lambda *a, **k: pd.DataFrame({"등락률": [-1.0, -6.0, 0.5], "거래량": [1, 1, 1]}),
    )
    start, end = df.index[70].date(), df.index[140].date()

    windowed = P.compute_panic_series("KOSPI", start, end, cache_dir=str(tmp_path / "w"))
    monkeypatch.setattr(P, "_SERIES_LOOKBACK", 10_000)  # 사실상 전체 이력
    full = P.compute_panic_series("KOSPI", start, end, cache_dir=str(tmp_path / "f"))

    assert not windowed.empty and len(windowed) == len(full)
    pd.testing.assert_frame_equal(windowed, full)
