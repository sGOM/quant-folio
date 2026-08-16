"""로컬 우선 조회의 단일 진입점 — 4상태를 명시적으로 가른다.

§48 이 실패/데이터없음/미설정 셋을 갈랐다면, 로컬 저장소는 네 번째 상태
"아직 적재 안 됨"을 추가한다. 이것이 "데이터 없음"으로 뭉개지면 휴장일마다 외부를
두드리거나(성능), 반대로 미적재를 빈 결과로 오인해 백테스트가 빈 패널 위에서
'성공'한다(정확성) — 후자가 §44-1·§47 에서 실제로 난 사고다.

| 원장 상태            | 동작                                          |
|---------------------|-----------------------------------------------|
| final=True 기록 있음 | read_local() 반환                              |
| 기록 없음/final=False| fetch_remote() 1회 → write_local() + 원장 기록  |
| fetch_remote() 실패  | DataSourceError 그대로 raise(값으로 삼키지 않음) |
| 소스 미설정          | 여기 오기 전에 통과(§48 미설정 경로 불변)        |

## 빈 결과를 확정으로 굳히는 조건(원칙, §49)

**빈 결과는 "소스가 명시적으로 없다고 선언한 경우"에만 확정으로 굳힌다.** 그 외에는
호출자가 `is_final=True`(또는 그렇게 평가되는 콜러블)를 넘겨도 `row_count==0`이면
`final=False`로 내려 다음 호출이 재조회하게 한다.

이유: 이 함수의 호출자(pykrx 기반 5종 — 펀더멘털·시가총액·기간등락률·순매수·
전종목/지수 OHLCV)는 "정상 응답인데 스키마가 바뀌어 값이 빈 경우"와 "진짜 휴장일/
무자료"를 구분할 채널이 없다(`krx_index.index_members`의 27번째 줄 주석과 같은
문제). 전자를 `final=True`로 굳히면 프로세스 재시작으로도 회복되지 않고 수동 DB
삭제 전까지 영구히 0행으로 고착된다(§47 재발 형태) — Task 8 리뷰가 blocking 으로
지적한 것과 정확히 같은 형태가 코어에도 있었다.

유일한 예외는 소스가 상태 코드 등으로 "무자료"를 **명시적으로 선언**하는 경우다
(OpenDART status 013). 그 경로는 이 함수(`cached_frame`)를 쓰지 않고 자체 저장소
(`dart_store`)에서 별도로 확정 기록한다 — 여기서 예외 처리를 두지 않는다.

트레이드오프: 진짜 휴장일·무실적 구간도 매 호출마다 재조회하게 되지만(pykrx 왕복
비용), 잘못 굳혀 영구 0행이 되는 쪽이 비교할 수 없이 위험하다. 백테스트는 거래일만
순회하므로 실비용은 작다.

**한계**: 이 가드는 **쓰기 시점**에만 작동한다. 이미 `final=True, row_count=0` 으로
기록된 원장 행은 되돌리지 못하므로, 위 표의 첫 행(final=True → read_local() 반환)에
걸려 계속 빈 결과를 돌려준다. 그런 행이 남을 수 있는 경로는 두 가지다 — ① 이 가드
이전 판이 굳힌 것, ② 시장 태그(`market`) 없이 적재된 행을 시장 필터가 걸러내
로컬이 0행이 되는 경우(§49 B1). 어느 쪽이든 해당 `external_fetches` 행을 지우면
다음 호출이 재조회한다. 이 저장소는 빈 상태에서 시작해야 한다(§49).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta

import pandas as pd

from app.services.data.errors import DataSourceError
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

    기준 '오늘'은 KST 로 계산한다(`app.services.market.now_kst`). KRX 거래일은 KST
    기준인데, 컨테이너 TZ 는 UTC 이고 celery_app.timezone(="Asia/Seoul")은 beat
    스케줄 표시에만 적용될 뿐 `date.today()`에는 영향을 주지 않는다
    (`worker.tasks._snapshot_target_date` 의 docstring 과 같은 취지). UTC 로 뒀다면
    ① 00:00-09:00 KST 사이의 "어제" 조회가 전부 미확정으로 남아 매번 재조회되고,
    ② 야간 배치를 KST 새벽대로 옮기면 UTC 날짜가 아직 전날이라 `is_final_date` 가
    전부 False 를 돌려줘 배치가 에러 없이 아무것도 확정하지 못하는 조용한 회귀가
    난다. UTC 날짜는 KST 날짜보다 항상 같거나 이르므로(반대는 없음) 이 함수가
    과확정(미래를 확정으로 굳힘)할 위험은 없다 — 잘못돼도 항상 "미확정" 쪽으로만
    틀린다.
    """
    ref = today or _now_kst_date()
    return last_day <= last_final_date(today=ref)


def _now_kst_date() -> date:
    """KST 오늘 날짜. 지연 임포트 — 이 모듈이 app.services.market 에 의존하지
    않아도 되는 다른 호출자(테스트 등)의 임포트 그래프를 가볍게 유지하기 위함."""
    from app.services.market import now_kst

    return now_kst().date()


def last_final_date(*, today: date | None = None) -> date:
    """확정으로 봐도 되는 마지막 날짜 — KST 기준 전날.

    `is_final_date` 와 같은 기준을 두 곳에서 따로 계산하면 언젠가 어긋난다. 범위형
    조회(`cached_range`)는 "어디까지 확정인가"를 **값으로** 필요로 하므로(커버 구간을
    거기서 자른다) 여기 한 곳에 두고 `is_final_date` 도 이것을 쓴다.
    """
    ref = today or _now_kst_date()
    return ref - timedelta(days=1)


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
        단, 결과가 0행이면 이 값과 무관하게 항상 `final=False`로 내린다(모듈 docstring
        "빈 결과를 확정으로 굳히는 조건" 참고) — 이 함수의 호출자들에게는 무자료를
        명시적으로 선언하는 채널이 없다.
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
    row_count = len(df)
    if row_count == 0:
        # §49: 빈 결과는 "소스가 명시적으로 없다고 선언한 경우"에만 확정으로 굳힌다.
        # 이 함수의 호출자(펀더멘털·시가총액·기간등락률/순매수·OHLCV)는 그런 명시적
        # 선언 채널이 없어, 예외 없이 빈 프레임이 오면 "진짜 무자료"인지 "스키마
        # 변경으로 값을 잃음"인지 구분할 수 없다. 잘못 굳히면 다음 호출부터 이
        # cache_key 를 영구히 0행으로 취급해 수동 DB 삭제 전까지 회복되지 않는다
        # (index_members 의 §47 재발 형태와 동일). 트레이드오프: 진짜 휴장일·무실적
        # 구간도 매 호출 재조회하지만, 백테스트는 거래일만 순회하므로 실비용은 작다.
        final = False
    led.put(source, cache_key, row_count=row_count, final=final)
    logger.debug(
        "로컬 적재: %s %s rows=%d final=%s", source, cache_key, len(df), final
    )
    return df


def _covers(intervals: list[tuple[date, date]], start: date, end: date) -> bool:
    """확보 구간 중 [start, end] 를 통째로 포함하는 것이 있는가.

    구간들의 합집합이 아니라 **개별 구간**을 본다. 합집합으로 판정하려면 사이의
    빈틈이 휴장일인지 미적재인지 알아야 하는데, 그 판단에는 거래일 달력이 필요하다.
    """
    return any(f <= start and t >= end for f, t in intervals)


def _with_coverage_hint(
    exc: DataSourceError, intervals: list[tuple[date, date]]
) -> DataSourceError:
    """실패 예외에 로컬 확보 구간을 덧붙여 **같은 클래스로** 다시 만든다.

    사용자가 기간을 좁혀 재시도할 근거를 응답 본문(`app/main.py` 가 502/503 으로
    변환한다)에서 바로 보게 하려는 것이다. 기간을 대신 축소하지는 않는다 — 구간이
    바뀐 성과를 같은 것으로 착각하는 함정은 이 저장소가 이미 데인 적이 있다.

    `errors.py` 의 예외 계층이 전부 `(source, message, retry_after=)` 시그니처를
    공유하는 것에 기댄다. 쿨다운·실패 집계는 `fetch_remote()` 안에서 이미 기록됐으므로
    인스턴스를 바꿔도 영향이 없다.
    """
    if intervals:
        newest = sorted(intervals, key=lambda iv: iv[1], reverse=True)
        shown = ", ".join(f"{f:%Y-%m-%d}~{t:%Y-%m-%d}" for f, t in newest[:3])
        if len(newest) > 3:
            shown += f" …외 {len(newest) - 3}건"
    else:
        shown = "없음"
    return type(exc)(
        exc.source,
        f"{exc.detail} — 로컬 확보 구간: {shown}",
        retry_after=exc.retry_after,
    )


def cached_range(
    key: str,
    start: date,
    end: date,
    *,
    read_local: Callable[[], pd.DataFrame],
    fetch_remote: Callable[[], pd.DataFrame],
    write_local: Callable[[pd.DataFrame], None],
    read_coverage: Callable[[], list[tuple[date, date]]],
    merge_coverage: Callable[[date, date, int], None],
) -> pd.DataFrame:
    """범위 조회의 로컬 우선 진입점 — 확보 구간이 요청을 덮으면 외부를 타지 않는다.

    `cached_frame` 의 형제다. 차이는 게이트가 캐시키 정확일치가 아니라 **구간 포함**
    이라는 점 하나다. 범위 키 소스(지수 OHLCV)는 요청 범위가 조금만 달라도 원장
    키가 어긋나 이미 가진 행을 못 쓰는 문제가 있었다.

    커버 구간으로 기록하는 것은 **요청 범위**이지 받아온 행의 범위가 아니다 —
    `[start, end]` 에 대해 소스가 정상 응답했으면 그 창 안은 전부 받은 것이므로,
    거래일 달력 없이도 갭 없음을 말할 수 있다.

    :param key: 로그·디버깅용 식별자(지수코드). 게이트 판정에는 쓰지 않는다 —
        커버 구간 조회 자체가 `read_coverage` 콜러블에 이미 묶여 있다.
    :raises DataSourceError: 외부 조회가 실패했을 때. `detail` 에 로컬 확보 구간을
        덧붙여 올린다. **빈 프레임으로 삼키지 않는다.**
    """
    final_through = last_final_date()

    if end <= final_through and _covers(read_coverage(), start, end):
        logger.debug("로컬 커버 히트: %s %s~%s", key, start, end)
        return read_local()

    try:
        df = fetch_remote()
    except DataSourceError as e:
        raise _with_coverage_hint(e, read_coverage()) from e

    if df is None:
        df = pd.DataFrame()
    write_local(df)

    if not df.empty:
        # 빈 결과는 커버리지로 기록하지 않는다 — "없다는 명시적 선언"이 아니라
        # 스키마 변동으로 값을 잃은 것일 수도 있다(cached_frame 의 0행 가드와 같은 판단).
        # row_count 는 그대로 전달만 한다 — "이 수가 이 구간에 그럴듯한가"는 소스마다
        # 다른 도메인 지식(예: 거래일 달력)이라 이 함수(소스 불가지)가 판단하지 않는다.
        merge_coverage(start, min(end, final_through), len(df))

    logger.debug("원격 조회: %s %s~%s rows=%d", key, start, end, len(df))
    return df
