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
    yield
    krx_index._MEMBERS_CACHE.clear()
    krx_index._STOCKS_CACHE = None


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
