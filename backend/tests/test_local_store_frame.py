"""로컬 영구 저장소의 원장·조회 계약 검증.

실제 DB 를 쓰지 않는다. 원장은 InMemoryLedger, 로컬 저장소는 dict 대역으로 갈음하고
"몇 번 외부를 호출했는가"만 본다 — 이 계층의 책임이 그것뿐이기 때문이다.
"""
from app.services.data.store.ledger import InMemoryLedger, LedgerEntry


def test_인메모리_원장은_기록이_없으면_None_을_준다():
    ledger = InMemoryLedger()
    assert ledger.get("fundamentals", "20190312|KOSPI") is None


def test_인메모리_원장은_기록을_되돌려준다():
    ledger = InMemoryLedger()
    ledger.put("fundamentals", "20190312|KOSPI", row_count=812, final=True)
    assert ledger.get("fundamentals", "20190312|KOSPI") == LedgerEntry(
        row_count=812, final=True
    )


def test_인메모리_원장은_0행_기록과_미기록을_구분한다():
    """이 구분이 이 저장소의 존재 이유다 — 휴장일과 미적재가 같은 값이면 안 된다."""
    ledger = InMemoryLedger()
    ledger.put("fundamentals", "20190101|KOSPI", row_count=0, final=True)
    assert ledger.get("fundamentals", "20190101|KOSPI") == LedgerEntry(
        row_count=0, final=True
    )
    assert ledger.get("fundamentals", "20190102|KOSPI") is None


def test_인메모리_원장은_같은_키를_덮어쓴다():
    ledger = InMemoryLedger()
    ledger.put("index_ohlcv", "1001|20260806", row_count=1, final=False)
    ledger.put("index_ohlcv", "1001|20260806", row_count=1, final=True)
    assert ledger.get("index_ohlcv", "1001|20260806").final is True


from datetime import date, timedelta

import pandas as pd
import pytest

from app.services.data.errors import SourceAuthError
from app.services.data.store.frame import cached_frame, is_final_date, make_cache_key


class _Spy:
    """외부 호출 횟수와 로컬 저장을 추적하는 대역."""

    def __init__(self, remote: pd.DataFrame | Exception):
        self.remote = remote
        self.calls = 0
        self.stored: pd.DataFrame | None = None

    def fetch_remote(self) -> pd.DataFrame:
        self.calls += 1
        if isinstance(self.remote, Exception):
            raise self.remote
        return self.remote

    def write_local(self, df: pd.DataFrame) -> None:
        self.stored = df

    def read_local(self) -> pd.DataFrame:
        return self.stored if self.stored is not None else pd.DataFrame()


def _frame(n: int) -> pd.DataFrame:
    return pd.DataFrame({"PER": [10.0] * n})


def test_미적재면_외부를_한_번_호출하고_저장한다():
    ledger = InMemoryLedger()
    spy = _Spy(_frame(3))

    out = cached_frame(
        "fundamentals", "20190312|KOSPI",
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=True, ledger=ledger,
    )

    assert spy.calls == 1
    assert len(out) == 3
    assert ledger.get("fundamentals", "20190312|KOSPI") == LedgerEntry(
        row_count=3, final=True
    )


def test_확정_적재분은_외부를_호출하지_않는다():
    ledger = InMemoryLedger()
    spy = _Spy(_frame(3))
    kwargs = dict(
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=True, ledger=ledger,
    )

    cached_frame("fundamentals", "20190312|KOSPI", **kwargs)
    out = cached_frame("fundamentals", "20190312|KOSPI", **kwargs)

    assert spy.calls == 1  # 두 번째 호출은 로컬에서 읽었다
    assert len(out) == 3


def test_빈_결과는_is_final이_True여도_확정으로_굳지_않는다():
    """§49: 빈 결과는 "소스가 명시적으로 없다고 선언한 경우"에만 확정으로 굳힌다.

    이 함수의 호출자(pykrx 기반 5종)는 그런 명시적 선언 채널이 없어, "진짜 휴장일"
    과 "스키마 변경으로 값을 잃음"을 구분할 수 없다. 과거엔(이 테스트가 원래
    검증하던 계약) 호출자가 넘긴 is_final=True 를 그대로 믿어 0행을 영구 확정했는데,
    이게 Task 8 리뷰가 blocking 으로 지적한 index_members 재발 형태와 같은 함정이라
    계약을 바꿨다 — 이제 row_count==0 이면 is_final 인자와 무관하게 항상 False 로
    내려 다음 호출이 재조회하게 한다.
    """
    ledger = InMemoryLedger()
    spy = _Spy(pd.DataFrame())
    kwargs = dict(
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=True, ledger=ledger,
    )

    cached_frame("fundamentals", "20190101|KOSPI", **kwargs)
    out = cached_frame("fundamentals", "20190101|KOSPI", **kwargs)

    assert spy.calls == 2  # 재조회된다 — 예전엔 1(확정으로 굳혀 재조회 안 함)이었다
    assert out.empty
    entry = ledger.get("fundamentals", "20190101|KOSPI")
    assert entry.final is False
    assert entry.row_count == 0


def test_외부_실패는_빈_프레임이_아니라_예외로_전파된다():
    """§48 의 핵심 계약 — 실패가 값이 되면 호출자가 무시할 수 있다."""
    ledger = InMemoryLedger()
    spy = _Spy(SourceAuthError("krx", "로그인 차단"))

    with pytest.raises(SourceAuthError):
        cached_frame(
            "fundamentals", "20190312|KOSPI",
            read_local=spy.read_local, fetch_remote=spy.fetch_remote,
            write_local=spy.write_local, is_final=True, ledger=ledger,
        )

    assert ledger.get("fundamentals", "20190312|KOSPI") is None  # 실패는 기록하지 않는다


def test_미확정_적재분은_다음_호출에서_재조회된다():
    """당일 시세·미확정 DART 는 저장하되 굳히지 않는다."""
    ledger = InMemoryLedger()
    spy = _Spy(_frame(2))
    kwargs = dict(
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=False, ledger=ledger,
    )

    cached_frame("index_ohlcv", "1001|20260806", **kwargs)
    cached_frame("index_ohlcv", "1001|20260806", **kwargs)

    assert spy.calls == 2


def test_ledger_생략시_기본_구현을_쓴다(monkeypatch):
    """프로덕션 경로가 default_ledger 를 경유하는지 확인한다."""
    fake = InMemoryLedger()
    monkeypatch.setattr(
        "app.services.data.store.frame.default_ledger", lambda: fake
    )
    spy = _Spy(_frame(1))

    cached_frame(
        "market_cap", "20190312|KOSPI",
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=True,
    )

    assert fake.get("market_cap", "20190312|KOSPI") == LedgerEntry(row_count=1, final=True)


def test_is_final_은_콜러블도_받는다():
    """확정 여부가 조회 결과에 달린 호출자가 있다 — 부분 실패면 굳히면 안 된다."""
    ledger = InMemoryLedger()
    spy = _Spy(_frame(2))

    cached_frame(
        "price_change", "20190101|20190131|KOSPI",
        read_local=spy.read_local, fetch_remote=spy.fetch_remote,
        write_local=spy.write_local, is_final=lambda: True, ledger=ledger,
    )

    assert ledger.get("price_change", "20190101|20190131|KOSPI").final is True


def test_is_final_콜러블은_외부조회_뒤에_평가된다():
    """값으로 미리 평가하면 fetch_remote 결과에 의존하는 판단이 항상 틀린다."""
    ledger = InMemoryLedger()
    complete = False

    def _fetch():
        nonlocal complete
        complete = True  # 조회가 끝나야 확정 여부가 정해진다
        return _frame(1)

    cached_frame(
        "price_change", "20190201|20190228|KOSPI",
        read_local=lambda: pd.DataFrame(), fetch_remote=_fetch,
        write_local=lambda df: None, is_final=lambda: complete, ledger=ledger,
    )

    assert ledger.get("price_change", "20190201|20190228|KOSPI").final is True


def test_is_final_date_는_전일까지만_확정으로_본다():
    today = date(2026, 8, 6)
    assert is_final_date(date(2026, 8, 5), today=today) is True
    assert is_final_date(date(2026, 8, 6), today=today) is False
    assert is_final_date(date(2026, 8, 7), today=today) is False


def test_is_final_date_는_today_생략시_KST_기준으로_판정한다(monkeypatch):
    """I2: date.today()(컨테이너 TZ=UTC)가 아니라 KST 로 '오늘'을 계산해야 한다.

    worker.tasks._snapshot_target_date 의 검증(test_worker_snapshots.py)과 같은
    _FixedDateTime 패턴 — UTC 15:30 = KST 익일 00:30 경계에서 KST 날짜 기준으로
    전날까지만 확정되는지 본다. date.today() 를 그대로 썼다면 이 시각의 UTC 날짜는
    아직 8/4 라 8/4 도 "당일"로 미확정 처리됐을 것이다.
    """
    from datetime import datetime, timezone

    from app.services import market as market_mod

    # UTC 2026-08-04 15:30 = KST 2026-08-05 00:30 → KST 날짜는 08-05, UTC 날짜는 08-04.
    fixed_utc = datetime(2026, 8, 4, 15, 30, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_utc.replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

    monkeypatch.setattr(market_mod, "datetime", _FixedDateTime)

    # KST 기준 오늘(08-05)의 전날(08-04)은 확정, 오늘(08-05) 이후는 미확정.
    assert is_final_date(date(2026, 8, 4)) is True
    assert is_final_date(date(2026, 8, 5)) is False


def test_make_cache_key_는_결정적이다():
    assert make_cache_key("20190312", "KOSPI") == "20190312|KOSPI"
    assert make_cache_key("20190312", ["KOSDAQ", "KOSPI"]) == "20190312|KOSDAQ,KOSPI"
    # 순서가 달라도 같은 키가 나와야 한다 — 시장 목록은 정렬된다.
    assert make_cache_key("20190312", ["KOSPI", "KOSDAQ"]) == make_cache_key(
        "20190312", ["KOSDAQ", "KOSPI"]
    )


# ───── cached_range: 범위 조회의 구간 커버리지 계약 ─────

from app.services.data.errors import SourceUnavailableError  # noqa: E402
from app.services.data.store.frame import cached_range, last_final_date  # noqa: E402


class _RangeStore:
    """cached_range 주입 대역 — 호출 횟수와 커버 구간만 본다."""

    def __init__(self, intervals=None, rows=1):
        self.intervals: list[tuple[date, date]] = list(intervals or [])
        self.remote_calls = 0
        self.written: list[int] = []
        self.merged_row_counts: list[int] = []
        self._rows = rows
        self.fail: Exception | None = None

    def read_local(self):
        return pd.DataFrame({"close": [1.0] * self._rows})

    def fetch_remote(self):
        self.remote_calls += 1
        if self.fail is not None:
            raise self.fail
        return pd.DataFrame({"close": [1.0] * self._rows})

    def write_local(self, df):
        self.written.append(len(df))

    def read_coverage(self):
        return list(self.intervals)

    def merge_coverage(self, start, end, row_count):
        self.merged_row_counts.append(row_count)
        if end < start:
            return
        self.intervals.append((start, end))

    def call(self, start, end):
        return cached_range(
            "1001", start, end,
            read_local=self.read_local,
            fetch_remote=self.fetch_remote,
            write_local=self.write_local,
            read_coverage=self.read_coverage,
            merge_coverage=self.merge_coverage,
        )


_PAST_A = date(2020, 1, 1)
_PAST_B = date(2020, 6, 30)


def test_last_final_date_는_KST_전날이다():
    assert last_final_date(today=date(2026, 8, 8)) == date(2026, 8, 7)
    assert is_final_date(date(2026, 8, 7), today=date(2026, 8, 8)) is True
    assert is_final_date(date(2026, 8, 8), today=date(2026, 8, 8)) is False


def test_커버된_구간은_외부를_타지_않는다():
    store = _RangeStore(intervals=[(date(2019, 1, 1), date(2021, 1, 1))])

    store.call(_PAST_A, _PAST_B)

    assert store.remote_calls == 0


def test_부분_커버는_원격을_타고_병합된_뒤_히트한다():
    """워크포워드처럼 창이 밀리며 쌓이는 경우가 이 형태다."""
    store = _RangeStore(intervals=[(_PAST_A, date(2020, 3, 31))])

    store.call(_PAST_A, _PAST_B)
    assert store.remote_calls == 1

    store.call(_PAST_A, _PAST_B)
    assert store.remote_calls == 1  # 병합된 구간이 요청을 덮는다


def test_끝이_오늘이면_커버리지가_있어도_원격을_탄다():
    """당일 봉은 장중 계속 변한다 — 로컬로 줄 수 없다."""
    today = last_final_date() + timedelta(days=1)
    store = _RangeStore(intervals=[(date(2000, 1, 1), date(2100, 1, 1))])

    store.call(_PAST_A, today)

    assert store.remote_calls == 1


def test_빈_결과는_커버리지로_기록하지_않는다():
    """§49 I3 와 같은 판단 — 빈 응답은 '없다는 명시적 선언'이 아니다."""
    store = _RangeStore(rows=0)

    store.call(_PAST_A, _PAST_B)
    store.call(_PAST_A, _PAST_B)

    assert store.intervals == []
    assert store.remote_calls == 2


def test_원격_실패는_클래스를_보존하고_보유_구간을_알려준다():
    store = _RangeStore(intervals=[(_PAST_A, date(2020, 3, 31))])
    store.fail = SourceUnavailableError("krx", "타임아웃")

    with pytest.raises(SourceUnavailableError) as caught:
        store.call(_PAST_A, _PAST_B)

    assert "2020-01-01~2020-03-31" in str(caught.value)
    assert "타임아웃" in str(caught.value)


def test_보유_구간이_없으면_없음이라고_알려준다():
    store = _RangeStore()
    store.fail = SourceUnavailableError("krx", "타임아웃")

    with pytest.raises(SourceUnavailableError) as caught:
        store.call(_PAST_A, _PAST_B)

    assert "없음" in str(caught.value)
