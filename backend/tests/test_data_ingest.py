"""일봉 로컬 적재(C-1) — ensure_ohlcv_coverage 갭 판정, build_universe 합집합 검증.

실제 DB 대신 db.execute/scalars 를 대역(fake)으로 대체하고, 외부 조회(load_ohlcv)·
적재(upsert_price_ticks)를 모두 monkeypatch 해 순수 로직만 검증한다(다른 테스트 파일의
_FakeRedis/_FakeDbCtx 패턴과 동일하게 실제 I/O 없이 동작을 확인).
"""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from app.services.data import ingest as ingest_mod
from app.services.data import loader as loader_mod
from app.services.data.loader import ensure_ohlcv_coverage


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)


class _FakeMinMaxResult:
    def __init__(self, mn, mx):
        self._row = (mn, mx)

    def one(self):
        return self._row


class _FakeDb:
    """db.execute(select(func.min,max ...)) 만 대역화 — 단일 쿼리 함수를 검증 대상으로 삼는다."""

    def __init__(self, mn: datetime | None, mx: datetime | None):
        self.mn = mn
        self.mx = mx

    async def execute(self, stmt):
        return _FakeMinMaxResult(self.mn, self.mx)


@pytest.fixture(autouse=True)
def _no_real_io(monkeypatch):
    """load_ohlcv(동기, 외부 I/O)·upsert_price_ticks(비동기 DB 기록)를 기록용 대역으로 교체."""
    fetch_calls: list[tuple[str, date, date]] = []
    upsert_calls: list[tuple[str, object]] = []

    def fake_load_ohlcv(symbol, start, end):
        fetch_calls.append((symbol, start, end))
        return pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
            index=pd.DatetimeIndex([pd.Timestamp(start)]),
        )

    async def fake_upsert(db, symbol, df):
        upsert_calls.append((symbol, df))

    monkeypatch.setattr(loader_mod, "load_ohlcv", fake_load_ohlcv)
    monkeypatch.setattr(loader_mod, "upsert_price_ticks", fake_upsert)
    return fetch_calls, upsert_calls


async def test_ensure_coverage_fetches_full_range_when_no_data(_no_real_io):
    fetch_calls, upsert_calls = _no_real_io
    db = _FakeDb(mn=None, mx=None)
    start, end = date(2024, 1, 1), date(2024, 1, 31)

    await ensure_ohlcv_coverage(db, "005930", _dt(start), _dt(end))

    assert fetch_calls == [("005930", start, end)]
    assert len(upsert_calls) == 1


async def test_ensure_coverage_noop_when_fully_covered(_no_real_io):
    fetch_calls, upsert_calls = _no_real_io
    start, end = date(2024, 1, 1), date(2024, 3, 31)
    # 커버 구간이 요청 구간을 grace(5일) 이내로 감싸고 있음 — 보충 불필요.
    db = _FakeDb(mn=_dt(date(2024, 1, 2)), mx=_dt(date(2024, 3, 30)))

    await ensure_ohlcv_coverage(db, "005930", _dt(start), _dt(end))

    assert fetch_calls == []
    assert upsert_calls == []


async def test_ensure_coverage_fetches_only_tail_gap(_no_real_io):
    """야간 배치 이후 새로 생긴 최근 거래일(꼬리)만 보충 — 흔한 케이스(증분 적재)."""
    fetch_calls, _ = _no_real_io
    start, end = date(2024, 1, 1), date(2024, 3, 31)
    # 머리는 충분히 덮여 있고(1/2), 꼬리는 2/28 까지만 있어 3월 전체가 비었다.
    db = _FakeDb(mn=_dt(date(2024, 1, 2)), mx=_dt(date(2024, 2, 28)))

    await ensure_ohlcv_coverage(db, "005930", _dt(start), _dt(end))

    assert fetch_calls == [("005930", date(2024, 2, 28), end)]


async def test_ensure_coverage_fetches_only_head_gap(_no_real_io):
    """백테스트 시작일을 과거로 확장한 경우(머리 확장) — 꼬리는 이미 충분히 덮여 있음."""
    fetch_calls, _ = _no_real_io
    start, end = date(2023, 1, 1), date(2024, 3, 31)
    db = _FakeDb(mn=_dt(date(2024, 1, 2)), mx=_dt(date(2024, 3, 30)))

    await ensure_ohlcv_coverage(db, "005930", _dt(start), _dt(end))

    assert fetch_calls == [("005930", start, date(2024, 1, 2))]


async def test_ensure_coverage_within_grace_is_noop(_no_real_io):
    """휴장·연휴로 인한 며칠의 자연스러운 공백은 결측으로 오판하지 않는다(grace=5일)."""
    fetch_calls, _ = _no_real_io
    start, end = date(2024, 1, 1), date(2024, 1, 31)
    # 머리 3일, 꼬리 3일 비어 있음 — grace(5일) 이내라 보충하지 않는다.
    db = _FakeDb(mn=_dt(date(2024, 1, 4)), mx=_dt(date(2024, 1, 28)))

    await ensure_ohlcv_coverage(db, "005930", _dt(start), _dt(end))

    assert fetch_calls == []


async def test_ensure_coverage_swallows_external_fetch_failure(monkeypatch, caplog):
    """외부 조회 실패는 예외를 전파하지 않고 경고 로그만 남긴다(호출자는 있는 만큼만 씀)."""

    def boom(symbol, start, end):
        raise RuntimeError("network down")

    upsert_calls: list = []

    async def fake_upsert(db, symbol, df):
        upsert_calls.append((symbol, df))

    monkeypatch.setattr(loader_mod, "load_ohlcv", boom)
    monkeypatch.setattr(loader_mod, "upsert_price_ticks", fake_upsert)

    db = _FakeDb(mn=None, mx=None)
    with caplog.at_level("WARNING"):
        await ensure_ohlcv_coverage(db, "005930", _dt(date(2024, 1, 1)), _dt(date(2024, 1, 31)))

    assert upsert_calls == []
    assert "005930" in caplog.text


# ───────────────────── build_universe ─────────────────────


class _FakeScalars:
    def __init__(self, values):
        self._values = values

    def __iter__(self):
        return iter(self._values)


class _FakeStrategyDb:
    def __init__(self, configs: list[dict]):
        self.configs = configs

    async def scalars(self, stmt):
        return _FakeScalars(self.configs)


async def test_build_universe_unions_index_members_and_strategy_symbols(monkeypatch):
    import app.services.data.krx_index as krx_index

    monkeypatch.setattr(
        krx_index, "index_members", lambda as_of, index="KOSPI200": ["005930", "000660"]
    )
    configs = [
        {"type": "sma", "symbol": "035420"},  # 단일종목 전략
        {"type": "rebalance", "universe": ["005930", "051910"]},  # 리밸런싱 고정 후보풀
        {"type": "rebalance", "universe": []},  # PIT 동적 유니버스(고정 후보풀 없음)
    ]
    db = _FakeStrategyDb(configs)

    universe = await ingest_mod.build_universe(db)

    assert set(universe) == {"005930", "000660", "035420", "051910"}


async def test_build_universe_survives_index_lookup_failure(monkeypatch):
    """DataSourceError(외부 장애)는 삼키고 전략 유니버스로 계속한다(Task 7 — except DataSourceError)."""
    import app.services.data.krx_index as krx_index
    from app.services.data.errors import SourceAuthError

    def boom(as_of, index="KOSPI200"):
        raise SourceAuthError("krx", "KRX 인증 실패")

    monkeypatch.setattr(krx_index, "index_members", boom)
    db = _FakeStrategyDb([{"type": "sma", "symbol": "005930"}])

    universe = await ingest_mod.build_universe(db)

    assert universe == ["005930"]  # 지수 조회 실패해도 전략 유니버스는 반영
    # 우리 쪽 버그(TypeError) 전파 계약은 tests/test_caller_degradation.py
    # ::TestBuildUniverseDegradation 에서 검증한다(중복 회피).
