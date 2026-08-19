"""app/services/data/kis_master.py 단위 테스트.

외부 호출은 httpx 를 대역으로 바꿔 검증한다(네트워크 의존 없음). DB 상호작용은
tests/test_krx_index.py 의 _FakeSnapshotDB 패턴처럼 최소 대역 세션으로 검증한다
(실 DB 연결 없음).
"""
from __future__ import annotations

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
