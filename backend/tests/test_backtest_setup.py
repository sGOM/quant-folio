"""run_rebalance_backtest 준비 로직(순수 헬퍼) 단위 테스트.

이 세 갈래는 588줄짜리 백테스트 함수 안에 인라인으로 묻혀 있어서, 기본값 하나를
확인하려 해도 전체 백테스트를 돌려야만 했다. 헬퍼로 뽑은 뒤의 직접 검증이다.
"""
import logging

import numpy as np
import pandas as pd
import pytest

from app.services.backtest.portfolio import (
    PanicOverlayParams,
    _adv_frame,
    _parse_panic_overlay,
    _with_sector_map,
)


# ───────────────────── _parse_panic_overlay ─────────────────────


def test_패닉_파라미터_기본값():
    p = _parse_panic_overlay({})
    assert p == PanicOverlayParams(
        arm_rank=2, arm_window=5, hold_days=20,
        profit_reclaim_pct=0.5, knife_stop_pct=0.05,
        base_exposure=0.70, panic_exposure=1.00, scale_in_confirm=0.5,
        ma_recovery_period=20, event_only=False,
    )


def test_비율_0_은_기본값으로_되살아나지_않는다():
    """`po.get(k) or default` 로 쓰면 "0 으로 끄기"가 조용히 기본값이 된다.

    scale_in_confirm=0 은 "Confirm 시점엔 아무것도 채우지 않는다"는 유효 설정이다.
    """
    p = _parse_panic_overlay({
        "scale_in_confirm": 0.0, "profit_reclaim_pct": 0.0,
        "knife_stop_pct": 0.0, "base_exposure": 0.0, "panic_exposure": 0.0,
    })
    assert p.scale_in_confirm == 0.0
    assert p.profit_reclaim_pct == 0.0
    assert p.knife_stop_pct == 0.0
    assert p.base_exposure == 0.0
    assert p.panic_exposure == 0.0


@pytest.mark.parametrize(
    "level,rank", [("normal", 0), ("caution", 1), ("warning", 2), ("panic", 3)]
)
def test_arm_level_이_심각도_순위로_해석된다(level, rank):
    assert _parse_panic_overlay({"arm_level": level}).arm_rank == rank


def test_알_수_없는_arm_level_은_warning_으로_떨어진다():
    assert _parse_panic_overlay({"arm_level": "무엇"}).arm_rank == 2


def test_파라미터는_불변이다():
    with pytest.raises(Exception):
        _parse_panic_overlay({}).arm_window = 99  # type: ignore[misc]


# ───────────────────────── _adv_frame ─────────────────────────


def _panel() -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=30)
    return pd.DataFrame({"A": np.linspace(100, 130, 30), "B": np.linspace(200, 260, 30)}, index=idx)


def test_ADV_캡_미설정이면_프레임을_만들지_않는다():
    assert _adv_frame(0.0, _panel(), _panel()) is None


def test_거래량_패널이_없으면_경고만_남기고_캡을_포기한다(caplog):
    with caplog.at_level(logging.WARNING):
        assert _adv_frame(0.1, _panel(), None) is None
    assert "거래량 패널" in caplog.text


def test_거래대금은_거래량과_종가의_곱의_20일_평균이다():
    panel = _panel()
    volume = pd.DataFrame(1000.0, index=panel.index, columns=panel.columns)

    adv = _adv_frame(0.1, panel, volume)

    assert adv is not None
    assert list(adv.columns) == ["A", "B"]
    # min_periods=10 — 앞 9행은 결측, 20행째부터는 직전 20봉 평균.
    assert adv["A"].iloc[:9].isna().all()
    expected = (panel["A"].iloc[10:30] * 1000.0).mean()
    assert adv["A"].iloc[29] == pytest.approx(expected)


def test_거래량_패널의_결측_종목은_0_으로_채워진다():
    """패널에 있으나 거래량이 없는 종목은 ADV 0 — 캡이 사실상 매매를 막는다."""
    panel = _panel()
    volume = pd.DataFrame({"A": 1000.0}, index=panel.index)

    adv = _adv_frame(0.1, panel, volume)

    assert adv is not None
    assert (adv["B"].dropna() == 0.0).all()


# ─────────────────────── _with_sector_map ───────────────────────


def test_섹터_한도가_없으면_config_를_그대로_돌려준다():
    cfg = {"capital": 1}
    assert _with_sector_map(cfg, {}) is cfg
    assert _with_sector_map(cfg, {"max_position_pct": 0.2}) is cfg


def test_이미_매핑이_실려_있으면_다시_조회하지_않는다():
    cfg = {"_sector_map": {"005930": "전기전자"}}
    assert _with_sector_map(cfg, {"max_sector_pct": 0.3}) is cfg


def test_조회_실패는_ERROR_로_남기고_캡을_포기한다(monkeypatch, caplog):
    from app.services.data import krx_index
    from app.services.data.errors import SourceUnavailableError

    def _boom(as_of):
        raise SourceUnavailableError("krx", "업종 조회 장애")

    monkeypatch.setattr(krx_index, "sector_map", _boom)
    cfg = {"capital": 1}

    with caplog.at_level(logging.ERROR):
        out = _with_sector_map(cfg, {"max_sector_pct": 0.3})

    assert out is cfg
    assert "_sector_map" not in out
    assert "조회 실패" in caplog.text


def test_조회는_됐지만_매핑이_비면_WARNING_이고_ERROR_는_아니다(monkeypatch, caplog):
    """"조회가 실패했다"와 "매핑이 없었다"는 다른 사건이라 로그를 겹치지 않는다."""
    from app.services.data import krx_index

    monkeypatch.setattr(krx_index, "sector_map", lambda as_of: {})

    with caplog.at_level(logging.WARNING):
        out = _with_sector_map({"capital": 1}, {"max_sector_pct": 0.3})

    assert "_sector_map" not in out
    assert "미확보" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_매핑을_받으면_원본을_건드리지_않고_실어_돌려준다(monkeypatch):
    from app.services.data import krx_index

    monkeypatch.setattr(krx_index, "sector_map", lambda as_of: {"005930": "전기전자"})
    cfg = {"capital": 1}

    out = _with_sector_map(cfg, {"max_sector_pct": 0.3})

    assert out["_sector_map"] == {"005930": "전기전자"}
    assert "_sector_map" not in cfg  # 원본 불변
