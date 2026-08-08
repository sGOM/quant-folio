"""로컬 스토어 전용 엔진의 동기 진입점(run_sync) 검증.

metrics/fetch.py 는 살아있는 메인 루프 아래 asyncio.to_thread 로 도는 동기 코드다.
run_sync 가 실행 중인 이벤트루프 안에서 불려도(B2: backend/scripts/ 의 22개 검증
스크립트가 async def main() 안에서 직접 이 경로를 태운다) 더는 raise 하지 않고
전용 워커 스레드로 폴백해 정상 실행된다 — 예전 계약(raise)은 그 스크립트들을
하드크래시시켰다.
"""
import asyncio
import logging

import pytest

from app.core.local_store_db import run_sync


async def _answer() -> int:
    return 42


async def _boom() -> int:
    raise ValueError("경계 확인")


def test_run_sync_동기_컨텍스트에서_코루틴을_실행한다():
    assert run_sync(_answer()) == 42


@pytest.mark.asyncio
async def test_run_sync_이벤트루프_안에서도_워커_스레드로_폴백해_실행된다():
    """이전 판은 여기서 RuntimeError 를 던져 async 컨텍스트의 호출자를 크래시시켰다."""
    assert run_sync(_answer()) == 42


def test_run_sync_워커_스레드_폴백에서도_예외가_그대로_전파된다():
    async def _outer():
        with pytest.raises(ValueError, match="경계 확인"):
            run_sync(_boom())

    asyncio.run(_outer())


def test_run_sync_는_워커_스레드에서도_동작한다():
    """to_thread 로 넘어간 워커 스레드에는 실행 중인 루프가 없어 정상 경로로 통과한다."""
    result: list[int] = []

    async def _outer():
        result.append(await asyncio.to_thread(lambda: run_sync(_answer())))

    asyncio.run(_outer())
    assert result == [42]


def test_run_sync_경고는_이벤트루프_안에서_호출될_때만_뜬다(caplog):
    """서버 경로(스레드에서 호출)는 이미 안전해 경고가 뜨면 안 된다 — 눈에 띄어야 하는
    쪽은 async def 안에서 직접 부르는 스크립트다."""
    caplog.set_level(logging.WARNING, logger="app.core.local_store_db")

    async def _outer():
        run_sync(_answer())

    asyncio.run(_outer())
    assert any("run_sync" in r.message for r in caplog.records)

    caplog.clear()
    run_sync(_answer())  # 루프 밖 정상 경로 — 경고가 없어야 한다
    assert not caplog.records
