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
    """스토어를 인메모리로 대역하되, 실제 daily.py/periods.py 처럼 테이블 컬럼명으로
    저장하고 market 필터까지 흉내 낸다.

    이전 판은 write 키(원본 컬럼명, 예 "PER")와 read 키(테이블 컬럼명, 예 "per")가
    서로 달라 저장 직후 조회가 **항상 캐시 미스**였다 — 로컬 읽기 경로를 한 번도
    태우지 못한 채 통과하던 결함(B1 통합 리뷰에서 지적됨). 이제 write/read 모두
    dst(테이블) 컬럼명 기준으로 같은 dict 에 upsert-coalesce 하므로, 이 픽스처를
    쓰는 테스트가 실제로 로컬 읽기 내용을 검증한다.
    """
    ledger = InMemoryLedger()
    daily_saved: dict[date, pd.DataFrame] = {}
    period_saved: dict[tuple[date, date, str], pd.DataFrame] = {}

    def _upsert(existing: pd.DataFrame | None, renamed: pd.DataFrame) -> pd.DataFrame:
        renamed = renamed.copy()
        renamed.index = renamed.index.astype(str).str.zfill(6)
        if existing is None:
            return renamed
        # coalesce: 새 값이 있으면 새 값, NaN 이면 기존값 보존(실제 upsert 와 동일 규약).
        return renamed.combine_first(existing)

    def _filter_and_rename(
        df: pd.DataFrame | None,
        cols: list[str],
        out_columns: dict[str, str],
        markets: list[str] | None,
    ) -> pd.DataFrame:
        if df is None:
            empty = pd.DataFrame(columns=[out_columns[c] for c in cols])
            empty.index.name = "티커"
            return empty
        if markets and "market" in df.columns:
            df = df[df["market"].isin(markets)]
        present = [c for c in cols if c in df.columns]
        out = df[present].rename(columns={c: out_columns[c] for c in present})
        out.index.name = "티커"
        return out

    def _write_daily(day, df, columns):
        if df is None or df.empty:
            return
        present = {src: dst for src, dst in columns.items() if src in df.columns}
        if not present:
            return
        renamed = df[list(present)].rename(columns=present)
        daily_saved[day] = _upsert(daily_saved.get(day), renamed)

    def _read_daily(day, cols, out_columns, markets=None):
        return _filter_and_rename(daily_saved.get(day), cols, out_columns, markets)

    def _write_periods(start, end, investors, df, columns):
        if df is None or df.empty:
            return
        present = {src: dst for src, dst in columns.items() if src in df.columns}
        if not present:
            return
        renamed = df[list(present)].rename(columns=present)
        key = (start, end, investors)
        period_saved[key] = _upsert(period_saved.get(key), renamed)

    def _read_periods(start, end, investors, cols, out_columns, markets=None):
        key = (start, end, investors)
        return _filter_and_rename(period_saved.get(key), cols, out_columns, markets)

    monkeypatch.setattr(F, "_store_ledger", lambda: ledger)
    monkeypatch.setattr(F, "_store_write_daily", _write_daily)
    monkeypatch.setattr(F, "_store_read_daily", _read_daily)
    monkeypatch.setattr(F, "_store_write_periods", _write_periods)
    monkeypatch.setattr(F, "_store_read_periods", _read_periods)
    F._FUND_CACHE.clear()
    yield ledger, daily_saved
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


def test_순매수_전량실패는_예외로_전파된다(_store, monkeypatch):
    """이전 판은 빈 프레임을 돌려줘 수급 팩터가 조용히 중립이 됐다."""

    class _Boom:
        def get_market_net_purchases_of_equities(self, *a, **kw):
            raise RuntimeError("차단")

    monkeypatch.setattr(F, "_pykrx_stock", lambda: _Boom())
    monkeypatch.setattr(F, "_store_write_periods", lambda *a, **kw: None)
    monkeypatch.setattr(F, "_store_read_periods", lambda *a, **kw: pd.DataFrame())

    with pytest.raises(DataSourceError):
        F._fetch_net_purchases("20190101", "20190131", ["KOSPI"])


# ───────────────────── B1: market 필터 회귀 ─────────────────────
# 재현 절차: (0) 야간 배치처럼 KOSPI+KOSDAQ 를 함께 적재 → (1) KOSPI 단독 1회차
# (새 캐시키라 원격 경로) → (2) KOSPI 단독 2회차(원장 final 히트 → 로컬 읽기).
# market 필터가 없으면 2회차부터 KOSDAQ 티커가 섞여 나온다.


class _CapFakeStock:
    """get_market_cap 대역 — 시장별로 다른 티커를 반환한다."""

    def __init__(self, per_market: dict[str, pd.DataFrame]):
        self.per_market = per_market

    def get_market_cap(self, ymd, market=None, **kw):
        return self.per_market[market]


def _cap_frame(ticker: str) -> pd.DataFrame:
    return pd.DataFrame(
        {"시가총액": [1_000_000], "거래량": [100], "거래대금": [10_000], "상장주식수": [1_000]},
        index=[ticker],
    )


def test_B1_시가총액_1회차와_2회차_결과가_같다(_store, monkeypatch):
    fake = _CapFakeStock({"KOSPI": _cap_frame("005930"), "KOSDAQ": _cap_frame("900001")})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    F._fetch_market_cap("20190312", ["KOSPI", "KOSDAQ"])  # 야간 배치 적재(오염원)

    first = F._fetch_market_cap("20190312", ["KOSPI"])  # 1회차: 원격 경로
    second = F._fetch_market_cap("20190312", ["KOSPI"])  # 2회차: 로컬 읽기 경로

    assert sorted(first.index) == sorted(second.index) == ["005930"]
    assert "900001" not in second.index


class _PriceChangeFakeStock:
    """get_market_price_change 대역 — 시장별로 다른 티커를 반환한다."""

    def __init__(self, per_market: dict[str, pd.DataFrame]):
        self.per_market = per_market

    def get_market_price_change(self, start_ymd, end_ymd, market=None, **kw):
        return self.per_market[market]


def _pc_frame(ticker: str, name: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "시가": [1000.0], "종가": [1050.0], "등락률": [5.0],
            "거래량": [100], "거래대금": [10_000], "종목명": [name],
        },
        index=[ticker],
    )


def test_B1_가격변동_1회차와_2회차_결과가_같다(_store, monkeypatch):
    fake = _PriceChangeFakeStock({
        "KOSPI": _pc_frame("005930", "삼성전자"),
        "KOSDAQ": _pc_frame("900001", "고스트"),
    })
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    F._fetch_price_change("20190301", "20190312", ["KOSPI", "KOSDAQ"])  # 야간 배치 적재

    first = F._fetch_price_change("20190301", "20190312", ["KOSPI"])
    second = F._fetch_price_change("20190301", "20190312", ["KOSPI"])

    assert sorted(first.index) == sorted(second.index) == ["005930"]
    assert "900001" not in second.index
    # I1: 로컬 히트에도 종목명이 살아있어야 한다(_build_krx_name_map 이 재활용).
    assert second.loc["005930", "종목명"] == "삼성전자"
