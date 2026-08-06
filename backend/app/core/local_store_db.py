"""로컬 영구 저장소 전용 DB 엔진 — 동기 스레드에서 안전하게 쓰기 위한 별도 풀.

왜 app.core.database 의 전역 엔진을 쓰지 않는가:

metrics/fetch.py 는 동기 함수이고 호출자가 asyncio.to_thread 로 실행한다. 즉 메인
이벤트루프가 살아있는 채로 워커 스레드에서 돈다. 그 스레드에서 asyncio.run 을 쓰면
루프가 매번 새로 만들어지는데, asyncpg 커넥션은 루프에 묶여 있어 전역 풀에 남은
커넥션을 다음 루프가 재사용하면 "Future attached to a different loop" 로 죽는다
(worker/tasks.py:21-39 가 겪고 dispose 로 막은 잠복 버그).

worker 의 해법(실행 끝에 engine.dispose())은 여기서 못 쓴다 — 메인 루프가 쓰던
커넥션까지 끊어버린다. 그래서 NullPool 전용 엔진을 따로 둔다. 풀링을 하지 않으므로
매 호출이 제 루프의 새 커넥션을 열고 닫아, 교차 루프 재사용이 원천적으로 불가능하다.
호출 빈도가 리밸런싱 날짜 단위라 커넥션 수립 비용은 무시할 수준이다.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings

T = TypeVar("T")

local_store_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    poolclass=NullPool,
)

LocalStoreSession = async_sessionmaker(
    bind=local_store_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """동기 컨텍스트(워커 스레드)에서 스토어 코루틴을 실행한다.

    이벤트루프 안에서 부르면 asyncio.run 이 어차피 터지지만, 그 예외는 스토어 내부
    깊은 곳에서 나와 원인이 흐려진다. 진입점에서 무엇이 잘못됐는지 말하고 막는다.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # 실행 중인 루프 없음 = 정상 경로
    else:
        raise RuntimeError(
            "run_sync 는 이벤트루프 안에서 호출할 수 없다. "
            "async 컨텍스트라면 코루틴을 직접 await 하라."
        )
    return asyncio.run(coro)
