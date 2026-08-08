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
    return last_day < ref


def _now_kst_date() -> date:
    """KST 오늘 날짜. 지연 임포트 — 이 모듈이 app.services.market 에 의존하지
    않아도 되는 다른 호출자(테스트 등)의 임포트 그래프를 가볍게 유지하기 위함."""
    from app.services.market import now_kst

    return now_kst().date()


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
