"""GET /api/screener/turnaround 캐싱 정책 검증.

§56: 재무 하드 필터가 OpenDART 조회 실패로 미적용된(financial_filter_applied=False)
결과는 items 가 비어있지 않아도 캐시하지 않아야 한다 — 그렇지 않으면 필터 없이
나온 왜곡된 후보 목록이 6시간 굳는다. DB·네트워크 불필요.
"""
from __future__ import annotations

from datetime import date

from app.api.routes.screener import turnaround
from app.schemas.screener import TurnaroundCandidate, TurnaroundScreenOut


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):  # noqa: ARG002
        self.store[key] = value


def _result(*, financial_filter_applied: bool) -> TurnaroundScreenOut:
    return TurnaroundScreenOut(
        as_of=date(2026, 8, 19),
        market="ALL",
        scanned=10,
        count=1,
        opendart_enabled=True,
        financial_filter_applied=financial_filter_applied,
        items=[
            TurnaroundCandidate(
                code="000001", name="테스트", market="KOSPI",
                market_cap=100_000_000, avg_value_20=1_000_000.0, score=1.0,
            )
        ],
    )


async def _run_turnaround(monkeypatch, redis, result):
    import app.api.routes.screener as route

    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ARG001
        return result

    monkeypatch.setattr(route.asyncio, "to_thread", _fake_to_thread)
    return await turnaround(_user=None, redis=redis)


async def test_result_with_filter_applied_is_cached(monkeypatch):
    redis = _FakeRedis()
    await _run_turnaround(monkeypatch, redis, _result(financial_filter_applied=True))
    assert len(redis.store) == 1


async def test_result_without_filter_applied_is_not_cached(monkeypatch):
    redis = _FakeRedis()
    await _run_turnaround(monkeypatch, redis, _result(financial_filter_applied=False))
    assert redis.store == {}
