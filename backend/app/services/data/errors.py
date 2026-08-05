"""외부 데이터 소스 호출 실패를 **원인별로** 표현하는 예외 계층.

왜 소스가 아니라 원인으로 나누는가: 호출자의 관심사는 "KRX 냐 DART 냐"가 아니라
"재시도해도 되나, 사람이 고쳐야 하나"다. 두 축을 모두 타입으로 만들면 조합이 15개가
되므로 소스는 속성(`source`)으로 싣는다. 소스별로 잡아야 하면 `err.source` 로 충분하다.

배경: docs/improvements.md §44-1 — KRX 차단으로 모든 PIT 조회가 0종목을 반환했고
백테스트가 빈 패널 위에서 '성공'하며 무의미한 수치를 냈다. 실패가 값이 아니라 제어
흐름이 되면 호출자가 무시할 수 없다.
"""
from __future__ import annotations

import time
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, time as dtime, timedelta


class DataSourceError(Exception):
    """외부 데이터 소스 호출 실패. 하위를 `except DataSourceError` 로 일괄 처리한다."""

    #: 같은 요청을 다시 보내는 것이 의미 있는가(자동 재시도 로직은 아직 없다 — 후속 작업).
    retryable: bool = False
    #: 이 원인으로 실패했을 때 재조회를 막을 기본 쿨다운(초). None 이면 쿨다운 없음.
    cooldown: float | None = None

    def __init__(self, source: str, message: str, *, retry_after: float | None = None):
        super().__init__(f"[{source}] {message}")
        self.source = source
        self.detail = message
        self.retry_after = retry_after


class SourceAuthError(DataSourceError):
    """인증·권한·차단. 재시도해도 안 풀리고 차단만 악화된다."""

    retryable = False
    cooldown = 300.0


class SourceQuotaError(DataSourceError):
    """호출 한도 초과. 창구가 리셋될 때까지 확정적으로 실패한다.

    리셋 시각을 아는 경우(DART 일일 한도)는 `retry_after` 로 개별 지정한다.
    """

    retryable = False
    cooldown = 300.0


class SourceUnavailableError(DataSourceError):
    """일시 장애(네트워크·타임아웃·5xx·시스템 점검). 재시도가 유효하다."""

    retryable = True
    cooldown = 60.0


class SourceSchemaError(DataSourceError):
    """응답이 우리 파서의 계약과 다르다. 코드 수정이 필요하다.

    쿨다운을 걸지 않는다 — 여기서 기다리면 오히려 문제를 은폐한다.
    """

    retryable = False
    cooldown = None


class SourceRequestError(DataSourceError):
    """우리가 잘못 요청했다(부적절한 파라미터·접근). 버그이므로 쿨다운하지 않는다."""

    retryable = False
    cooldown = None


def classify_httpx(source: str, exc: Exception) -> DataSourceError:
    """httpx 예외를 원인별 DataSourceError 로 변환한다.

    `resp.raise_for_status()` 가 던진 HTTPStatusError 와 연결·타임아웃 계열을 가른다.
    분류가 불확실하면 Unavailable(재시도 가능) 쪽으로 보수적으로 떨어뜨린다.
    """
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return SourceQuotaError(source, f"HTTP 429 요청 한도 초과")
        if code in (401, 403):
            return SourceAuthError(source, f"HTTP {code} 인증·권한 거부")
        if 400 <= code < 500:
            return SourceRequestError(source, f"HTTP {code} 잘못된 요청")
        return SourceUnavailableError(source, f"HTTP {code} 서버 오류")
    if isinstance(exc, httpx.TransportError):  # 타임아웃·연결오류의 공통 조상
        return SourceUnavailableError(source, f"연결 실패: {exc}")
    return SourceUnavailableError(source, f"알 수 없는 오류: {exc}")


# ───────────────────────── 원인별 쿨다운 ─────────────────────────
# 소스 단위로 "언제까지 재조회를 막을지"를 들고 있는다. §44-1 에서 KRX 로그인
# 재시도 폭주가 차단을 악화시켰던 방어를 원인별로 차등화해 일반화한 것.
_cooldown_until: dict[str, float] = {}


def note_failure(exc: DataSourceError) -> None:
    """실패 원인에 맞는 쿨다운을 건다(Schema·Request 는 쿨다운 없음)."""
    secs = exc.retry_after if exc.retry_after is not None else exc.cooldown
    if not secs:
        return
    _cooldown_until[exc.source] = time.monotonic() + secs


def cooldown_remaining(source: str) -> float:
    """해당 소스의 남은 쿨다운(초). 쿨다운 중이 아니면 0.0."""
    return max(0.0, _cooldown_until.get(source, 0.0) - time.monotonic())


def clear_cooldown(source: str) -> None:
    """쿨다운 해제(성공 시·테스트에서 사용)."""
    _cooldown_until.pop(source, None)


#: 원인이 섞였을 때 대표를 고르는 우선순위 — 확정적·심각한 쪽이 앞선다.
_CAUSE_PRIORITY: tuple[type[DataSourceError], ...] = (
    SourceAuthError,
    SourceQuotaError,
    SourceRequestError,
    SourceSchemaError,
    SourceUnavailableError,
)


def representative(errors: Sequence[DataSourceError]) -> DataSourceError:
    """집계 계층이 전량 실패했을 때 올릴 대표 예외를 고른다.

    원인이 섞이면 우선순위 최상위를 고르고, 메시지에 원인별 건수를 담아
    "무엇이 몇 건 실패했는지"가 로그 한 줄로 드러나게 한다.
    """
    if not errors:
        raise ValueError("errors 가 비었다 — 실패가 없으면 대표 예외를 고를 수 없다")
    counts = dict(Counter(type(e).__name__ for e in errors))
    for cls in _CAUSE_PRIORITY:
        picked = next((e for e in errors if isinstance(e, cls)), None)
        if picked is not None:
            return type(picked)(
                picked.source,
                f"{picked.detail} — 총 {len(errors)}건 실패 {counts}",
                retry_after=picked.retry_after,
            )
    return errors[0]


#: 스키마 오류가 '종목별 사정'이 아니라 '응답 형식이 통째로 바뀐 것'이라고 단정하기까지
#: 허용할 연속 실패 수. 1건으로도 사실상 확정이지만, 특정 종목만 이상한 응답을 주는
#: 경우를 배제하지 못하므로 몇 건을 본다.
_SCHEMA_SYSTEMIC_THRESHOLD = 3


def stop_aggregate(source: str, errors: Sequence[DataSourceError], ok: int) -> bool:
    """종목별 집계 루프를 여기서 멈춰야 하는가(부분 결과는 그대로 반환된다).

    두 가지를 본다.

    1. **쿨다운 중**: 더 시도해도 전송 계층이 즉시 같은 차단을 재현할 뿐이고, 그
       자기유발 차단이 `representative()` 의 대표 원인을 오염시킨다(원래 원인이
       Unavailable 이어도 Quota 로 뒤바뀐다).
    2. **쿨다운이 없는 체계적 실패**: Schema·Request 는 기다려도 해결되지 않으므로
       쿨다운을 걸지 않는다(설계). 그래서 1번만 보면 이 원인들은 단락이 **아예 걸리지
       않아**, DART 포맷이 바뀐 날 200종목 루프가 끝까지 돌며 종목당 3회씩 호출을
       소진한 뒤에야 raise 한다 — 일일 20,000건 한도가 있고, 리밸런싱일마다 부르는
       백테스트 한 건이면 한도를 통째로 태울 수 있다.

    2번은 **스키마 오류에만** 적용한다. 스키마 불일치는 우리 파서와 상대 형식이
    어긋난 것이라 종목별로 다를 수 없다. 반면 `SourceRequestError` 는 특정 종목의
    인자 문제로 날 수 있어, 앞 몇 건이 실패했다고 나머지를 포기하면 돌려줄 수 있었던
    부분 결과를 잃는다. 한 번이라도 성공했다면(`ok > 0`) 형식은 맞다는 뜻이므로
    나머지 실패는 종목별 사정으로 보고 계속한다.

    :param ok: 지금까지 예외 없이 응답을 받은 항목 수('성공' = 응답 수신, 데이터 획득 아님).
    """
    if not errors:
        return False
    if cooldown_remaining(source) > 0:
        return True
    if ok == 0 and len(errors) >= _SCHEMA_SYSTEMIC_THRESHOLD:
        recent = errors[-_SCHEMA_SYSTEMIC_THRESHOLD:]
        if all(isinstance(e, SourceSchemaError) for e in recent):
            return True
    return False


def seconds_until_midnight() -> float:
    """다음 자정까지 남은 초. DART 일일 호출 한도(020) 의 리셋 시각 근사.

    컨테이너 로컬 시각 기준이다. DART 한도는 KST 자정 기준이므로 TZ 가 KST 가
    아니면 오차가 생기지만, 쿨다운이 짧아지는 방향이면 재시도가 조금 이를 뿐이고
    길어지는 방향이면 안전측이라 실무상 문제되지 않는다.
    """
    now = datetime.now()
    tomorrow = datetime.combine(now.date() + timedelta(days=1), dtime.min)
    return (tomorrow - now).total_seconds()
