"""metrics/fetch.py 의 로컬 스토어 배선과 실패 전파 검증.

pykrx 는 호출하지 않는다 — _pykrx_stock 을 대역으로 갈아끼운다.
스토어도 인메모리 원장 + dict 저장소로 대역해 실제 DB 를 쓰지 않는다.
"""
from datetime import date, timedelta

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


class _NetPurchaseFakeStock:
    """get_market_net_purchases_of_equities 대역 — 시장별로 다른 티커를 반환한다."""

    def __init__(self, per_market: dict[str, str]):
        self.per_market = per_market

    def get_market_net_purchases_of_equities(self, start_ymd, end_ymd, market, investor):
        ticker = self.per_market[market]
        return pd.DataFrame({"순매수거래대금": [1_000_000.0]}, index=[ticker])


def test_B1_순매수_1회차와_2회차_결과가_같다(_store, monkeypatch):
    """순매수도 다른 4종과 같은 계약이어야 한다 — 시장 격리 + 컬럼 집합 대칭.

    이 소스만 회귀 테스트가 없어서, 원격 경로는 market 을 싣고 로컬 경로는 안 싣는
    비대칭이 리뷰에서야 드러났다(§49 I1 의 잠복 형태 — 1회차와 2회차의 컬럼이 달라
    소비자가 조용히 다르게 동작하는 것).
    """
    fake = _NetPurchaseFakeStock({"KOSPI": "005930", "KOSDAQ": "900001"})
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    F._fetch_net_purchases("20190301", "20190312", ["KOSPI", "KOSDAQ"])  # 야간 배치 적재

    first = F._fetch_net_purchases("20190301", "20190312", ["KOSPI"])
    second = F._fetch_net_purchases("20190301", "20190312", ["KOSPI"])

    assert sorted(first.index) == sorted(second.index) == ["005930"]
    assert "900001" not in second.index
    assert sorted(first.columns) == sorted(second.columns)
    assert first.loc["005930", "net_buy_value"] == second.loc["005930", "net_buy_value"]


# ───────────────── 지수 OHLCV 구간 커버리지 배선 (Task 4) ─────────────────


class _IndexOhlcvFakeStock:
    """get_index_ohlcv 대역 — 호출 횟수를 센다."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def get_index_ohlcv(self, start_ymd, end_ymd, ticker):
        self.calls.append((start_ymd, end_ymd, ticker))
        idx = pd.to_datetime([start_ymd, end_ymd])
        return pd.DataFrame(
            {"시가": [1.0, 1.0], "고가": [2.0, 2.0], "저가": [0.5, 0.5],
             "종가": [1.5, 1.5], "거래량": [10, 10], "거래대금": [100, 100]},
            index=idx,
        )


@pytest.fixture
def _index_ohlcv_store(monkeypatch) -> dict[str, pd.DataFrame]:
    """지수 OHLCV 로컬 저장 대역 — write/read 를 인메모리로 흉내 낸다.

    브리프 원안은 `_store_write_index_ohlcv`/`_store_read_index_ohlcv` 를 패치하지
    않는다. 그러면 이 테스트들이 실제로 `app.services.data.store.indexes` 를 거쳐
    실 DB(index_ohlcv 테이블)에 쓰기·읽기를 그대로 내보낸다 — 이 스위트는 실 DB 를
    쓰지 않는다는 불변(`test_local_store_repo.py` 헤더 참고, "기본 스위트는 실제
    DB 를 쓰지 않는다")을 어긴다. 특히 원격-미스 테스트는 실제로 index_ohlcv 행을
    쓴다. 커버리지 대역과 대칭으로 이 함수도 인메모리로 갈아끼운다.
    """
    store: dict[str, pd.DataFrame] = {}

    def _write(code, df, name):
        if df is not None and not df.empty:
            store[code] = df

    def _read(code, start, end):
        return store.get(code, pd.DataFrame())

    monkeypatch.setattr(F, "_store_write_index_ohlcv", _write)
    monkeypatch.setattr(F, "_store_read_index_ohlcv", _read)
    return store


@pytest.fixture
def _coverage(monkeypatch) -> dict[str, list[tuple[date, date]]]:
    """지수 커버리지 인메모리 대역 — 기록과 조회만 한다.

    실제 병합 규칙(겹침·±1일 인접)은 여기서 재구현하지 않는다. 재구현하면 프로덕션과
    갈릴 수 있고, 그 규칙은 Task 2 의 실 DB 테스트가 이미 지킨다. 이 파일의 관심사는
    fetch.py 가 커버리지 조회·기록을 제대로 배선했는가뿐이다.
    """
    store: dict[str, list[tuple[date, date]]] = {}

    monkeypatch.setattr(F, "_store_read_coverage", lambda code: list(store.get(code, [])))
    monkeypatch.setattr(
        F,
        "_store_merge_coverage",
        # row_count 는 fetch.py 의 부분 응답 휴리스틱 인자다 — 이 픽스처는 배선만
        # 검증하므로 받되 무시하고 항상 기록한다(휴리스틱 자체는 별도 테스트가 본다).
        lambda code, start, end, row_count: store.setdefault(code, []).append((start, end)),
    )
    return store


def test_확보구간에_포함되면_원격을_안_탄다(_store, monkeypatch, _coverage, _index_ohlcv_store):
    """워크포워드 시나리오 — [A,D] 를 확보해 두면 그 안의 [B,C] 는 이미 가진 데이터다.

    구간 커버리지 이전에는 캐시키가 정확히 일치해야 해서 이 요청이 매번 원격을 탔다.
    이 테스트의 책임은 **fetch.py 가 커버리지 조회를 제대로 배선했는가**다. 병합 규칙
    자체는 Task 2 의 실 DB 테스트가 본다 — 여기서 병합을 재구현하면 두 곳이 갈린다.
    """
    fake = _IndexOhlcvFakeStock()
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)
    _coverage["1001"] = [(date(2018, 1, 1), date(2023, 4, 1))]  # [A, D]

    F._fetch_index_ohlcv("20180401", "20230101", "1001")  # [B, C] ⊂ [A, D]

    assert fake.calls == []


def test_확보구간_밖이면_원격을_타고_구간이_기록된다(_store, monkeypatch, _coverage, _index_ohlcv_store):
    fake = _IndexOhlcvFakeStock()
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)

    F._fetch_index_ohlcv("20180401", "20230101", "1001")

    assert len(fake.calls) == 1
    assert _coverage["1001"] == [(date(2018, 4, 1), date(2023, 1, 1))]


def test_확보구간을_하루라도_못_미치면_원격을_탄다(_store, monkeypatch, _coverage, _index_ohlcv_store):
    """경계 이빨 검증 — 확보구간이 요청을 '거의' 덮어도 하루라도 못 미치면 미스다.

    Step 5 가 지정한 이빨 검증(`read_coverage=lambda: []`)은 커버리지를 아예
    비웠을 때만 실패를 잡는다. 이 테스트는 그보다 촘촘하게, 요청 범위를 하루라도
    못 채우는 커버 구간이 로컬 히트로 오판되지 않는지(=원격을 타는지) 확인한다.

    **주의**: 이 구성(`covered_to = end - 1일`)에서는 `_covers()` 의 부등호
    (`t >= end`)를 `t > end` 로 뒤집어도 둘 다 "미스"로 같은 결과를 내 이 테스트로는
    그 부등호 방향의 회귀를 잡지 못한다(실측 확인함 — 뒤집어도 이 테스트는 계속
    통과한다). 부등호 방향 회귀는 정확 경계를 찍는
    `test_확보구간이_요청_끝과_정확히_같으면_히트한다` 가 담당한다.
    """
    fake = _IndexOhlcvFakeStock()
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)
    covered_to = date(2023, 1, 1)
    _coverage["1001"] = [(date(2018, 1, 1), covered_to)]
    end_ymd = (covered_to + timedelta(days=1)).strftime("%Y%m%d")

    F._fetch_index_ohlcv("20180401", end_ymd, "1001")

    assert len(fake.calls) == 1


def test_확보구간이_요청_끝과_정확히_같으면_히트한다(
    _store, monkeypatch, _coverage, _index_ohlcv_store
):
    """정확 경계(covered_to == end) 히트 검증 — `_covers()` 의 부등호 방향을 찍는다.

    위 테스트(`test_확보구간을_하루라도_못_미치면_원격을_탄다`)는 `covered_to = end -
    1일` 구성이라 `t >= end` 와 `t > end` 가 똑같이 "미스"를 내 부등호 방향을
    구분하지 못한다. 이 테스트는 `covered_to == end` 로 두 연산자가 실제로 갈라지는
    지점을 찍는다 — `t >= end` 면 히트(원격 0회), `t > end` 면 미스(원격 1회).

    `_covers()` 의 `t >= end` 를 `t > end` 로 뒤집으면 이 테스트가 실패한다(실측
    확인: 뒤집었더니 `fake.calls == []` 단언이 깨졌고, 원복 후 다시 통과함).
    """
    fake = _IndexOhlcvFakeStock()
    monkeypatch.setattr(F, "_pykrx_stock", lambda: fake)
    end = date(2023, 1, 1)
    _coverage["1001"] = [(date(2018, 1, 1), end)]

    F._fetch_index_ohlcv("20180401", end.strftime("%Y%m%d"), "1001")

    assert fake.calls == []


# ───── _store_merge_coverage: 부분 응답 방어 휴리스틱 ─────


@pytest.fixture
def _merge_spy(monkeypatch):
    """indexes.merge_coverage 호출 여부만 본다 — _store_merge_coverage 는 실제로 돈다."""
    calls: list[tuple] = []
    from app.services.data.store import indexes

    monkeypatch.setattr(indexes, "merge_coverage", lambda *a: calls.append(a))
    return calls


def test_긴_구간에_행이_현저히_적으면_커버리지_기록을_건너뛴다(_merge_spy):
    """5년 구간(기대 거래일 1000+개)에 2행만 오면 명백한 부분 응답이다."""
    F._store_merge_coverage("1001", date(2018, 1, 1), date(2023, 1, 1), 2)

    assert _merge_spy == []


def test_긴_구간에_그럴듯한_행_수면_커버리지를_기록한다(_merge_spy):
    """5년 구간에 900행이면 기대치(대략 1170)의 절반을 넘어 정상 응답으로 본다."""
    start, end = date(2018, 1, 1), date(2023, 1, 1)
    F._store_merge_coverage("1001", start, end, 900)

    assert _merge_spy == [("1001", start, end)]


def test_짧은_구간은_행_수와_무관하게_항상_기록한다(_merge_spy):
    """기대 거래일이 임계(10일) 미만인 짧은 구간은 노이즈가 커 검사하지 않는다."""
    start, end = date(2024, 6, 17), date(2024, 6, 18)  # 이틀 — 행 0이 아닌 1행
    F._store_merge_coverage("1001", start, end, 1)

    assert _merge_spy == [("1001", start, end)]
