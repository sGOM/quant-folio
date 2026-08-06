"""로컬 저장소 SQL 리포지토리 통합 테스트.

기본 스위트는 실제 DB 를 쓰지 않는다(다른 테스트가 전부 FakeDB 대역). 이 파일만
실제 Postgres 를 필요로 하므로 QF_DB_TESTS=1 일 때만 돈다.

실행:
  docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v
"""
import os
from datetime import date

import pandas as pd
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("QF_DB_TESTS") != "1",
    reason="실제 DB 가 필요하다 — QF_DB_TESTS=1 로 실행",
)

from app.services.data.store import daily  # noqa: E402

_DAY = date(1990, 1, 2)  # 실데이터와 겹치지 않는 과거 일자


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    daily.delete_daily(_DAY)


def test_쓰고_읽으면_같은_값이_나온다():
    df = pd.DataFrame({"PER": [10.5, 20.0], "market": ["KOSPI", "KOSPI"]},
                      index=["005930", "000660"])
    daily.write_daily(_DAY, df, columns={"PER": "per", "market": "market"})

    out = daily.read_daily(_DAY, ["per", "market"], out_columns={"per": "PER", "market": "market"})

    assert sorted(out.index) == ["000660", "005930"]
    assert float(out.loc["005930", "PER"]) == pytest.approx(10.5)
    assert out.loc["005930", "market"] == "KOSPI"


def test_다른_소스가_같은_행의_다른_컬럼을_채운다():
    """시총 적재가 먼저 오고 펀더멘털이 나중에 와도 서로 지우지 않아야 한다."""
    cap = pd.DataFrame({"시가총액": [500_000]}, index=["005930"])
    daily.write_daily(_DAY, cap, columns={"시가총액": "market_cap"})

    fund = pd.DataFrame({"PER": [10.5]}, index=["005930"])
    daily.write_daily(_DAY, fund, columns={"PER": "per"})

    out = daily.read_daily(
        _DAY, ["per", "market_cap"], out_columns={"per": "PER", "market_cap": "시가총액"}
    )
    assert float(out.loc["005930", "PER"]) == pytest.approx(10.5)
    assert int(out.loc["005930", "시가총액"]) == 500_000  # 앞선 적재가 살아있다


def test_같은_컬럼_재적재시_NaN이_기존값을_지우지_않는다():
    """재조회에서 그 종목만 결측이 나와도 먼저 저장된 값이 살아있어야 한다."""
    first = pd.DataFrame({"PER": [10.5]}, index=["005930"])
    daily.write_daily(_DAY, first, columns={"PER": "per"})

    second = pd.DataFrame({"PER": [float("nan")]}, index=["005930"])
    daily.write_daily(_DAY, second, columns={"PER": "per"})

    out = daily.read_daily(_DAY, ["per"], out_columns={"per": "PER"})
    assert float(out.loc["005930", "PER"]) == pytest.approx(10.5)


def test_행이_없으면_요청한_컬럼의_빈_프레임을_준다():
    out = daily.read_daily(_DAY, ["per"], out_columns={"per": "PER"})
    assert out.empty
    assert list(out.columns) == ["PER"]


from app.services.data.store import periods  # noqa: E402

_START = date(1990, 1, 2)
_END = date(1990, 1, 31)


@pytest.fixture(autouse=True)
def _cleanup_periods():
    yield
    periods.delete_periods(_START, _END, "")
    periods.delete_periods(_START, _END, "기관합계,외국인")


def test_기간통계를_쓰고_읽는다():
    df = pd.DataFrame({"등락률": [5.5], "거래대금": [1_000_000]}, index=["005930"])
    periods.write_periods(
        _START, _END, "", df, columns={"등락률": "change_pct", "거래대금": "trading_value"}
    )

    out = periods.read_periods(
        _START, _END, "", ["change_pct", "trading_value"],
        out_columns={"change_pct": "등락률", "trading_value": "거래대금"},
    )
    assert float(out.loc["005930", "등락률"]) == pytest.approx(5.5)


def test_투자자군이_다르면_다른_행이다():
    """등락률 행과 순매수 행이 서로를 덮어쓰면 안 된다."""
    periods.write_periods(
        _START, _END, "", pd.DataFrame({"등락률": [5.5]}, index=["005930"]),
        columns={"등락률": "change_pct"},
    )
    periods.write_periods(
        _START, _END, "기관합계,외국인",
        pd.DataFrame({"net_buy_value": [1e9]}, index=["005930"]),
        columns={"net_buy_value": "net_buy_value"},
    )

    pc = periods.read_periods(_START, _END, "", ["change_pct"], out_columns={"change_pct": "등락률"})
    npv = periods.read_periods(
        _START, _END, "기관합계,외국인", ["net_buy_value"],
        out_columns={"net_buy_value": "net_buy_value"},
    )
    assert float(pc.loc["005930", "등락률"]) == pytest.approx(5.5)
    assert float(npv.loc["005930", "net_buy_value"]) == pytest.approx(1e9)


from app.services.data.store import indexes  # noqa: E402

_IDX = "9999"  # 실제 지수코드와 겹치지 않는 시험용 코드


@pytest.fixture(autouse=True)
def _cleanup_indexes():
    yield
    indexes.delete_index_ohlcv(_IDX)
    indexes.delete_constituents(_IDX, _DAY)


def test_지수_OHLCV_를_쓰고_기간으로_읽는다():
    df = pd.DataFrame(
        {"open": [100.0, 110.0], "high": [120.0, 130.0], "low": [90.0, 100.0],
         "close": [115.0, 125.0], "volume": [1000, 2000], "trading_value": [10, 20]},
        index=pd.to_datetime(["1990-01-02", "1990-01-03"]),
    )
    indexes.write_index_ohlcv(_IDX, df, index_name="시험지수")

    out = indexes.read_index_ohlcv(_IDX, date(1990, 1, 2), date(1990, 1, 3))
    assert len(out) == 2
    assert float(out.iloc[0]["close"]) == pytest.approx(115.0)
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "trading_value"]


def test_지수_OHLCV_기간_밖은_안_읽힌다():
    df = pd.DataFrame(
        {"open": [100.0], "high": [120.0], "low": [90.0],
         "close": [115.0], "volume": [1000], "trading_value": [10]},
        index=pd.to_datetime(["1990-01-02"]),
    )
    indexes.write_index_ohlcv(_IDX, df)
    assert indexes.read_index_ohlcv(_IDX, date(1990, 2, 1), date(1990, 2, 28)).empty


def test_PIT_지수구성을_쓰고_읽는다():
    indexes.write_constituents(_IDX, _DAY, ["005930", "000660"])
    assert sorted(indexes.read_constituents(_IDX, _DAY)) == ["000660", "005930"]


def test_지수구성_미적재는_빈_목록이다():
    assert indexes.read_constituents(_IDX, date(1991, 5, 5)) == []
