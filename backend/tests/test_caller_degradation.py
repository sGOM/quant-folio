"""명시적 저하 경로 — 외부 장애는 삼키되 우리 쪽 버그는 삼키지 않는다(Task 7).

`except Exception` 이 외부 장애만이 아니라 우리 쪽 버그(TypeError 등)까지 삼키던 지점을
`except DataSourceError` 로 좁힌 6개 호출부(+ portfolio.py 신규)를 검증한다. 저하 자체는
유지하되(외부 장애 시 빈 값으로 계속 진행), 우리 쪽 버그는 반드시 전파돼야 한다 —
`except Exception` 시절엔 이 전파가 없어 `DID NOT RAISE` 로 실패했다.

실제 KRX 로그인·DART 호출·KOFIA 호출은 절대 하지 않는다 — 전부 monkeypatch 로 대역화한다.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.api.routes import backtests as bt_mod
from app.services import symbols
from app.services.backtest import portfolio
from app.services.data import ingest
from app.services.data import krx_index
from app.services.data.errors import SourceUnavailableError
from app.services.metrics import factors as factors_mod


# ───────────────────── symbols.py: 종목 카탈로그(all_listed_stocks) ─────────────────────


class TestSymbolCatalogDegradation:
    """종목명 카탈로그는 없어도 매매가 계속돼야 한다 — 저하는 유지, 은폐만 제거."""

    def test_외부_장애는_삼키고_계속_진행한다(self, monkeypatch):
        def _boom():
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(krx_index, "all_listed_stocks", _boom)
        catalog, external_ok = symbols._build_catalog()
        assert external_ok is False  # KRX 실패가 드러난다
        assert isinstance(catalog, list)  # 폴백 소스로 진행

    def test_우리_쪽_버그는_전파된다(self, monkeypatch):
        """except Exception 이던 시절엔 TypeError 까지 삼켜 은신처가 됐다."""

        def _bug():
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(krx_index, "all_listed_stocks", _bug)
        with pytest.raises(TypeError):
            symbols._build_catalog()


# ───────────────────── ingest.py: build_universe(index_members) ─────────────────────


class _FakeScalars:
    def __init__(self, values):
        self._values = values

    def __iter__(self):
        return iter(self._values)


class _FakeStrategyDb:
    """Strategy.config 목록만 흉내내는 최소 대역(tests/test_data_ingest.py 와 동일 패턴)."""

    def __init__(self, configs: list[dict]):
        self.configs = configs

    async def scalars(self, stmt):
        return _FakeScalars(self.configs)


class TestBuildUniverseDegradation:
    async def test_외부_장애는_삼키고_전략_유니버스로_계속_진행한다(self, monkeypatch):
        def _boom(as_of, index="KOSPI200"):
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(krx_index, "index_members", _boom)
        db = _FakeStrategyDb([{"type": "sma", "symbol": "005930"}])

        universe = await ingest.build_universe(db)

        assert universe == ["005930"]  # 지수 조회 실패해도 전략 유니버스는 반영

    async def test_우리_쪽_버그는_전파된다(self, monkeypatch):
        def _bug(as_of, index="KOSPI200"):
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(krx_index, "index_members", _bug)
        db = _FakeStrategyDb([])

        with pytest.raises(TypeError):
            await ingest.build_universe(db)


# ───────────────────── backtests.py: _fundamentals_provider_with_neutralize_cols ─────


class TestBacktestsNeutralizeDegradation:
    """market_caps(사이즈)·sector_map(섹터) 중립화 축 조회(정정 D — 로그 신규 추가)."""

    @pytest.fixture(autouse=True)
    def _stub_fundamentals(self, monkeypatch):
        import app.services.metrics as metrics_mod

        # _fundamentals_provider 내부 밸류 조회를 막아 순수 로직만 검증(OpenDART 는 테스트
        # 환경에서 is_enabled()=False 라 이미 네트워크를 타지 않는다).
        monkeypatch.setattr(metrics_mod, "_fetch_fundamentals", lambda ymd, mkts: pd.DataFrame())

    def test_market_caps_외부_장애는_삼키고_사이즈_중립화_없이_계속(self, monkeypatch, caplog):
        def _boom(as_of):
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(krx_index, "market_caps", _boom)
        with caplog.at_level("ERROR"):
            out = bt_mod._fundamentals_provider_with_neutralize_cols(
                date(2024, 1, 2), ["005930"], neutralize="size"
            )
        assert out is None or "market_cap" not in out.columns
        assert "사이즈 중립화" in caplog.text

    def test_market_caps_우리_쪽_버그는_전파된다(self, monkeypatch):
        def _bug(as_of):
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(krx_index, "market_caps", _bug)
        with pytest.raises(TypeError):
            bt_mod._fundamentals_provider_with_neutralize_cols(
                date(2024, 1, 2), ["005930"], neutralize="size"
            )

    def test_sector_map_외부_장애는_삼키고_섹터_중립화_없이_계속(self, monkeypatch, caplog):
        def _boom(as_of):
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(krx_index, "sector_map", _boom)
        with caplog.at_level("ERROR"):
            out = bt_mod._fundamentals_provider_with_neutralize_cols(
                date(2024, 1, 2), ["005930"], neutralize="sector"
            )
        assert out is None or "sector" not in out.columns
        assert "섹터 중립화" in caplog.text

    def test_sector_map_우리_쪽_버그는_전파된다(self, monkeypatch):
        def _bug(as_of):
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(krx_index, "sector_map", _bug)
        with pytest.raises(TypeError):
            bt_mod._fundamentals_provider_with_neutralize_cols(
                date(2024, 1, 2), ["005930"], neutralize="sector"
            )


# ───────────────────── factors.py: compute_universe_scores 중립화 축 ─────────────────────


def _minimal_universe_scores_stubs(monkeypatch):
    """momentum/flow/resid_mom/pead 전량실패 방어에 안 걸리도록 최소 정상 응답을 준다."""
    monkeypatch.setattr(factors_mod, "_fetch_fundamentals", lambda ymd, mkts: pd.DataFrame())
    monkeypatch.setattr(
        factors_mod, "_fetch_price_change",
        lambda s, e, m: pd.DataFrame({"등락률": {"005930": 1.0}}),
    )
    monkeypatch.setattr(
        "app.services.metrics.stocks._compute_tech_indicators",
        lambda code, hs, ae: {},
    )


class TestFactorsNeutralizeDegradation:
    """compute_universe_scores 의 market_caps(사이즈)·sector_map(섹터) 중립화 축 조회."""

    def test_market_caps_외부_장애는_삼키고_계속_진행한다(self, monkeypatch, caplog):
        _minimal_universe_scores_stubs(monkeypatch)

        def _boom(as_of):
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(krx_index, "market_caps", _boom)
        with caplog.at_level("ERROR"):
            out = factors_mod.compute_universe_scores(
                ["005930"], date(2024, 4, 1),
                factor_weights={"momentum": 0.0, "value": 0.0, "lowvol": 0.0,
                                "quality": 0.0, "growth": 0.0, "flow": 0.0},
                neutralize="size",
            )
        assert isinstance(out, dict)  # 예외 없이 완료
        assert "사이즈 중립화" in caplog.text

    def test_market_caps_우리_쪽_버그는_전파된다(self, monkeypatch):
        _minimal_universe_scores_stubs(monkeypatch)

        def _bug(as_of):
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(krx_index, "market_caps", _bug)
        with pytest.raises(TypeError):
            factors_mod.compute_universe_scores(
                ["005930"], date(2024, 4, 1),
                factor_weights={"momentum": 0.0, "value": 0.0, "lowvol": 0.0,
                                "quality": 0.0, "growth": 0.0, "flow": 0.0},
                neutralize="size",
            )

    def test_sector_map_외부_장애는_삼키고_계속_진행한다(self, monkeypatch, caplog):
        _minimal_universe_scores_stubs(monkeypatch)

        def _boom(as_of):
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(krx_index, "sector_map", _boom)
        with caplog.at_level("ERROR"):
            out = factors_mod.compute_universe_scores(
                ["005930"], date(2024, 4, 1),
                factor_weights={"momentum": 0.0, "value": 0.0, "lowvol": 0.0,
                                "quality": 0.0, "growth": 0.0, "flow": 0.0},
                neutralize="sector",
            )
        assert isinstance(out, dict)
        assert "섹터 중립화" in caplog.text

    def test_sector_map_우리_쪽_버그는_전파된다(self, monkeypatch):
        _minimal_universe_scores_stubs(monkeypatch)

        def _bug(as_of):
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(krx_index, "sector_map", _bug)
        with pytest.raises(TypeError):
            factors_mod.compute_universe_scores(
                ["005930"], date(2024, 4, 1),
                factor_weights={"momentum": 0.0, "value": 0.0, "lowvol": 0.0,
                                "quality": 0.0, "growth": 0.0, "flow": 0.0},
                neutralize="sector",
            )


# ───────────────────── portfolio.py: run_rebalance_backtest 의 섹터 캡 매핑(정정 A) ─────


def _sector_risk_cfg() -> dict:
    return {
        "universe": ["A", "B"],
        "selection": {"method": "all"},
        "cadence": "monthly",
        "rebalance_dom": 1,
        "capital": 1_000_000,
        "drift_band_pct": 0.0,
        "fill_mode": "same_close",
        "risk_layer": {"max_sector_pct": 0.5},
    }


class TestPortfolioSectorMapDegradation:
    """정정 A — 원 계획에는 없던 try/except 신규 추가. `max_sector_pct` 설정 시 sector_map
    조회가 실패해도 섹터 캡만 미적용된 채 백테스트는 계속돼야 한다(기존 else 저하와 동일 결과)."""

    def test_외부_장애는_삼키고_섹터_캡_없이_계속_진행한다(self, monkeypatch, caplog):
        dates = pd.bdate_range("2024-01-01", periods=30)
        panel = pd.DataFrame({"A": [100.0] * 30, "B": [100.0] * 30}, index=dates)

        def _boom(as_of):
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(krx_index, "sector_map", _boom)
        with caplog.at_level("ERROR"):
            res = portfolio.run_rebalance_backtest(panel, _sector_risk_cfg(), dates[0], dates[-1])
        assert res["num_rebalances"] >= 1  # 섹터 캡 없이도 백테스트는 정상 완료
        assert "섹터 집중 한도" in caplog.text

    def test_우리_쪽_버그는_전파된다(self, monkeypatch):
        dates = pd.bdate_range("2024-01-01", periods=30)
        panel = pd.DataFrame({"A": [100.0] * 30, "B": [100.0] * 30}, index=dates)

        def _bug(as_of):
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(krx_index, "sector_map", _bug)
        with pytest.raises(TypeError):
            portfolio.run_rebalance_backtest(panel, _sector_risk_cfg(), dates[0], dates[-1])
