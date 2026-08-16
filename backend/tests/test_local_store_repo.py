"""로컬 저장소 SQL 리포지토리 통합 테스트.

기본 스위트는 실제 DB 를 쓰지 않는다(다른 테스트가 전부 FakeDB 대역). 이 파일만
실제 Postgres 를 필요로 하므로 QF_DB_TESTS=1 일 때만 돈다.

실행:
  docker compose exec -T -e QF_DB_TESTS=1 web pytest tests/test_local_store_repo.py -v
"""
import os
from datetime import date, timedelta

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


def test_B1_market_필터_없으면_전_시장이_섞이고_필터를_걸면_격리된다():
    """PK 가 (trade_date, symbol) 라 KOSPI/KOSDAQ 행이 같은 키공간에 섞여 저장된다.
    markets 필터가 그 오염을 막는지 실제 DB 로 검증한다.
    """
    df = pd.DataFrame(
        {"per": [10.0, 20.0], "market": ["KOSPI", "KOSDAQ"]},
        index=["005930", "900001"],
    )
    daily.write_daily(_DAY, df, columns={"per": "per", "market": "market"})

    kospi_only = daily.read_daily(
        _DAY, ["per", "market"], out_columns={"per": "PER", "market": "market"},
        markets=["KOSPI"],
    )
    assert sorted(kospi_only.index) == ["005930"]

    unfiltered = daily.read_daily(
        _DAY, ["per", "market"], out_columns={"per": "PER", "market": "market"},
    )
    assert sorted(unfiltered.index) == ["005930", "900001"]  # markets=None 은 기존과 동일(하위호환)


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


def test_B1_기간통계_market_필터_없으면_전_시장이_섞이고_필터를_걸면_격리된다():
    """stock_period_stats 도 PK 에 시장 구분이 없어 같은 오염이 가능하다."""
    df = pd.DataFrame(
        {"change_pct": [5.5, -3.0], "market": ["KOSPI", "KOSDAQ"]},
        index=["005930", "900001"],
    )
    periods.write_periods(_START, _END, "", df, columns={"change_pct": "change_pct", "market": "market"})

    kospi_only = periods.read_periods(
        _START, _END, "", ["change_pct", "market"],
        out_columns={"change_pct": "등락률", "market": "market"},
        markets=["KOSPI"],
    )
    assert sorted(kospi_only.index) == ["005930"]


def test_I1_기간통계_종목명_컬럼이_왕복된다():
    """I1: name 컬럼이 없으면 로컬 히트에서 종목명이 조용히 사라진다."""
    df = pd.DataFrame({"change_pct": [5.5], "name": ["삼성전자"]}, index=["005930"])
    periods.write_periods(
        _START, _END, "", df, columns={"change_pct": "change_pct", "name": "name"}
    )

    out = periods.read_periods(
        _START, _END, "", ["change_pct", "name"],
        out_columns={"change_pct": "등락률", "name": "종목명"},
    )
    assert out.loc["005930", "종목명"] == "삼성전자"
    assert float(out.loc["005930", "등락률"]) == pytest.approx(5.5)  # name 이 숫자변환을 오염시키지 않는다


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


from app.services.data.store import dart_store  # noqa: E402

_CORP = "00000000"  # 실제 corp_code 와 겹치지 않는 시험값


@pytest.fixture(autouse=True)
def _cleanup_dart():
    yield
    dart_store.delete_accounts(_CORP, 1990, "11011", "CFS")
    dart_store.delete_accounts(_CORP, date.today().year - 2, "11011", "CFS")


def test_원계정을_쓰고_읽는다():
    accounts = [{"account_nm": "자산총계", "thstrm_amount": "1000", "rcept_no": "19910301000001"}]
    dart_store.write_accounts(_CORP, 1990, "11011", "CFS", accounts)

    got = dart_store.read_accounts(_CORP, 1990, "11011", "CFS")
    assert got is not None
    rows, final = got
    assert rows[0]["account_nm"] == "자산총계"
    assert final is True  # 1991년 접수 + 90일은 이미 한참 지났다


def test_미적재는_None_이다():
    assert dart_store.read_accounts(_CORP, 1989, "11011", "CFS") is None


def test_I5_재조회에_접수번호가_없어도_기존_접수일이_지워지지_않는다():
    """I5: dart_financials 만 NULL 보존 upsert(COALESCE)를 안 써서 생기던 함정.

    정정공시 재조회분에서 rcept_no 파싱이 실패하면 새 rcept_dt 가 None 이 된다.
    그대로 덮으면 confirmed_at 이 폴백(사업연도말 + 1년)으로 다시 계산되는데, 늦은
    정정(접수일 + 90일이 아직 미래)인 경우 그 폴백은 **더 이른** 날짜라 아직 유예
    중인 보고서를 확정으로 굳혀버린다 — 정정 전 값이 영구히 박히는 방향이다.

    날짜는 오늘 기준 상대값으로 잡아 시간이 지나도 테스트가 썩지 않게 한다.
    """
    today = date.today()
    bsns_year = today.year - 2          # 폴백 confirmed_at = (today.year-1)-12-31 → 과거
    recent = today - timedelta(days=30)  # 접수일 + 90일 → 미래(아직 유예 중)
    rcept_no = f"{recent:%Y%m%d}000001"

    with_rcept = [{"account_nm": "자산총계", "thstrm_amount": "1000", "rcept_no": rcept_no}]
    dart_store.write_accounts(_CORP, bsns_year, "11011", "CFS", with_rcept)

    got = dart_store.read_accounts(_CORP, bsns_year, "11011", "CFS")
    assert got is not None and got[1] is False  # 유예 중 = 미확정

    # 재조회분에 rcept_no 가 없다(파싱 실패 상황).
    without_rcept = [{"account_nm": "자산총계", "thstrm_amount": "2000"}]
    dart_store.write_accounts(_CORP, bsns_year, "11011", "CFS", without_rcept)

    got = dart_store.read_accounts(_CORP, bsns_year, "11011", "CFS")
    assert got is not None
    rows, final = got
    # 본문(accounts)은 정정 반영을 위해 새 값으로 덮인다.
    assert rows[0]["thstrm_amount"] == "2000"
    # 확정 판정은 기존 접수일을 유지해 여전히 미확정이어야 한다.
    assert final is False


# ───── 지수 OHLCV 확보 구간(커버리지) ─────

_COV_CODE = "9999"  # 실제 지수코드와 겹치지 않는 시험값


@pytest.fixture(autouse=True)
def _cleanup_coverage():
    yield
    indexes.delete_index_ohlcv(_COV_CODE)


def test_확보구간을_쓰고_읽는다():
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30))

    assert indexes.read_coverage(_COV_CODE) == [(date(2020, 1, 1), date(2020, 6, 30))]


def test_겹치는_구간은_하나로_병합된다():
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30))
    indexes.merge_coverage(_COV_CODE, date(2020, 4, 1), date(2020, 9, 30))

    assert indexes.read_coverage(_COV_CODE) == [(date(2020, 1, 1), date(2020, 9, 30))]


def test_새_구간이_기존보다_앞서도_병합된다():
    """병합 조건을 한쪽만 보면(새.from <= 기존.to+1) 이 케이스가 누락된다."""
    indexes.merge_coverage(_COV_CODE, date(2020, 6, 1), date(2020, 9, 30))
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 7, 31))

    assert indexes.read_coverage(_COV_CODE) == [(date(2020, 1, 1), date(2020, 9, 30))]


def test_새_구간이_기존보다_한참_앞서면_병합하지_않는다():
    """양방향 조건 중 `기존.from <= 새.to + 1일` 쪽이 빠지면 이 케이스가 잘못 병합된다.

    위 '앞서도' 테스트는 새 구간이 기존과 겹칠 때만 검증해 한쪽 조건만으로도
    우연히 통과한다. 이 테스트는 겹치지 않고 확실히 떨어진(갭 2개월) 경우를 써서
    두 조건이 실제로 각자 필요함을 고정한다.
    """
    indexes.merge_coverage(_COV_CODE, date(2020, 6, 1), date(2020, 9, 30))
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 3, 31))

    assert indexes.read_coverage(_COV_CODE) == [
        (date(2020, 1, 1), date(2020, 3, 31)),
        (date(2020, 6, 1), date(2020, 9, 30)),
    ]


def test_하루_맞닿은_구간은_병합된다():
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30))
    indexes.merge_coverage(_COV_CODE, date(2020, 7, 1), date(2020, 9, 30))

    assert indexes.read_coverage(_COV_CODE) == [(date(2020, 1, 1), date(2020, 9, 30))]


def test_주말만큼_벌어진_구간은_병합하지_않는다():
    """그 사이에 거래일이 있었는지 달력 없이 단정할 수 없다 — 보수적으로 둘로 남긴다."""
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 26))  # 금
    indexes.merge_coverage(_COV_CODE, date(2020, 6, 29), date(2020, 9, 30))  # 월

    assert indexes.read_coverage(_COV_CODE) == [
        (date(2020, 1, 1), date(2020, 6, 26)),
        (date(2020, 6, 29), date(2020, 9, 30)),
    ]


def test_역전된_구간은_기록하지_않는다():
    indexes.merge_coverage(_COV_CODE, date(2020, 6, 30), date(2020, 1, 1))

    assert indexes.read_coverage(_COV_CODE) == []


def test_일봉_삭제는_커버리지도_함께_지운다():
    """행만 지우고 커버리지가 남으면 '커버됐다는데 행이 없는' 영구 빈 결과가 된다."""
    df = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
         "volume": [10], "trading_value": [100]},
        index=pd.to_datetime(["2020-01-02"]),
    )
    indexes.write_index_ohlcv(_COV_CODE, df, index_name=None)
    indexes.merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30))

    indexes.delete_index_ohlcv(_COV_CODE)

    assert indexes.read_coverage(_COV_CODE) == []
    assert indexes.read_index_ohlcv(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30)).empty


def test_동시_병합은_유실_없이_직렬화된다():
    """select→delete→insert 에 행 잠금이 없으면, 겹치는 두 범위를 동시에 병합할 때
    나중에 커밋한 쪽이 먼저 커밋한 쪽의 확장분을 낡은 읽기값으로 덮어써 커버리지가
    좁아질 수 있다(유실 방향은 항상 좁아지는 쪽이라 거짓 커버리지는 안 생기지만,
    불필요한 재조회를 유발한다). 반복 실행으로 타이밍 의존 회귀를 잡는다.
    """
    import asyncio

    async def _concurrent_merge():
        await asyncio.gather(
            indexes._merge_coverage(_COV_CODE, date(2020, 1, 1), date(2020, 6, 30)),
            indexes._merge_coverage(_COV_CODE, date(2020, 4, 1), date(2020, 9, 30)),
        )

    for _ in range(20):
        indexes.delete_index_ohlcv(_COV_CODE)
        asyncio.run(_concurrent_merge())
        assert indexes.read_coverage(_COV_CODE) == [
            (date(2020, 1, 1), date(2020, 9, 30))
        ]
