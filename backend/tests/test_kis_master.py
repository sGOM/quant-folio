"""app/services/data/kis_master.py 단위 테스트.

외부 호출은 httpx 를 대역으로 바꿔 검증한다(네트워크 의존 없음). DB 상호작용은
tests/test_krx_index.py 의 _FakeSnapshotDB 패턴처럼 최소 대역 세션으로 검증한다
(실 DB 연결 없음).
"""
from __future__ import annotations

import io
from datetime import date

import pytest


def test_kis_stock_master_snapshot_model_schema():
    from app.models import KisStockMasterSnapshot

    table = KisStockMasterSnapshot.__table__
    assert table.name == "kis_stock_master_snapshots"
    assert set(table.columns.keys()) == {
        "id", "trade_date", "symbol", "market", "name", "raw", "created_at",
    }
    unique_cols = {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if type(constraint).__name__ == "UniqueConstraint"
    }
    assert ("symbol", "trade_date") in unique_cols


def _build_line(market: str, symbol: str, std_code: str, name: str, values: dict) -> str:
    """테스트용 종목마스터 1행을 실제 파일과 동일한 고정폭 레이아웃으로 만든다.

    프로덕션 파서가 쓰는 것과 동일한 field_specs/columns 를 그대로 가져다 써서
    폭이 어긋날 여지를 없앤다 — 필드 순서가 바뀌면 프로덕션 코드와 이 헬퍼가
    동시에 깨지므로 회귀를 잡을 수 있다.
    """
    from app.services.data import kis_master as km

    field_specs, columns = (
        (km._KOSPI_FIELD_SPECS, km._KOSPI_COLUMNS) if market == "KOSPI"
        else (km._KOSDAQ_FIELD_SPECS, km._KOSDAQ_COLUMNS)
    )
    part2 = "".join(
        str(values.get(col, "")).ljust(width)[:width]
        for col, width in zip(columns, field_specs)
    )
    head = symbol.ljust(9) + std_code.ljust(12) + name
    return head + part2


def test_parse_master_kospi_extracts_flags_and_par_value():
    from app.services.data import kis_master as km

    line = _build_line(
        "KOSPI", "005930", "KR7005930003", "삼성전자",
        {"거래정지": "N", "관리종목": "N", "액면가": "100", "지수업종대분류": "20"},
    )
    rows = km._parse_master(line, "KOSPI")

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "005930"
    assert row["name"] == "삼성전자"
    assert row["raw"]["거래정지"].strip() == "N"
    assert row["raw"]["액면가"].strip() == "100"
    assert row["raw"]["표준코드"] == "KR7005930003"


def test_parse_master_kosdaq_multiple_rows():
    from app.services.data import kis_master as km

    lines = "\n".join([
        _build_line("KOSDAQ", "247540", "KR7247540008", "에코프로비엠",
                    {"거래정지 여부": "N", "관리 종목 여부": "N"}),
        _build_line("KOSDAQ", "086520", "KR7086520004", "에코프로",
                    {"거래정지 여부": "Y", "관리 종목 여부": "N"}),
    ])
    rows = km._parse_master(lines, "KOSDAQ")

    assert [r["symbol"] for r in rows] == ["247540", "086520"]
    assert rows[0]["name"] == "에코프로비엠"
    assert rows[1]["raw"]["거래정지 여부"].strip() == "Y"


def test_parse_master_line_too_short_raises_schema_error():
    from app.services.data.errors import SourceSchemaError
    from app.services.data import kis_master as km

    with pytest.raises(SourceSchemaError):
        km._parse_master("005930짧은행", "KOSPI")


def test_parse_master_empty_text_raises_schema_error():
    from app.services.data.errors import SourceSchemaError
    from app.services.data import kis_master as km

    with pytest.raises(SourceSchemaError):
        km._parse_master("", "KOSPI")


def test_parse_master_all_rows_filtered_raises_schema_error():
    """symbol/name 이 모두 빈 값이라 모든 행이 걸러지면 빈 리스트를 조용히
    반환하지 말고 SourceSchemaError 로 실패해야 한다(KIS 포맷 변경 감지용)."""
    from app.services.data.errors import SourceSchemaError
    from app.services.data import kis_master as km

    line = _build_line("KOSPI", "", "", "", {"거래정지": "N"})

    with pytest.raises(SourceSchemaError):
        km._parse_master(line, "KOSPI")


class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            import httpx
            request = httpx.Request("GET", "https://example.test")
            response = httpx.Response(self._status, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)


def _zip_bytes(inner_name: str, content: bytes) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_name, content)
    return buf.getvalue()


def test_fetch_market_master_downloads_extracts_and_parses(monkeypatch):
    from app.services.data import kis_master as km

    line = _build_line(
        "KOSPI", "005930", "KR7005930003", "삼성전자", {"거래정지": "N"},
    )
    zip_content = _zip_bytes("kospi_code.mst", line.encode("cp949"))

    captured = {}

    def fake_get(url, timeout=None, follow_redirects=None):
        captured["url"] = url
        return _Resp(zip_content)

    monkeypatch.setattr(km.httpx, "get", fake_get)

    rows = km.fetch_market_master("KOSPI")

    assert "kospi_code.mst.zip" in captured["url"]
    assert rows[0]["symbol"] == "005930"


def test_fetch_market_master_download_failure_raises_unavailable(monkeypatch):
    from app.services.data import kis_master as km
    from app.services.data.errors import SourceUnavailableError

    def boom(*a, **kw):
        raise RuntimeError("연결 실패")

    monkeypatch.setattr(km.httpx, "get", boom)

    with pytest.raises(SourceUnavailableError):
        km.fetch_market_master("KOSPI")


def test_fetch_market_master_zip_without_mst_raises_schema_error(monkeypatch):
    from app.services.data import kis_master as km
    from app.services.data.errors import SourceSchemaError

    zip_content = _zip_bytes("readme.txt", b"not the master file")
    monkeypatch.setattr(km.httpx, "get", lambda *a, **kw: _Resp(zip_content))

    with pytest.raises(SourceSchemaError):
        km.fetch_market_master("KOSPI")


def test_fetch_market_master_bad_zip_raises_schema_error(monkeypatch):
    from app.services.data import kis_master as km
    from app.services.data.errors import SourceSchemaError

    monkeypatch.setattr(km.httpx, "get", lambda *a, **kw: _Resp(b"this is not a zip"))

    with pytest.raises(SourceSchemaError):
        km.fetch_market_master("KOSPI")


class _FakeMasterDB:
    """KisStockMasterSnapshot 대상 execute/add_all/flush 만 지원하는 최소 대역."""

    def __init__(self):
        self.deleted_markets: list[str] = []
        self.added: list = []
        self.flushed = False

    async def execute(self, stmt):
        self.deleted_markets.append(str(stmt))
        return None

    def add_all(self, objs):
        self.added.extend(objs)

    async def flush(self):
        self.flushed = True


def test_snapshot_stock_master_saves_both_markets(monkeypatch):
    import asyncio

    from app.services.data import kis_master as km

    def _fake_fetch(market):
        if market == "KOSPI":
            return [{"symbol": "005930", "name": "삼성전자", "raw": {"거래정지": "N"}}]
        return [{"symbol": "247540", "name": "에코프로비엠", "raw": {"거래정지 여부": "N"}}]

    monkeypatch.setattr(km, "fetch_market_master", _fake_fetch)
    db = _FakeMasterDB()

    n = asyncio.run(km.snapshot_stock_master(db, trade_date=date(2026, 8, 19)))

    assert n == 2
    assert {o.symbol for o in db.added} == {"005930", "247540"}
    assert {o.market for o in db.added} == {"KOSPI", "KOSDAQ"}
    assert all(o.trade_date == date(2026, 8, 19) for o in db.added)
    assert db.flushed


def test_snapshot_stock_master_partial_failure_still_saves_other_market(monkeypatch):
    import asyncio

    from app.services.data import kis_master as km
    from app.services.data.errors import SourceUnavailableError

    def _fake_fetch(market):
        if market == "KOSPI":
            raise SourceUnavailableError("kis_master", "다운로드 실패")
        return [{"symbol": "247540", "name": "에코프로비엠", "raw": {}}]

    monkeypatch.setattr(km, "fetch_market_master", _fake_fetch)
    db = _FakeMasterDB()

    n = asyncio.run(km.snapshot_stock_master(db, trade_date=date(2026, 8, 19)))

    assert n == 1
    assert {o.symbol for o in db.added} == {"247540"}


def test_snapshot_stock_master_both_markets_fail_raises(monkeypatch):
    import asyncio

    from app.services.data import kis_master as km
    from app.services.data.errors import DataSourceError, SourceUnavailableError

    def _fake_fetch(market):
        raise SourceUnavailableError("kis_master", f"{market} 다운로드 실패")

    monkeypatch.setattr(km, "fetch_market_master", _fake_fetch)
    db = _FakeMasterDB()

    with pytest.raises(DataSourceError):
        asyncio.run(km.snapshot_stock_master(db, trade_date=date(2026, 8, 19)))

    assert db.added == []


def test_snapshot_stock_master_kospi_download_failure_does_not_block_kosdaq(monkeypatch):
    """Finding 1 회귀 방지: 쿨다운 키를 시장 간에 공유하면 KOSPI 다운로드 실패가
    건 쿨다운을 KOSDAQ 의 _download_zip 호출이 물려받아 httpx.get 을 아예 타지
    않는다. fetch_market_master 를 몽키패치하면 이 버그가 숨으므로, 실제
    _download_zip 경로(httpx.get)를 그대로 태워야 한다."""
    import asyncio

    from app.services.data import kis_master as km

    kosdaq_line = _build_line(
        "KOSDAQ", "247540", "KR7247540008", "에코프로비엠", {"거래정지 여부": "N"},
    )
    kosdaq_zip = _zip_bytes("kosdaq_code.mst", kosdaq_line.encode("cp949"))

    call_count = {"count": 0}

    def fake_get(url, timeout=None, follow_redirects=None):
        call_count["count"] += 1
        if "kospi" in url:
            raise RuntimeError("KOSPI CDN 연결 실패")
        return _Resp(kosdaq_zip)

    monkeypatch.setattr(km.httpx, "get", fake_get)
    db = _FakeMasterDB()

    n = asyncio.run(km.snapshot_stock_master(db, trade_date=date(2026, 8, 19)))

    # httpx.get 이 두 시장 모두에 대해 실제로 호출됐어야 한다 — 쿨다운으로
    # KOSDAQ 시도가 생략됐다면 1회에서 그친다.
    assert call_count["count"] == 2
    assert n == 1
    assert {o.symbol for o in db.added} == {"247540"}
    assert {o.market for o in db.added} == {"KOSDAQ"}


def test_snapshot_stock_master_non_datasource_error_does_not_block_other_market(monkeypatch):
    """Finding 3 회귀 방지: fetch_market_master 가 DataSourceError 로 포장되지
    않은 예외(UnicodeDecodeError·ParserError 류를 대표하는 RuntimeError)를 던져도
    다른 시장 처리는 계속돼야 한다."""
    import asyncio

    from app.services.data import kis_master as km

    def _fake_fetch(market):
        if market == "KOSPI":
            raise RuntimeError("예상 못한 파싱 버그")
        return [{"symbol": "247540", "name": "에코프로비엠", "raw": {}}]

    monkeypatch.setattr(km, "fetch_market_master", _fake_fetch)
    db = _FakeMasterDB()

    n = asyncio.run(km.snapshot_stock_master(db, trade_date=date(2026, 8, 19)))

    assert n == 1
    assert {o.symbol for o in db.added} == {"247540"}


class _FakeScalarDB:
    def __init__(self, row):
        self._row = row

    async def scalar(self, stmt):
        return self._row


def test_latest_stock_master_returns_none_when_missing():
    import asyncio

    from app.services.data import kis_master as km

    db = _FakeScalarDB(None)
    result = asyncio.run(km.latest_stock_master(db, "999999"))
    assert result is None


def test_latest_stock_master_flattens_raw():
    import asyncio

    from app.services.data import kis_master as km

    class _Row:
        trade_date = date(2026, 8, 19)
        market = "KOSPI"
        name = "삼성전자"
        raw = {"거래정지": "N"}

    db = _FakeScalarDB(_Row())
    result = asyncio.run(km.latest_stock_master(db, "005930"))

    assert result == {
        "trade_date": date(2026, 8, 19), "market": "KOSPI",
        "name": "삼성전자", "거래정지": "N",
    }
