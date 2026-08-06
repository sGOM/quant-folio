"""로컬 우선 조회의 단일 진입점 — 4상태를 명시적으로 가른다.

§48 이 실패/데이터없음/미설정 셋을 갈랐다면, 로컬 저장소는 네 번째 상태
"아직 적재 안 됨"을 추가한다. 이것이 "데이터 없음"으로 뭉개지면 휴장일마다 외부를
두드리거나(성능), 반대로 미적재를 빈 결과로 오인해 백테스트가 빈 패널 위에서
'성공'한다(정확성) — 후자가 §44-1·§47 에서 실제로 난 사고다.

| 원장 상태            | 동작                                          |
|---------------------|-----------------------------------------------|
| final=True 기록 있음 | read_local() 반환. 0행이면 0행 그대로(진짜 없음) |
| 기록 없음/final=False| fetch_remote() 1회 → write_local() + 원장 기록  |
| fetch_remote() 실패  | DataSourceError 그대로 raise(값으로 삼키지 않음) |
| 소스 미설정          | 여기 오기 전에 통과(§48 미설정 경로 불변)        |
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import pandas as pd

from app.services.data.store.ledger import Ledger, default_ledger

logger = logging.getLogger("app.services.data.store")


def make_cache_key(*parts: object) -> str:
    """조회 인자를 결정적인 문자열로 직렬화한다.

    같은 인자가 언제나 같은 키를 만들어야 원장이 제 구실을 한다. 목록형 인자
    (시장 목록·투자자군)는 호출 순서가 달라도 같은 조회이므로 정렬한다.
    """
    out: list[str] = []
    for p in parts:
        if isinstance(p, (list, tuple, set, frozenset)):
            out.append(",".join(sorted(str(x) for x in p)))
        elif isinstance(p, date):
            out.append(p.strftime("%Y%m%d"))
        else:
            out.append(str(p))
    return "|".join(out)


def is_final_date(last_day: date, *, today: date | None = None) -> bool:
    """그 날짜의 시장데이터를 영구 확정으로 봐도 되는가.

    전일까지만 확정으로 본다. 당일분은 장중 값이 계속 바뀌므로 굳히면 안 된다.
    """
    ref = today or date.today()
    return last_day < ref


def cached_frame(
    source: str,
    cache_key: str,
    *,
    read_local: Callable[[], pd.DataFrame],
    fetch_remote: Callable[[], pd.DataFrame],
    write_local: Callable[[pd.DataFrame], None],
    is_final: bool | Callable[[], bool],
    ledger: Ledger | None = None,
) -> pd.DataFrame:
    """로컬에 있으면 로컬에서, 없으면 외부에서 1회 가져와 영구 저장한다.

    :param source: 조회 종류(fundamentals·market_cap·index_ohlcv 등)
    :param cache_key: make_cache_key 로 만든 결정적 인자 키
    :param is_final: 이 결과를 영구 확정으로 굳혀도 되는가(당일분·미확정 DART 는 False).
        콜러블을 넘기면 `fetch_remote()` 가 끝난 **뒤에** 평가한다 — 확정 여부가 조회
        결과(부분 실패 여부)에 달린 호출자가 있어, 값으로 미리 평가하면 항상 틀린다.
    :raises DataSourceError: 외부 조회가 실패했을 때. **빈 프레임으로 삼키지 않는다.**
    """
    led = ledger if ledger is not None else default_ledger()

    entry = led.get(source, cache_key)
    if entry is not None and entry.final:
        return read_local()

    df = fetch_remote()  # 실패는 DataSourceError 로 그대로 올라간다
    if df is None:
        df = pd.DataFrame()

    write_local(df)
    # 확정 여부는 조회가 끝난 지금 평가한다 — 부분 실패 여부에 달린 호출자가 있다.
    final = is_final() if callable(is_final) else is_final
    led.put(source, cache_key, row_count=len(df), final=final)
    logger.debug(
        "로컬 적재: %s %s rows=%d final=%s", source, cache_key, len(df), final
    )
    return df
