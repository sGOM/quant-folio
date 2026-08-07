"""야간 선적재 태스크 검증 — 외부는 전부 대역, 호출 여부와 실패 집계만 본다."""
from datetime import date

import pytest

from app.services.data.errors import SourceUnavailableError
from worker import tasks


class _FakeRedis:
    """Redis.from_url 대역 — aclose 호출 여부만 기록한다(실제 접속 없음)."""

    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _patch_redis(monkeypatch):
    import redis.asyncio as redis_mod

    fake_redis = _FakeRedis()
    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(lambda cls, url: fake_redis))
    return fake_redis


def _patch_publish_alert(monkeypatch):
    import engine.alerts as alerts_mod

    calls: list[dict] = []

    async def fake_publish_alert(redis, *, user_id, strategy_id, severity, message,
                                  code=None, dedup_window_hours=None):
        calls.append({
            "user_id": user_id, "strategy_id": strategy_id, "severity": severity,
            "message": message, "code": code, "dedup_window_hours": dedup_window_hours,
        })

    monkeypatch.setattr(alerts_mod, "publish_alert", fake_publish_alert)
    return calls


@pytest.fixture(autouse=True)
def _isolate_alert_publishing(monkeypatch):
    """이 파일의 모든 테스트를 실 Redis·`publish_alert`(따라서 실 DB)로부터 격리한다.

    `ingest_daily_snapshots` 는 실패율이 임계(10%)를 넘으면 `_publish_snapshot_alert`
    로 Redis 접속을 열고 `engine.alerts.publish_alert` 를 거쳐 `alerts` 테이블에 행을
    남긴다. 실패 시나리오를 다루는 테스트(예: 1/2 실패)는 이 임계를 쉽게 넘기는데,
    개별 테스트마다 대역을 붙이면 새 테스트가 추가될 때 또 샌다 — 실제로 기존
    `test_일부_실패해도_나머지를_계속한다` 가 대역 없이 이 경로를 타 실 개발 DB 에
    알림 행을 남긴 사고가 있었다. 그래서 파일 전체에 기본 차단을 걸고, 알림 발행을
    검증하는 테스트는 이 픽스처가 돌려주는 `calls` 로 단언한다.
    """
    fake_redis = _patch_redis(monkeypatch)
    calls = _patch_publish_alert(monkeypatch)
    return {"redis": fake_redis, "calls": calls}


def test_전날_확정분을_적재한다(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [("펀더멘털", lambda: called.append("fund")),
                     ("시가총액", lambda: called.append("cap"))],
    )

    out = tasks.ingest_daily_snapshots()

    assert called == ["fund", "cap"]
    assert out == {"date": "20260805", "ok": 2, "failed": 0}


def test_일부_실패해도_나머지를_계속한다(monkeypatch):
    """한 종류가 막혔다고 나머지 선적재를 포기하면 다음 백테스트가 그만큼 더 조회한다."""

    def _boom():
        raise SourceUnavailableError("krx", "차단")

    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [("펀더멘털", _boom), ("시가총액", lambda: None)],
    )

    out = tasks.ingest_daily_snapshots()

    assert out == {"date": "20260805", "ok": 1, "failed": 1}


def test_전량_실패시_알림을_발행한다(monkeypatch, _isolate_alert_publishing):
    """§44-1 재발 시나리오: 6단계 전부 DataSourceError 로 실패해도 조용히 넘어가지 않는다."""

    def _boom():
        raise SourceUnavailableError("krx", "차단")

    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [(f"단계{i}", _boom) for i in range(6)],
    )

    out = tasks.ingest_daily_snapshots()

    calls = _isolate_alert_publishing["calls"]
    assert out == {"date": "20260805", "ok": 0, "failed": 6}
    assert len(calls) == 1
    assert calls[0]["code"] == "snapshot_ingest_failure_rate"
    assert calls[0]["severity"] == "warning"
    assert calls[0]["dedup_window_hours"] == 20.0
    assert calls[0]["user_id"] is None


def test_한_단계만_실패해도_임계를_넘어_알림을_발행한다(monkeypatch, _isolate_alert_publishing):
    """6단계 중 1건(약 17%)만 실패해도 10% 임계를 넘는다 — 한 단계가 하루치 데이터
    종류 하나 전체를 뜻하므로, 종목별 집계와 달리 1건 실패도 무시할 잡음이 아니다."""

    def _boom():
        raise SourceUnavailableError("krx", "차단")

    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [("펀더멘털", _boom)] + [(f"단계{i}", lambda: None) for i in range(5)],
    )

    out = tasks.ingest_daily_snapshots()

    calls = _isolate_alert_publishing["calls"]
    assert out == {"date": "20260805", "ok": 5, "failed": 1}
    assert len(calls) == 1


def test_알림_발행은_run_async_를_거친다(monkeypatch, _isolate_alert_publishing):
    """asyncio.run 으로 직접 감싸면 그 루프에서 연 전역 DB 커넥션이 풀에 반환된 채
    루프가 닫혀, 같은 워커 프로세스의 다음 태스크가 재사용하다 교차 루프 오류로
    죽는다 — 알림 발행은 반드시 `_run_async`(종료 전 전역 엔진 dispose)를 거쳐야
    한다. 실제 교차 루프 재현은 컨테이너에서 별도로 확인한다(단위테스트로는 잡기
    어려움)."""
    calls: list = []
    original = tasks._run_async

    def _spy(coro):
        calls.append(coro)
        return original(coro)

    monkeypatch.setattr(tasks, "_run_async", _spy)

    def _boom():
        raise SourceUnavailableError("krx", "차단")

    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [(f"단계{i}", _boom) for i in range(6)],
    )

    tasks.ingest_daily_snapshots()

    assert len(calls) == 1


def test_전부_성공하면_알림이_없다(monkeypatch, _isolate_alert_publishing):
    monkeypatch.setattr(tasks, "_snapshot_target_date", lambda: date(2026, 8, 5))
    monkeypatch.setattr(
        tasks, "_snapshot_steps",
        lambda ymd: [(f"단계{i}", lambda: None) for i in range(6)],
    )

    out = tasks.ingest_daily_snapshots()

    assert out == {"date": "20260805", "ok": 6, "failed": 0}
    assert _isolate_alert_publishing["calls"] == []


def test_snapshot_steps_는_올바른_함수를_올바른_인자로_배선한다(monkeypatch):
    """_snapshot_steps 본문이 실제로 무엇을 어떤 인자로 부르는지 검증한다.

    기존 두 테스트는 _snapshot_steps 자체를 통째로 대역해 이 함수 본문이
    `return []` 여도 통과한다 — 이 테스트가 그 공백을 메운다. 특히 late binding
    (루프 변수 캡처) 이 깨져 KOSPI/KOSDAQ 이나 1001/2001 이 같은 값으로 뭉개지는
    회귀를 잡는다.
    """
    import app.services.metrics.fetch as fetch_mod

    calls: dict[str, list[tuple]] = {
        "fundamentals": [], "market_cap": [], "market_ohlcv": [], "index_ohlcv": [],
    }

    def fake_fundamentals(as_of_ymd, mkts):
        calls["fundamentals"].append((as_of_ymd, tuple(mkts)))

    def fake_market_cap(as_of_ymd, mkts):
        calls["market_cap"].append((as_of_ymd, tuple(mkts)))

    def fake_market_ohlcv(date_ymd, mkt):
        calls["market_ohlcv"].append((date_ymd, mkt))

    def fake_index_ohlcv(start_ymd, end_ymd, ticker):
        calls["index_ohlcv"].append((start_ymd, end_ymd, ticker))

    monkeypatch.setattr(fetch_mod, "_fetch_fundamentals", fake_fundamentals)
    monkeypatch.setattr(fetch_mod, "_fetch_market_cap", fake_market_cap)
    monkeypatch.setattr(fetch_mod, "_fetch_market_ohlcv_snapshot", fake_market_ohlcv)
    monkeypatch.setattr(fetch_mod, "_fetch_index_ohlcv", fake_index_ohlcv)

    steps = tasks._snapshot_steps("20260805")
    assert len(steps) == 6
    for _name, call in steps:
        call()

    assert calls["fundamentals"] == [("20260805", ("KOSPI", "KOSDAQ"))]
    assert calls["market_cap"] == [("20260805", ("KOSPI", "KOSDAQ"))]
    assert calls["market_ohlcv"] == [("20260805", "KOSPI"), ("20260805", "KOSDAQ")]
    assert calls["index_ohlcv"] == [
        ("20260805", "20260805", "1001"), ("20260805", "20260805", "2001"),
    ]


def test_snapshot_target_date는_KST_기준_전날이다(monkeypatch):
    """컨테이너 TZ(UTC)·celery timezone(Asia/Seoul) 과 무관하게 KST 로 명시 계산한다."""
    from datetime import datetime, timedelta, timezone

    from app.services import market as market_mod

    # UTC 자정 직후(예: 2026-08-05 00:30 UTC = 08-05 09:30 KST) — KST 날짜는 그대로
    # 08-05 이므로 대상일은 전날인 08-04 여야 한다. date.today()(UTC 기준)를 그대로
    # 썼다면 UTC 날짜(08-05)의 전날(08-04)과 우연히 같아 보일 수 있으니, KST 가
    # 다음날로 넘어간 경계 시각으로 확실히 갈라 검증한다.
    # UTC 2026-08-04 15:30 = KST 2026-08-05 00:30 → KST 날짜는 08-05, UTC 날짜는 08-04.
    fixed_utc = datetime(2026, 8, 4, 15, 30, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_utc.replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

    monkeypatch.setattr(market_mod, "datetime", _FixedDateTime)

    result = tasks._snapshot_target_date()

    # KST 기준 오늘은 08-05 → 전날은 08-04. UTC 기준(date.today())이었다면
    # 오늘이 08-04 로 계산돼 전날 08-03 이 나왔을 것이다.
    assert result == date(2026, 8, 4)
