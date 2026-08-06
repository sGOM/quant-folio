"""metrics/fetch.py 의 로컬 스토어 배선과 실패 전파 검증.

pykrx 는 호출하지 않는다 — _pykrx_stock 을 대역으로 갈아끼운다.
스토어도 인메모리 원장 + dict 저장소로 대역해 실제 DB 를 쓰지 않는다.
"""
from datetime import date

import pandas as pd
import pytest

from app.services.data.errors import DataSourceError, SourceUnavailableError
from app.services.data.store.ledger import InMemoryLedger
from app.services.metrics import fetch as F


class _FakeStock:
    """pykrx.stock 대역 — 호출 횟수를 세고 지정된 응답/예외를 돌려준다."""

    def __init__(self, per_market: dict[str, pd.DataFrame | Exception]):
        self.per_market = per_market
        self.calls: list[str] = []

    def get_market_fundamental(self, ymd, market=None, **kw):
        self.calls.append(market)
        val = self.per_market[market]
        if isinstance(val, Exception):
            raise val
        return val


@pytest.fixture
def _store(monkeypatch):
    """스토어를 인메모리로 대역하고, 저장된 프레임을 그대로 돌려주게 한다."""
    ledger = InMemoryLedger()
    saved: dict[tuple, pd.DataFrame] = {}

    monkeypatch.setattr(F, "_store_ledger", lambda: ledger)
    monkeypatch.setattr(F, "_store_write_daily", lambda day, df, columns: saved.__setitem__((day, tuple(columns)), df))
    monkeypatch.setattr(F, "_store_read_daily", lambda day, cols, out_columns: saved.get((day, tuple(out_columns)), pd.DataFrame()))
    F._FUND_CACHE.clear()
    yield ledger, saved
    F._FUND_CACHE.clear()


def _fund_frame():
    return pd.DataFrame(
        {"PER": [10.0], "PBR": [1.0], "DIV": [2.0]}, index=["005930"]
    )


def test_전량_실패는_예외로_전파된다(_store, monkeypatch):
    """§47 사고의 직접 원인 — 전량 실패가 빈 프레임이 되어 백테스트가 '성공'했다."""
    boom = SourceUnavailableError("krx", "차단")
    fake = _FakeStock({"KOSPI": boom, "KOSDAQ": boom})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    with pytest.raises(DataSourceError):
        F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])


def test_일부_시장만_실패하면_성공분을_돌려준다(_store, monkeypatch):
    fake = _FakeStock({"KOSPI": _fund_frame(), "KOSDAQ": SourceUnavailableError("krx", "일시장애")})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    out = F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])
    assert len(out) == 1


def test_부분_실패는_확정으로_굳히지_않는다(_store, monkeypatch):
    """다음 호출에서 빠진 시장을 보완할 수 있어야 한다."""
    ledger, _ = _store
    fake = _FakeStock({"KOSPI": _fund_frame(), "KOSDAQ": SourceUnavailableError("krx", "일시장애")})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])
    entry = ledger.get("fundamentals", "20190312|KOSDAQ,KOSPI")
    assert entry is None or entry.final is False


def test_확정_적재분은_pykrx_를_다시_부르지_않는다(_store, monkeypatch):
    fake = _FakeStock({"KOSPI": _fund_frame(), "KOSDAQ": _fund_frame()})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])
    F._FUND_CACHE.clear()  # 프로세스 내 1차 캐시를 비워 2차(로컬)만 남긴다
    F._fetch_fundamentals("20190312", ["KOSPI", "KOSDAQ"])

    assert len(fake.calls) == 2  # 첫 호출의 두 시장뿐
