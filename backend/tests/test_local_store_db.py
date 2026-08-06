"""로컬 스토어 전용 엔진의 동기 진입점 가드 검증.

metrics/fetch.py 는 살아있는 메인 루프 아래 asyncio.to_thread 로 도는 동기 코드다.
run_sync 가 이벤트루프 안에서 호출되면 asyncio.run 이 곧바로 터지는데, 그 지점이
스토어 내부 깊은 곳이면 원인 파악이 어렵다. 진입점에서 명시적으로 막는다.
"""
import asyncio

import pytest

from app.core.local_store_db import run_sync


async def _answer() -> int:
    return 42


def test_run_sync_동기_컨텍스트에서_코루틴을_실행한다():
    assert run_sync(_answer()) == 42


@pytest.mark.asyncio
async def test_run_sync_이벤트루프_안에서는_거부한다():
    coro = _answer()
    with pytest.raises(RuntimeError, match="이벤트루프"):
        run_sync(coro)
    coro.close()  # "never awaited" 경고 방지


def test_run_sync_는_워커_스레드에서도_동작한다():
    """to_thread 로 넘어간 워커 스레드에는 실행 중인 루프가 없어 통과해야 한다."""
    result: list[int] = []

    async def _outer():
        result.append(await asyncio.to_thread(lambda: run_sync(_answer())))

    asyncio.run(_outer())
    assert result == [42]
