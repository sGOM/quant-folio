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
from app.services.data import opendart
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


# ───── factors.py: 퀄리티 팩터(opendart.metrics_by_symbol) — Task 7 리뷰 H-2 ─────


class TestFactorsQualityDegradation:
    """`compute_universe_scores` 의 퀄리티 팩터 조회.

    같은 함수 안 6줄 아래 중립화 블록은 좁혀졌는데 이 블록만 bare `except Exception` 으로
    남아 있었다(Task 7 리뷰 H-2). 라이브 엔진이 매 리밸런싱마다 부르는 경로라, 여기만
    조용하면 우리 쪽 버그가 '중립 처리'로 위장된 채 리밸런싱이 '성공'으로 기록된다.
    """

    def test_외부_장애는_삼키고_중립_처리로_계속한다(self, monkeypatch, caplog):
        _minimal_universe_scores_stubs(monkeypatch)

        def _boom(codes, as_of, use_ttm=False):
            raise SourceUnavailableError("dart", "연결 실패")

        monkeypatch.setattr(opendart, "metrics_by_symbol", _boom)
        with caplog.at_level("ERROR"):
            out = factors_mod.compute_universe_scores(
                ["005930"], date(2024, 4, 1),
                factor_weights={"momentum": 0.0, "value": 0.0, "lowvol": 0.0,
                                "quality": 0.0, "growth": 0.0, "flow": 0.0},
            )
        assert isinstance(out, dict)  # 예외 없이 완료
        assert "퀄리티 팩터" in caplog.text

    def test_우리_쪽_버그는_전파된다(self, monkeypatch):
        _minimal_universe_scores_stubs(monkeypatch)

        def _bug(codes, as_of, use_ttm=False):
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(opendart, "metrics_by_symbol", _bug)
        with pytest.raises(TypeError):
            factors_mod.compute_universe_scores(
                ["005930"], date(2024, 4, 1),
                factor_weights={"momentum": 0.0, "value": 0.0, "lowvol": 0.0,
                                "quality": 0.0, "growth": 0.0, "flow": 0.0},
            )


# ───── engine/rebalance_runner.py: 라이브 섹터맵 — Task 7 리뷰 H-1 ─────


class TestLiveSectorMapDegradation:
    """`_get_sector_map` 의 저하 계약(라이브 매매 엔진).

    docstring 이 "미확보(빈 dict) 시 섹터 캡은 조용히 미적용" 이라고 계약을 적어뒀는데,
    Task 3 에서 `sector_map()` 이 빈 dict 대신 raise 로 바뀌면서 그 계약이 깨져 있었다.
    백테스트(portfolio.py)는 정정 A 로 고쳤지만 **라이브 거울상은 그대로였다** — 같은
    리스크 레이어 기능인데 백테스트는 우아하게 저하되고 라이브는 리밸런싱 자체가 무산된다.
    """

    @staticmethod
    def _runner():
        from engine.rebalance_runner import RebalanceRunner

        from tests.conftest import FakeRedis

        return RebalanceRunner(strategy_id=1, redis=FakeRedis())

    async def test_외부_장애는_삼키고_섹터_캡_없이_계속_진행한다(self, monkeypatch, caplog):
        def _boom(as_of=None):
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(krx_index, "sector_map", _boom)
        with caplog.at_level("ERROR"):
            smap = await self._runner()._get_sector_map()

        assert smap == {}  # 캡 미적용으로 저하 — 리밸런싱은 계속된다
        assert "업종 매핑" in caplog.text

    async def test_우리_쪽_버그는_전파된다(self, monkeypatch):
        def _bug(as_of=None):
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(krx_index, "sector_map", _bug)
        with pytest.raises(TypeError):
            await self._runner()._get_sector_map()


# ───── screener.py: 저하 사실이 응답에 드러나는가 — Task 7 리뷰 M-1 ─────


class TestScreenerDegradationIsVisibleInResponse:
    """하드 필터를 건너뛴 결과와 정상 결과가 응답에서 구분돼야 한다.

    로그에만 남기면 사용자는 `opendart_enabled=True` 인 정상 응답을 받으면서 걸러졌어야
    할 종목이 섞인 목록을 본다 — 이 작업이 없애려는 §44-1 의 형태 그대로다.
    """

    @staticmethod
    def _stub_market(monkeypatch):
        """screen_turnaround 가 후보 1종목을 만들도록 하는 최소 대역(전부 목 — 실 조회 없음)."""
        from app.services import screener as sc

        monkeypatch.setattr(
            sc, "_fetch_market_cap",
            lambda ymd, mkts: pd.DataFrame({"시가총액": {"005930": 5.0e10}}),
        )

        # 5일창은 일평균 10억, 60일창은 일평균 5억 → 유동성(≥3억)·급증(2.0배≥1.5) 통과.
        calls = {"n": 0}

        def _fake_price_change(start, end, mkts):
            calls["n"] += 1
            total = 5 * 1.0e9 if calls["n"] == 1 else 60 * 5.0e8
            return pd.DataFrame({"등락률": {"005930": 1.0}, "거래대금": {"005930": total}})

        monkeypatch.setattr(sc, "_fetch_price_change", _fake_price_change)
        monkeypatch.setattr(sc, "_fetch_fundamentals", lambda ymd, mkts: pd.DataFrame())
        # 종목명 해석은 이 계약과 무관한데 실제 KRX/FDR 조회를 탄다 — 반드시 대역화한다.
        monkeypatch.setattr(sc, "_build_name_map", lambda: {"005930": "삼성전자"})
        monkeypatch.setattr(sc, "_build_krx_name_map", lambda a, b: {})
        return sc

    def test_조회_실패시_필터_미적용이_응답에_드러난다(self, monkeypatch):
        sc = self._stub_market(monkeypatch)

        def _boom(codes, as_of):
            raise SourceUnavailableError("dart", "연결 실패")

        monkeypatch.setattr(sc.opendart, "metrics_by_symbol", _boom)
        # as_of 를 명시한다 — 생략하면 _last_business_day() 가 pykrx 지수 조회(실 네트워크)를 탄다.
        out = sc.screen_turnaround(as_of=date(2026, 7, 1), market="ALL")

        assert out.scanned == 1  # 후보가 실제로 생겼는지(대역이 무력하지 않은지) 확인
        assert out.financial_filter_applied is False

    def test_정상_조회면_필터_적용으로_표시된다(self, monkeypatch):
        sc = self._stub_market(monkeypatch)
        monkeypatch.setattr(sc.opendart, "metrics_by_symbol", lambda codes, as_of: {})
        # as_of 를 명시한다 — 생략하면 _last_business_day() 가 pykrx 지수 조회(실 네트워크)를 탄다.
        out = sc.screen_turnaround(as_of=date(2026, 7, 1), market="ALL")

        assert out.scanned == 1
        assert out.financial_filter_applied is True


# ───── panic.py/sectors.py/backtests.py/factors.py: 로컬 저장소 도입 이월 항목(Task 11) ─────


def test_업종_하나가_막혀도_로테이션은_계속한다(monkeypatch):
    """개별 업종 실패로 전체 섹터 로테이션이 죽으면 안 된다."""
    from app.services.metrics import sectors

    monkeypatch.setattr(
        sectors, "_fetch_index_ohlcv",
        lambda *a, **kw: (_ for _ in ()).throw(SourceUnavailableError("krx", "일시장애")),
    )
    got = sectors._compute_one_sector(
        "1001", "KOSPI", "20260806", "20260101", ref_return=0.01
    )
    assert got is None


class TestProviderWithFlowDegradation:
    """이월 항목 (1): `_provider_with_flow` 가 flow 조회 실패를 flow=None 으로 삼키지
    않고 그대로 전파하는지(예전 `except Exception: flow = None` 은 §47 사고 패턴을
    이 진입 경로에 재현했다)."""

    def test_flow_조회_실패는_전파된다(self, monkeypatch):
        import app.services.metrics as metrics_mod

        def _boom(codes, as_of, window, denom):
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(metrics_mod, "compute_flow_norm", _boom)
        provider = bt_mod._provider_with_flow(None, window=90, denom="mcap")

        with pytest.raises(SourceUnavailableError):
            provider(date(2024, 4, 1), ["005930"])


class TestComputeFlowNormDenomShortCircuit:
    """이월 항목 (2): 순매수 조회 결과가 비면 분모(시총/거래대금) 조회를 아예 하지
    않는지 — 순매수가 없으면 분모는 어차피 쓰이지 않는데 조회하던 낭비를 없앴다."""

    def test_순매수가_비면_분모_조회를_하지_않는다(self, monkeypatch):
        calls = {"market_cap": 0, "price_change": 0}

        monkeypatch.setattr(
            factors_mod, "_fetch_net_purchases",
            lambda s, e, m: pd.DataFrame(columns=["net_buy_value"]),
        )

        def _mcap_spy(*a, **kw):
            calls["market_cap"] += 1
            return pd.DataFrame()

        def _pc_spy(*a, **kw):
            calls["price_change"] += 1
            return pd.DataFrame()

        monkeypatch.setattr(factors_mod, "_fetch_market_cap", _mcap_spy)
        monkeypatch.setattr(factors_mod, "_fetch_price_change", _pc_spy)

        out = factors_mod.compute_flow_norm(["005930"], date(2024, 4, 1))

        assert out.empty
        assert calls["market_cap"] == 0
        assert calls["price_change"] == 0
