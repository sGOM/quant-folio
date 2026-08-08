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
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 워커 스레드 폴백 경고를 프로세스당 1회로 제한하는 플래그(run_sync docstring 참고).
_fallback_warned = False

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

    정상 경로(실행 중인 이벤트루프 없음)는 asyncio.run 으로 바로 돈다.

    이미 루프 안(async 컨텍스트)에서 불렸다면 asyncio.run 을 그대로 쓰면
    "asyncio.run() cannot be called from a running event loop" 로 죽는다 — 이전
    판의 raise 가드가 원래 하려던 일은 그 예외를 진입점에서 명확히 하는 것뿐이었다
    (가드가 없어도 크래시는 났다. 가드는 원인만 밝혔지 문제를 만들지 않았다).

    이제는 raise 대신 전용 워커 스레드에서 새 루프를 열어 실행한다.
    `local_store_engine` 이 NullPool 이라(이 모듈 상단 docstring 참고) 워커 스레드가
    연 커넥션이 그 스레드의 루프에 묶인 채 풀에 남는 일이 없다 — 이 모듈이 막으려던
    교차 루프(asyncpg "Future attached to a different loop") 문제가 재발하지 않는
    이유가 이것이다. 워커 스레드의 루프는 호출자 루프와 독립적이라 교착도 없다.
    대가는 호출자 루프가 그동안 블로킹된다는 점뿐이다.

    다만 서버 코드(FastAPI 라우트·engine 데몬)에서 이 폴백을 타는 것은 여전히
    잘못이다 — 그쪽은 `asyncio.to_thread`/`run_in_threadpool` 로 감싸 애초에 실행
    중인 루프 밖에서 불러야 한다. 그래서 폴백 진입 시 경고 로그를 남긴다. 정상
    배선된 서버 경로는 전부 이미 스레드에서 호출되므로 이 경고가 뜨지 않는다.
    `backend/scripts/` 의 검증 스크립트처럼 `async def main()` 안에서 직접 부르는
    코드가 이 경고의 정상적인 대상이다. 경고는 **프로세스당 1회**만 남긴다 — 한
    `_fetch_*` 가 원장 조회·읽기/쓰기·원장 기록으로 run_sync 를 2~3회 부르므로,
    리밸런싱 수십 회짜리 스크립트에서는 매번 찍으면 천 줄이 넘어 정작 스크립트
    출력을 덮는다.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # 실행 중인 루프 없음 = 정상 경로

    global _fallback_warned
    if not _fallback_warned:
        _fallback_warned = True
        logger.warning(
            "run_sync 가 실행 중인 이벤트루프 안에서 호출됐다 — 전용 워커 스레드로 "
            "폴백한다(이 경고는 프로세스당 1회만 남긴다). 서버 코드(FastAPI 라우트·"
            "engine 데몬)라면 asyncio.to_thread 로 감싸 애초에 실행 중인 루프 밖에서 "
            "불러야 한다."
        )
    return _run_in_worker_thread(coro)


def _run_in_worker_thread(coro: Coroutine[Any, Any, T]) -> T:
    """coro 를 전용 스레드에서 새 이벤트루프(asyncio.run)로 돌리고 결과를 되돌린다.

    예외도 그대로 재전파한다 — 호출자가 DataSourceError 등을 잡아 처리하는 계약이
    깨지지 않아야 한다.
    """
    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - 호출자에게 그대로 재전파한다
            box["error"] = exc

    # daemon=True: 코루틴이 걸려도 인터프리터 종료를 막지 않는다. join 은 타임아웃 없이
    # 기다린다 — 여기서 중도 포기하면 호출자에게 돌려줄 값이 없고, 스토어 질의는
    # asyncpg 자체 타임아웃과 상위의 bounded_socket_timeout 이 이미 상한을 건다.
    thread = threading.Thread(target=_runner, name="local-store-run-sync", daemon=True)
    thread.start()
    thread.join()

    if "error" in box:
        raise box["error"]
    return box["result"]
