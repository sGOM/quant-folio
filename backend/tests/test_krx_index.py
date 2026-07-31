"""KRX 지수 구성종목 클라이언트 — 파싱·휴장일 스냅·캐시 검증(네트워크 목).

실제 KRX 호출(인증 필요) 대신 세션을 목으로 주입해 순수 로직만 검증한다.
"""
from datetime import date

import pytest

from app.services.data import krx_index


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """trdDd 별 응답을 미리 정해두는 목 세션. 없는 날짜는 빈 output."""

    def __init__(self, by_date):
        self.by_date = by_date
        self.calls = []

    def post(self, url, data=None, timeout=None):
        dd = data["trdDd"]
        self.calls.append(dd)
        rows = self.by_date.get(dd, [])
        return _FakeResp({"output": rows})


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    krx_index._MEMBERS_CACHE.clear()
    krx_index._STOCKS_CACHE = None
    krx_index._MKTCAP_CACHE.clear()
    krx_index._SECTOR_CACHE = None
    # 업종분류 PIT 스냅샷 조회(DB)는 순수 로직 테스트(네트워크·DB 목)와 무관하므로 기본은
    # "스냅샷 없음"(빈 dict)으로 목 처리해 실제 DB 접속을 타지 않게 한다. 스냅샷 동작
    # 자체를 검증하는 테스트는 개별적으로 이 목을 덮어쓴다.
    monkeypatch.setattr(krx_index, "_lookup_pit_snapshot_sync", lambda as_of: {})
    yield
    krx_index._MEMBERS_CACHE.clear()
    krx_index._STOCKS_CACHE = None
    krx_index._MKTCAP_CACHE.clear()
    krx_index._SECTOR_CACHE = None


def _rows(*codes):
    return [{"ISU_SRT_CD": c, "ISU_ABBRV": f"종목{c}"} for c in codes]


def test_index_members_parses_and_zfills(monkeypatch):
    fake = _FakeSession({"20250630": _rows("5930", "000660", "207940")})
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    out = krx_index.index_members(date(2025, 6, 30), "KOSPI200")
    assert out == ["005930", "000660", "207940"]  # 6자리 zfill


def test_index_members_snaps_back_over_holiday(monkeypatch):
    # 2025-06-01(일)·05-31(토) 휴장 → 빈 응답, 05-30(금) 에 구성 존재.
    fake = _FakeSession({"20250530": _rows("005930", "000660")})
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    out = krx_index.index_members(date(2025, 6, 1), "KOSPI200")
    assert out == ["005930", "000660"]
    assert fake.calls[:3] == ["20250601", "20250531", "20250530"]  # 하루씩 소급


def test_index_members_empty_when_unauthenticated(monkeypatch):
    monkeypatch.setattr(krx_index, "_session", lambda: None)
    assert krx_index.index_members(date(2025, 6, 30)) == []


def test_index_members_cached(monkeypatch):
    fake = _FakeSession({"20250630": _rows("005930")})
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    krx_index.index_members(date(2025, 6, 30))
    n_first = len(fake.calls)
    krx_index.index_members(date(2025, 6, 30))  # 캐시 히트 → 추가 호출 없음
    assert len(fake.calls) == n_first


def test_unknown_index_raises(monkeypatch):
    monkeypatch.setattr(krx_index, "_session", lambda: _FakeSession({}))
    with pytest.raises(ValueError):
        krx_index.index_members(date(2025, 6, 30), "NASDAQ")


# ───────────────────── 전 상장종목 마스터(all_listed_stocks) ─────────────────────


class _FakeFinderSession:
    """finder_stkisu 응답(block1)을 돌려주는 목 세션."""

    def __init__(self, rows, *, raise_exc=False):
        self.rows = rows
        self.raise_exc = raise_exc
        self.calls = 0

    def post(self, url, data=None, timeout=None):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("네트워크 오류")
        return _FakeResp({"block1": self.rows})


def _finder_rows(*items):
    # items: (short_code, codeName, marketCode)
    return [
        {"short_code": c, "codeName": n, "marketCode": mc}
        for c, n, mc in items
    ]


def test_all_listed_stocks_parses_and_maps_market(monkeypatch):
    fake = _FakeFinderSession(_finder_rows(
        ("005930", "삼성전자", "STK"), ("060310", "3S", "KSQ"), ("000000", "", "STK"),
    ))
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    out = krx_index.all_listed_stocks()
    assert {"code": "005930", "name": "삼성전자", "market": "KOSPI"} in out
    assert {"code": "060310", "name": "3S", "market": "KOSDAQ"} in out
    # 이름 없는 행은 제외.
    assert all(s["code"] != "000000" for s in out)


def test_all_listed_stocks_caches_on_success(monkeypatch):
    fake = _FakeFinderSession(_finder_rows(("005930", "삼성전자", "STK")))
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    krx_index.all_listed_stocks()
    krx_index.all_listed_stocks()  # 두 번째는 캐시 히트 → post 재호출 없음
    assert fake.calls == 1


def test_all_listed_stocks_unauthenticated_returns_empty(monkeypatch):
    monkeypatch.setattr(krx_index, "_session", lambda: None)
    assert krx_index.all_listed_stocks() == []
    assert krx_index._STOCKS_CACHE is None  # 실패는 캐시하지 않음(자가복구)


def test_all_listed_stocks_failure_not_cached(monkeypatch):
    fake = _FakeFinderSession([], raise_exc=True)
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    assert krx_index.all_listed_stocks() == []
    assert krx_index._STOCKS_CACHE is None  # 예외도 캐시 안 함


# ───────────────────── 라우트 PIT 풀 빌더(_build_pit_pool) ─────────────────────


def test_build_pit_pool_fixed_source_returns_none():
    from app.api.routes.backtests import _build_pit_pool
    cfg = {"universe": ["005930"], "selection": {"universe_rule": {"source": "fixed"}}}
    assert _build_pit_pool(cfg, date(2023, 1, 1), date(2023, 3, 31)) == (None, None)

    # universe_rule 없음도 fixed 취급
    assert _build_pit_pool({"selection": {}}, date(2023, 1, 1), date(2023, 3, 31)) == (None, None)


def test_build_pit_pool_index_source_union_and_provider(monkeypatch):
    import pandas as pd
    from app.api.routes import backtests as bt

    # 월별 멤버십을 다르게 주고 합집합·시점별 공급을 검증.
    per_month = {
        (2023, 1): ["005930", "000660"],
        (2023, 2): ["005930", "035420"],   # 000660 편출, 035420 편입
        (2023, 3): ["005930", "035420"],
    }
    import app.services.data.krx_index as ki
    monkeypatch.setattr(
        ki, "index_members",
        lambda as_of, index="KOSPI200": per_month.get((as_of.year, as_of.month), []),
    )

    cfg = {"universe": [], "selection": {"universe_rule": {"source": "KOSPI200"}}}
    union, provider = bt._build_pit_pool(cfg, date(2023, 1, 1), date(2023, 3, 31))
    assert set(union) == {"005930", "000660", "035420"}   # 편출·편입 모두 포함
    assert provider(pd.Timestamp("2023-01-15")) == ["005930", "000660"]
    assert provider(pd.Timestamp("2023-02-15")) == ["005930", "035420"]


# ───────────────────── 시가총액(market_caps) · 유동성 필터 ─────────────────────


class _FakeCapSession:
    """MDCSTAT01501(시가총액) 응답(OutBlock_1)을 mkt별로 돌려주는 목 세션."""

    def __init__(self, by_market):
        self.by_market = by_market  # {"STK": [(code, mktcap_won), ...], "KSQ": [...]}

    def post(self, url, data=None, timeout=None):
        mkt = data["mktId"]
        rows = [
            {"ISU_SRT_CD": c, "MKTCAP": f"{v:,}"}
            for c, v in self.by_market.get(mkt, [])
        ]
        return _FakeResp({"OutBlock_1": rows})


def test_market_caps_parses_and_merges_markets(monkeypatch):
    fake = _FakeCapSession({
        "STK": [("005930", 350_000_000_000_000), ("000660", 200_000_000_000_000)],
        "KSQ": [("247540", 20_000_000_000_000)],
    })
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    caps = krx_index.market_caps(date(2025, 6, 30))
    assert caps["005930"] == 350_000_000_000_000
    assert caps["247540"] == 20_000_000_000_000  # 콤마 파싱 + KOSPI/KOSDAQ 병합


def test_market_caps_unauthenticated_empty(monkeypatch):
    monkeypatch.setattr(krx_index, "_session", lambda: None)
    assert krx_index.market_caps(date(2025, 6, 30)) == {}


def test_build_pit_pool_applies_min_market_cap(monkeypatch):
    from app.api.routes import backtests as bt
    import app.services.data.krx_index as ki

    monkeypatch.setattr(ki, "index_members", lambda as_of, index="KOSPI200": ["005930", "000660", "007340"])
    # 007340 만 5000억 미만 → 필터로 제외되어야 한다.
    caps = {"005930": 350_000_000_000_000, "000660": 200_000_000_000_000, "007340": 100_000_000_000}
    monkeypatch.setattr(ki, "market_caps", lambda as_of: caps)

    cfg = {"universe": [], "selection": {"universe_rule": {"source": "KOSPI200", "min_market_cap": 5000}}}
    union, provider = bt._build_pit_pool(cfg, date(2023, 1, 1), date(2023, 1, 31))
    assert set(union) == {"005930", "000660"}  # 007340(1000억) 제외


def test_build_pit_pool_min_cap_keeps_all_when_cap_lookup_fails(monkeypatch):
    """시총 조회 실패 시 필터를 적용하지 않고 원본 멤버십을 유지(과도한 축소 방지)."""
    from app.api.routes import backtests as bt
    import app.services.data.krx_index as ki

    monkeypatch.setattr(ki, "index_members", lambda as_of, index="KOSPI200": ["005930", "000660"])
    monkeypatch.setattr(ki, "market_caps", lambda as_of: {})  # 조회 실패

    cfg = {"universe": [], "selection": {"universe_rule": {"source": "KOSPI200", "min_market_cap": 5000}}}
    union, _ = bt._build_pit_pool(cfg, date(2023, 1, 1), date(2023, 1, 31))
    assert set(union) == {"005930", "000660"}


# ───────────────────── 업종분류(sector_map) · 섹터 한도 ─────────────────────


class _FakeSectorSession:
    """MDCSTAT03901(업종분류) 응답(block1)을 mkt별로 돌려주는 목 세션."""

    def __init__(self, by_market, *, raise_exc=False):
        self.by_market = by_market  # {"STK": [(code, 업종명), ...], "KSQ": [...]}
        self.raise_exc = raise_exc
        self.calls = 0

    def post(self, url, data=None, timeout=None):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("네트워크 오류")
        mkt = data["mktId"]
        rows = [
            {"ISU_SRT_CD": c, "IDX_IND_NM": ind}
            for c, ind in self.by_market.get(mkt, [])
        ]
        return _FakeResp({"block1": rows})


def test_sector_map_parses_and_merges_markets(monkeypatch):
    fake = _FakeSectorSession({
        "STK": [("005930", "전기전자"), ("000660", "전기전자"), ("005380", "운수장비")],
        "KSQ": [("247540", "전기전자")],
    })
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    smap = krx_index.sector_map(date(2025, 6, 30))
    assert smap["005930"] == "전기전자"
    assert smap["005380"] == "운수장비"
    assert smap["247540"] == "전기전자"  # KOSDAQ 병합 + 6자리


def test_sector_map_snaps_back_over_holiday(monkeypatch):
    calls = []

    class _S:
        def post(self, url, data=None, timeout=None):
            calls.append(data["trdDd"])
            # 05-31·06-01 휴장(빈 응답), 05-30 에만 자료 존재.
            if data["trdDd"] == "20250530" and data["mktId"] == "STK":
                return _FakeResp({"block1": [{"ISU_SRT_CD": "005930", "IDX_IND_NM": "전기전자"}]})
            return _FakeResp({"block1": []})

    monkeypatch.setattr(krx_index, "_session", lambda: _S())
    smap = krx_index.sector_map(date(2025, 6, 1))
    assert smap == {"005930": "전기전자"}
    assert calls[:2] == ["20250601", "20250601"]  # STK·KSQ 각 1회씩 소급


def test_sector_map_unauthenticated_empty(monkeypatch):
    monkeypatch.setattr(krx_index, "_session", lambda: None)
    assert krx_index.sector_map(date(2025, 6, 30)) == {}


def test_sector_map_caches_on_success(monkeypatch):
    fake = _FakeSectorSession({"STK": [("005930", "전기전자")]})
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    krx_index.sector_map(date(2025, 6, 30))
    n = fake.calls
    krx_index.sector_map(date(2025, 6, 30))  # 캐시 히트 → 추가 호출 없음
    assert fake.calls == n


def test_sector_map_failure_not_cached(monkeypatch):
    fake = _FakeSectorSession({}, raise_exc=True)
    monkeypatch.setattr(krx_index, "_session", lambda: fake)
    assert krx_index.sector_map(date(2025, 6, 30)) == {}
    assert krx_index._SECTOR_CACHE is None  # 실패는 캐시 안 함(자가복구)


# ───────────────────── 업종분류 PIT 스냅샷(sector_map_snapshots, C-2) ─────────────────────


def test_sector_map_uses_pit_snapshot_when_available(monkeypatch):
    """as_of 에 스냅샷이 있으면 KRX 조회 없이 스냅샷 매핑을 그대로 반환한다."""
    calls = {"n": 0}

    def _fake_snapshot(as_of):
        calls["n"] += 1
        return {"005930": "전기전자(스냅샷)"}

    monkeypatch.setattr(krx_index, "_lookup_pit_snapshot_sync", _fake_snapshot)
    # _session 이 호출되면 실패하도록 해 "스냅샷만으로 응답했는지"를 검증한다.
    monkeypatch.setattr(
        krx_index, "_session", lambda: (_ for _ in ()).throw(AssertionError("KRX 조회 금지"))
    )

    smap = krx_index.sector_map(as_of=date(2026, 7, 1))
    assert smap == {"005930": "전기전자(스냅샷)"}
    assert calls["n"] == 1


def test_sector_map_falls_back_to_krx_when_no_snapshot(monkeypatch):
    """스냅샷이 없으면(빈 dict) 기존처럼 KRX MDC 직접 조회로 폴백한다."""
    monkeypatch.setattr(krx_index, "_lookup_pit_snapshot_sync", lambda as_of: {})
    fake = _FakeSectorSession({"STK": [("005930", "전기전자")]})
    monkeypatch.setattr(krx_index, "_session", lambda: fake)

    smap = krx_index.sector_map(as_of=date(2020, 1, 1))
    assert smap == {"005930": "전기전자"}
    assert fake.calls > 0  # 실제로 KRX 조회 경로를 탔다


def test_sector_map_without_as_of_skips_snapshot_lookup(monkeypatch):
    """as_of=None(기존 호출 방식)이면 스냅샷 조회 자체를 건너뛴다(과거 동작 그대로)."""
    def _should_not_be_called(as_of):
        raise AssertionError("as_of=None 이면 스냅샷 조회가 없어야 한다")

    monkeypatch.setattr(krx_index, "_lookup_pit_snapshot_sync", _should_not_be_called)
    fake = _FakeSectorSession({"STK": [("005930", "전기전자")]})
    monkeypatch.setattr(krx_index, "_session", lambda: fake)

    smap = krx_index.sector_map()
    assert smap == {"005930": "전기전자"}


class _FakeAsyncScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeSnapshotDB:
    """SectorMapSnapshot 대상 scalar/execute/add_all/flush 만 지원하는 최소 대역."""

    def __init__(self, *, existing_count: int = 0, latest_date=None, rows=None):
        self._existing_count = existing_count
        self._latest_date = latest_date
        self._rows = rows or []
        self.added: list = []
        self.deleted_dates: list = []
        self.flushed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalar(self, stmt):
        # snapshot_sector_map 은 count(*) 쿼리 1회만 scalar() 로 던진다(멱등성 스킵 판단용).
        return self._existing_count

    async def execute(self, stmt):
        text = str(stmt).lower()
        if text.startswith("delete"):
            self.deleted_dates.append("deleted")
            return None
        return _FakeAsyncScalarResult(self._rows)

    def add_all(self, objs):
        self.added.extend(objs)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        return None

    async def rollback(self):
        return None


def test_snapshot_sector_map_persists_current_mapping(monkeypatch):
    monkeypatch.setattr(krx_index, "sector_map", lambda: {"005930": "전기전자", "005380": "운수장비"})
    db = _FakeSnapshotDB(existing_count=0)

    import asyncio as _asyncio
    n = _asyncio.run(krx_index.snapshot_sector_map(db, as_of=date(2026, 7, 1)))

    assert n == 2
    assert {o.symbol for o in db.added} == {"005930", "005380"}
    assert all(o.snapshot_date == date(2026, 7, 1) for o in db.added)
    assert db.flushed


def test_snapshot_sector_map_skips_when_already_exists(monkeypatch):
    monkeypatch.setattr(krx_index, "sector_map", lambda: {"005930": "전기전자"})
    db = _FakeSnapshotDB(existing_count=1)

    import asyncio as _asyncio
    n = _asyncio.run(krx_index.snapshot_sector_map(db, as_of=date(2026, 7, 1)))

    assert n == 0
    assert db.added == []


def test_snapshot_sector_map_force_overwrites(monkeypatch):
    monkeypatch.setattr(krx_index, "sector_map", lambda: {"005930": "전기전자"})
    db = _FakeSnapshotDB(existing_count=1)

    import asyncio as _asyncio
    n = _asyncio.run(
        krx_index.snapshot_sector_map(db, as_of=date(2026, 7, 1), force=True)
    )

    assert n == 1
    assert db.deleted_dates  # 기존 스냅샷 삭제 후 재적재
    assert len(db.added) == 1


def test_snapshot_sector_map_returns_zero_when_krx_unavailable(monkeypatch):
    monkeypatch.setattr(krx_index, "sector_map", lambda: {})
    db = _FakeSnapshotDB(existing_count=0)

    import asyncio as _asyncio
    n = _asyncio.run(krx_index.snapshot_sector_map(db, as_of=date(2026, 7, 1)))

    assert n == 0
    assert db.added == []


# ───────────────────── 팩터 커버리지 경고(_factor_coverage_warnings) ─────────────────────


def test_factor_warnings_when_opendart_coverage_low(monkeypatch):
    from app.api.routes import backtests as bt
    import app.services.data.opendart as od

    # quality 가중치>0, 표본 5종목 중 커버리지 0 → 경고.
    monkeypatch.setattr(od, "metrics_by_symbol", lambda codes, as_of: {})
    cfg = {"selection": {"factor_weights": {"quality": 0.3, "value": 0.7}}}
    warns = bt._factor_coverage_warnings(cfg, ["005930", "000660", "035420", "005380", "000270"], date(2025, 6, 30))
    assert warns and "quality" in warns[0]


def test_no_factor_warnings_when_coverage_ok(monkeypatch):
    from app.api.routes import backtests as bt
    import app.services.data.opendart as od

    monkeypatch.setattr(od, "metrics_by_symbol", lambda codes, as_of: {c: {"roe": 0.1} for c in codes})
    cfg = {"selection": {"factor_weights": {"quality": 0.3, "value": 0.7}}}
    assert bt._factor_coverage_warnings(cfg, ["005930", "000660"], date(2025, 6, 30)) == []


def test_no_factor_warnings_when_no_quality_growth_weight(monkeypatch):
    from app.api.routes import backtests as bt
    # quality/growth 가중치 0 → OpenDART 조회 자체를 안 함(경고 없음).
    cfg = {"selection": {"factor_weights": {"value": 0.5, "momentum": 0.5}}}
    assert bt._factor_coverage_warnings(cfg, ["005930"], date(2025, 6, 30)) == []


# ─────────────────── 레버리지 ETF 잔고(취약성 관측) ───────────────────

_ETF_ROWS = [
    {"ISU_SRT_CD": "122630", "ISU_ABBRV": "KODEX 레버리지", "MKTCAP": "6,000,000,000,000"},
    {"ISU_SRT_CD": "0193T0", "ISU_ABBRV": "KODEX SK하이닉스단일종목레버리지",
     "MKTCAP": "3,000,000,000,000"},
    {"ISU_SRT_CD": "252670", "ISU_ABBRV": "KODEX 200선물인버스2X",
     "MKTCAP": "1,000,000,000,000"},
    {"ISU_SRT_CD": "069500", "ISU_ABBRV": "KODEX 200", "MKTCAP": "10,000,000,000,000"},
]


class TestEtfLeverageExposure:
    def test_레버리지와_단일종목_레버리지를_분리_집계한다(self, monkeypatch):
        monkeypatch.setattr(krx_index, "_session", lambda: _FakeSession({"20260731": _ETF_ROWS}))
        monkeypatch.setattr(krx_index, "is_business_day", lambda d: True)
        r = krx_index.etf_leverage_exposure(date(2026, 7, 31))
        assert r["total_mktcap"] == pytest.approx(20e12)
        # 레버리지 = KODEX 레버리지 + 단일종목레버리지 (인버스2X 는 제외)
        assert r["leveraged_mktcap"] == pytest.approx(9e12)
        assert r["leveraged_count"] == 2
        assert r["single_stock_mktcap"] == pytest.approx(3e12)
        assert r["single_stock_count"] == 1
        assert r["leveraged_ratio"] == pytest.approx(0.45)
        assert r["single_stock_ratio"] == pytest.approx(0.15)

    def test_인버스는_레버리지_집계에서_제외한다(self, monkeypatch):
        # 인버스2X 도 배수 상품이지만 방향이 반대라 상승장 취약성 축적과 성격이 다르다.
        rows = [{"ISU_SRT_CD": "252670", "ISU_ABBRV": "KODEX 200선물인버스2X",
                 "MKTCAP": "1,000,000,000,000"}]
        monkeypatch.setattr(krx_index, "_session", lambda: _FakeSession({"20260731": rows}))
        monkeypatch.setattr(krx_index, "is_business_day", lambda d: True)
        r = krx_index.etf_leverage_exposure(date(2026, 7, 31))
        assert r["leveraged_mktcap"] == 0
        assert r["leveraged_ratio"] == 0

    def test_휴장일은_조회_전에_직전_영업일로_스냅한다(self, monkeypatch):
        # 이 엔드포인트는 휴장일에 빈 응답이 아니라 직전 영업일 데이터를 그대로 준다.
        # 스냅하지 않으면 반환 as_of 가 실제 데이터 시점과 어긋난 거짓 라벨이 된다.
        sess = _FakeSession({"20260731": _ETF_ROWS, "20260801": _ETF_ROWS})
        monkeypatch.setattr(krx_index, "_session", lambda: sess)
        monkeypatch.setattr(krx_index, "is_business_day", lambda d: d.weekday() < 5)
        r = krx_index.etf_leverage_exposure(date(2026, 8, 1))  # 토요일
        assert r["as_of"] == date(2026, 7, 31)
        assert "20260801" not in sess.calls  # 휴장일로는 조회 자체를 하지 않는다

    def test_미인증이면_빈_dict(self, monkeypatch):
        monkeypatch.setattr(krx_index, "_session", lambda: None)
        assert krx_index.etf_leverage_exposure(date(2026, 7, 31)) == {}

    def test_응답이_비면_빈_dict(self, monkeypatch):
        monkeypatch.setattr(krx_index, "_session", lambda: _FakeSession({}))
        monkeypatch.setattr(krx_index, "is_business_day", lambda d: True)
        assert krx_index.etf_leverage_exposure(date(2026, 7, 31)) == {}

    def test_시총_파싱_실패는_0_으로_처리하고_중단하지_않는다(self, monkeypatch):
        rows = [{"ISU_SRT_CD": "1", "ISU_ABBRV": "KODEX 레버리지", "MKTCAP": "-"},
                {"ISU_SRT_CD": "2", "ISU_ABBRV": "KODEX 200", "MKTCAP": "10,000,000,000,000"}]
        monkeypatch.setattr(krx_index, "_session", lambda: _FakeSession({"20260731": rows}))
        monkeypatch.setattr(krx_index, "is_business_day", lambda d: True)
        r = krx_index.etf_leverage_exposure(date(2026, 7, 31))
        assert r["leveraged_mktcap"] == 0
        assert r["leveraged_count"] == 1
        assert r["total_mktcap"] == pytest.approx(10e12)
