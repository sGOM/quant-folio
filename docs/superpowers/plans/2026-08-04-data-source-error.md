# 외부 데이터 소스 조용한 실패 제거 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `krx_index`·`opendart`·`kofia` 의 외부 호출 실패가 빈 값으로 감춰지지 않고 원인별 예외로 호출자에게 전달되게 한다.

**Architecture:** 전송 계층(HTTP 1회 왕복)은 실패 시 무조건 원인별 `DataSourceError` 를 raise 한다. 집계 계층(종목·날짜 루프)은 실패를 세다가 **성공이 0건이면** 대표 예외를 raise 하고, 하나라도 성공하면 부분 결과를 돌려준다. 호출자는 결과 수치를 오염시키는 경로면 전파하고, 부가 정보 경로면 `except DataSourceError` 로 좁혀 잡고 ERROR 로그를 남긴다.

**Tech Stack:** Python 3.12, httpx(동기 `Client`), pykrx 인증 세션(requests 기반), pytest, FastAPI

**설계 근거:** `docs/superpowers/specs/2026-08-04-external-api-silent-failure-design.md`

## Global Constraints

- 주석·docstring·커밋 메시지는 **한국어**(`docs/CONVENTIONS.md`).
- 테스트는 컨테이너 안에서 실행: `docker compose exec web pytest <경로>`.
- 백엔드는 핫리로드가 없다. 수동 확인이 필요하면 `docker compose restart web`.
- **"성공"은 예외 없이 응답을 받은 것**이지 데이터를 얻은 것이 아니다. 이 정의를 뒤집으면 과거 구간 무자료 종목 때문에 정상 백테스트가 죽는다.
- **캐시 정책은 건드리지 않는다.** 세 모듈 모두 이미 "성공만 캐시"다.
- DART status `013`(조회된 데이터 없음)과 미설정(`is_enabled()` False, `KRX_ID/PW` 공백)은 **실패가 아니다**. 예외를 던지지 않는다.
- 자동 백오프 재시도는 **범위 밖**이다. `retryable` 속성만 깔고 로직은 만들지 않는다.

---

### Task 1: 예외 계층과 쿨다운 레지스트리

**Files:**
- Create: `backend/app/services/data/errors.py`
- Test: `backend/tests/test_data_errors.py`

**Interfaces:**
- Consumes: 없음(최초 태스크)
- Produces:
  - `DataSourceError(source: str, message: str, *, retry_after: float | None = None)` — 속성 `source`, `detail`, `retry_after`, 클래스 속성 `retryable: bool`, `cooldown: float | None`
  - 하위: `SourceAuthError`, `SourceQuotaError`, `SourceUnavailableError`, `SourceSchemaError`, `SourceRequestError`
  - `classify_httpx(source: str, exc: Exception) -> DataSourceError`
  - `note_failure(exc: DataSourceError) -> None`
  - `cooldown_remaining(source: str) -> float`
  - `clear_cooldown(source: str) -> None`
  - `representative(errors: list[DataSourceError]) -> DataSourceError`
  - `seconds_until_midnight() -> float`

- [ ] **Step 1: 실패 테스트 작성**

`backend/tests/test_data_errors.py` 를 새로 만든다:

```python
"""외부 데이터 소스 예외 계층 — 원인 분류·쿨다운·대표 예외 선정 검증."""
import httpx
import pytest

from app.services.data.errors import (
    DataSourceError,
    SourceAuthError,
    SourceQuotaError,
    SourceRequestError,
    SourceSchemaError,
    SourceUnavailableError,
    classify_httpx,
    clear_cooldown,
    cooldown_remaining,
    note_failure,
    representative,
)


@pytest.fixture(autouse=True)
def _clear():
    for src in ("krx", "dart", "kofia"):
        clear_cooldown(src)
    yield
    for src in ("krx", "dart", "kofia"):
        clear_cooldown(src)


class TestHierarchy:
    def test_모든_하위는_DataSourceError로_잡힌다(self):
        for cls in (SourceAuthError, SourceQuotaError, SourceUnavailableError,
                    SourceSchemaError, SourceRequestError):
            with pytest.raises(DataSourceError):
                raise cls("krx", "테스트")

    def test_source_와_detail_이_보존된다(self):
        e = SourceAuthError("dart", "키 거부")
        assert e.source == "dart"
        assert e.detail == "키 거부"
        assert "dart" in str(e) and "키 거부" in str(e)

    def test_일시장애만_retryable(self):
        assert SourceUnavailableError("krx", "x").retryable is True
        for cls in (SourceAuthError, SourceQuotaError, SourceSchemaError, SourceRequestError):
            assert cls("krx", "x").retryable is False


class TestClassifyHttpx:
    @staticmethod
    def _status_error(code: int) -> httpx.HTTPStatusError:
        req = httpx.Request("GET", "https://example.test")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError("boom", request=req, response=resp)

    def test_429는_Quota(self):
        assert isinstance(classify_httpx("dart", self._status_error(429)), SourceQuotaError)

    @pytest.mark.parametrize("code", [401, 403])
    def test_401_403은_Auth(self, code):
        assert isinstance(classify_httpx("dart", self._status_error(code)), SourceAuthError)

    def test_기타_4xx는_Request(self):
        assert isinstance(classify_httpx("dart", self._status_error(400)), SourceRequestError)

    def test_5xx는_Unavailable(self):
        assert isinstance(classify_httpx("dart", self._status_error(503)), SourceUnavailableError)

    def test_타임아웃은_Unavailable(self):
        exc = httpx.ReadTimeout("timed out")
        assert isinstance(classify_httpx("dart", exc), SourceUnavailableError)

    def test_알수없는_예외도_Unavailable(self):
        assert isinstance(classify_httpx("dart", RuntimeError("???")), SourceUnavailableError)


class TestCooldown:
    def test_Auth는_300초_쿨다운(self):
        note_failure(SourceAuthError("krx", "차단"))
        assert 290 < cooldown_remaining("krx") <= 300

    def test_Unavailable은_60초_쿨다운(self):
        note_failure(SourceUnavailableError("krx", "타임아웃"))
        assert 50 < cooldown_remaining("krx") <= 60

    def test_Schema와_Request는_쿨다운_없음(self):
        note_failure(SourceSchemaError("krx", "키 없음"))
        note_failure(SourceRequestError("krx", "잘못된 파라미터"))
        assert cooldown_remaining("krx") == 0.0

    def test_retry_after가_클래스_기본값을_이긴다(self):
        note_failure(SourceQuotaError("dart", "일일 한도", retry_after=7200))
        assert 7100 < cooldown_remaining("dart") <= 7200

    def test_소스별로_격리된다(self):
        note_failure(SourceAuthError("krx", "차단"))
        assert cooldown_remaining("dart") == 0.0


class TestRepresentative:
    def test_Auth가_Unavailable보다_우선(self):
        errs = [SourceUnavailableError("krx", "a"), SourceAuthError("krx", "b"),
                SourceUnavailableError("krx", "c")]
        assert isinstance(representative(errs), SourceAuthError)

    def test_대표_메시지에_총_실패건수가_담긴다(self):
        errs = [SourceUnavailableError("krx", "a"), SourceAuthError("krx", "b")]
        assert "2건" in str(representative(errs))

    def test_빈_목록은_ValueError(self):
        with pytest.raises(ValueError):
            representative([])
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `docker compose exec web pytest tests/test_data_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.data.errors'`

- [ ] **Step 3: 최소 구현 작성**

`backend/app/services/data/errors.py` 를 새로 만든다:

```python
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


def representative(errors: list[DataSourceError]) -> DataSourceError:
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


def seconds_until_midnight() -> float:
    """다음 자정까지 남은 초. DART 일일 호출 한도(020) 의 리셋 시각 근사.

    컨테이너 로컬 시각 기준이다. DART 한도는 KST 자정 기준이므로 TZ 가 KST 가
    아니면 오차가 생기지만, 쿨다운이 짧아지는 방향이면 재시도가 조금 이를 뿐이고
    길어지는 방향이면 안전측이라 실무상 문제되지 않는다.
    """
    now = datetime.now()
    tomorrow = datetime.combine(now.date() + timedelta(days=1), dtime.min)
    return (tomorrow - now).total_seconds()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `docker compose exec web pytest tests/test_data_errors.py -v`
Expected: PASS (20건 내외 전부)

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/errors.py backend/tests/test_data_errors.py
git commit -m "feat: 외부 데이터 소스 원인별 예외 계층과 쿨다운 레지스트리

- 소스가 아니라 원인(Auth·Quota·Unavailable·Schema·Request)으로 나눈다 — 호출자의 관심사가 '재시도해도 되나'이고 두 축을 다 타입으로 만들면 조합이 15개가 된다
- 원인별 쿨다운 차등: Auth 300s(재시도해도 차단만 악화), Unavailable 60s, Schema·Request 는 없음(기다리면 문제를 은폐한다)
- 집계 계층 전량 실패 시 대표 예외를 우선순위로 고르고 원인별 건수를 메시지에 담는다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `krx_index` 전송 계층 — 원인별 raise

**Files:**
- Modify: `backend/app/services/data/krx_index.py:75-113` (`_session`), `:143-148`, `:182-187`, `:229-234`, `:300-305`, `:503-508` (POST 5곳)
- Test: `backend/tests/test_krx_index.py`

**Interfaces:**
- Consumes: Task 1 의 `SourceAuthError`, `SourceUnavailableError`, `SourceSchemaError`, `SourceRequestError`, `note_failure`, `cooldown_remaining`, `clear_cooldown`
- Produces:
  - `_krx_rows(sess, payload: dict, key: str, label: str, timeout: float = 20) -> list[dict]` — POST 1회, 실패 시 raise
  - `_session()` — 미설정이면 `None`, 실패·쿨다운이면 `SourceAuthError`
  - `has_krx_auth() -> bool` — preflight 용(예외를 던지지 않음)

**주의:** 기존 `_SESSION_FAIL_COOLDOWN`/`_session_fail_until` 모듈 전역을 Task 1 의 레지스트리로 이관한다. 이를 검증하던 `tests/test_krx_index.py::TestSessionFailureCooldown` 3건이 그 전역을 직접 만지므로 **같이 고쳐야 한다**(삭제가 아니라 레지스트리 기준으로 재작성).

- [ ] **Step 1: 실패 테스트 작성**

`backend/tests/test_krx_index.py` 끝에 추가한다:

```python
class TestTransportErrors:
    """전송 계층 — KRX 는 에러 코드가 없어 응답 형태로 원인을 가른다."""

    @staticmethod
    def _sess(post):
        class _S:
            pass
        s = _S()
        s.post = post
        return s

    def test_HTTP200_비JSON은_차단으로_보고_Auth(self, monkeypatch):
        """§44-1 에서 실제 관측한 동작 — 차단 시 JSON 이 아닌 HTML 안내 페이지가 온다."""
        class _Html:
            status_code = 200

            def json(self):
                raise ValueError("Expecting value: line 1 column 1")

        sess = self._sess(lambda *a, **kw: _Html())
        with pytest.raises(SourceAuthError):
            krx_index._krx_rows(sess, {"trdDd": "20260801"}, "output", "구성종목")

    def test_연결실패는_Unavailable(self):
        def _boom(*a, **kw):
            raise OSError("connection reset")

        with pytest.raises(SourceUnavailableError):
            krx_index._krx_rows(self._sess(_boom), {}, "output", "구성종목")

    def test_5xx는_Unavailable(self):
        class _R:
            status_code = 503

            def json(self):
                return {}

        with pytest.raises(SourceUnavailableError):
            krx_index._krx_rows(self._sess(lambda *a, **kw: _R()), {}, "output", "구성종목")

    def test_4xx는_Request(self):
        class _R:
            status_code = 400

            def json(self):
                return {}

        with pytest.raises(SourceRequestError):
            krx_index._krx_rows(self._sess(lambda *a, **kw: _R()), {}, "output", "구성종목")

    def test_기대_키_부재는_Schema(self):
        class _R:
            status_code = 200

            def json(self):
                return {"unexpected": []}

        with pytest.raises(SourceSchemaError):
            krx_index._krx_rows(self._sess(lambda *a, **kw: _R()), {}, "output", "구성종목")

    def test_정상JSON_빈리스트는_예외가_아니다(self):
        """휴장일 — 소스가 '없다'고 정상 응답한 것이라 실패가 아니다."""
        class _R:
            status_code = 200

            def json(self):
                return {"output": []}

        assert krx_index._krx_rows(self._sess(lambda *a, **kw: _R()), {}, "output", "구성종목") == []


class TestSessionAuth:
    """미설정과 실패를 가른다 — 둘 다 None 이던 것이 §44-1 재발 경로였다."""

    def test_자격증명_미설정이면_None(self, monkeypatch):
        monkeypatch.setattr(krx_index.settings, "KRX_ID", "")
        monkeypatch.setattr(krx_index.settings, "KRX_PW", "")
        assert krx_index._session() is None

    def test_로그인_실패는_Auth_예외(self, monkeypatch):
        def _boom():
            raise RuntimeError("로그인 실패")

        monkeypatch.setattr(krx_index.settings, "KRX_ID", "id")
        monkeypatch.setattr(krx_index.settings, "KRX_PW", "pw")
        monkeypatch.setattr(krx_index, "_build_session", _boom)
        with pytest.raises(SourceAuthError):
            krx_index._session()

    def test_빈_세션도_실패로_본다(self, monkeypatch):
        monkeypatch.setattr(krx_index.settings, "KRX_ID", "id")
        monkeypatch.setattr(krx_index.settings, "KRX_PW", "pw")
        monkeypatch.setattr(krx_index, "_build_session", lambda: None)
        with pytest.raises(SourceAuthError):
            krx_index._session()

    def test_실패_후_쿨다운이_걸린다(self, monkeypatch):
        def _boom():
            raise RuntimeError("로그인 실패")

        monkeypatch.setattr(krx_index.settings, "KRX_ID", "id")
        monkeypatch.setattr(krx_index.settings, "KRX_PW", "pw")
        monkeypatch.setattr(krx_index, "_build_session", _boom)
        with pytest.raises(SourceAuthError):
            krx_index._session()
        assert cooldown_remaining("krx") > 0

    def test_쿨다운_중에는_로그인을_시도하지_않는다(self, monkeypatch):
        monkeypatch.setattr(krx_index.settings, "KRX_ID", "id")
        monkeypatch.setattr(krx_index.settings, "KRX_PW", "pw")
        note_failure(SourceAuthError("krx", "차단"))
        calls = []
        monkeypatch.setattr(krx_index, "_build_session", lambda: calls.append(1))
        with pytest.raises(SourceAuthError):
            krx_index._session()
        assert calls == []

    def test_has_krx_auth_는_예외를_던지지_않는다(self, monkeypatch):
        monkeypatch.setattr(krx_index.settings, "KRX_ID", "")
        monkeypatch.setattr(krx_index.settings, "KRX_PW", "")
        assert krx_index.has_krx_auth() is False
```

파일 상단 import 에 다음을 추가한다:

```python
from app.services.data.errors import (
    SourceAuthError,
    SourceRequestError,
    SourceSchemaError,
    SourceUnavailableError,
    clear_cooldown,
    cooldown_remaining,
    note_failure,
)
```

`_clear_cache` fixture 에 쿨다운 초기화를 추가한다(테스트 간 누수 방지):

```python
    clear_cooldown("krx")
```
(fixture 의 `yield` 앞과 뒤 양쪽 모두)

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `docker compose exec web pytest tests/test_krx_index.py -v -k "TestTransportErrors or TestSessionAuth"`
Expected: FAIL — `AttributeError: module 'app.services.data.krx_index' has no attribute '_krx_rows'`

- [ ] **Step 3: 구현 작성**

`krx_index.py` 상단 import 에 추가:

```python
from app.core.config import settings
from app.services.data.errors import (
    DataSourceError,
    SourceAuthError,
    SourceRequestError,
    SourceSchemaError,
    SourceUnavailableError,
    clear_cooldown,
    cooldown_remaining,
    note_failure,
    representative,
)
```

(`DataSourceError` 는 `has_krx_auth` 와 `_krx_rows` 의 타입 힌트가, `representative` 는 Task 3 의 집계 루프가 쓴다.)

`_SESSION_FAIL_COOLDOWN`/`_session_fail_until` 전역과 `import time` 을 삭제하고(쿨다운은 Task 1 레지스트리가 관리), `_session` 을 아래로 교체한다:

```python
def _build_session():
    """pykrx 로그인 1회. 테스트에서 목으로 갈아끼우기 위해 분리한다."""
    from pykrx.website.comm import auth

    # build_krx_session() 은 로그인 로그 이후에도 내부적으로 추가 요청을 할 수 있는데,
    # 그 경로엔 우리가 손댈 수 없는 timeout 미지정 호출이 있을 수 있다(실제로 로그인
    # 완료 로그 이후 응답 없이 멈추는 현상을 관측함). 소켓 레벨로 강제 타임아웃을 건다.
    with bounded_socket_timeout(20):
        return auth.build_krx_session()


def _session():
    """pykrx 인증 KRX 세션을 반환한다.

    **미설정과 실패를 가른다** — 둘 다 None 이던 것이 §44-1 재발 경로였다.
    자격증명이 없으면 애초에 물어보지 않은 것이므로 None(실패 아님). 로그인 실패·
    빈 세션·쿨다운 중은 SourceAuthError 를 던진다.
    """
    if not (settings.KRX_ID and settings.KRX_PW):
        return None  # 미설정 — 실패가 아니다(용도별 preflight 가 필요한 곳에서 막는다)

    try:
        from pykrx.website.comm import auth
    except Exception as e:  # noqa: BLE001
        raise SourceUnavailableError("krx", f"pykrx 로드 실패: {e}") from e

    # 쿨다운 검사는 '유효 세션 재사용'보다 **앞**이다 — 차단 상태에서도 세션 쿠키는
    # 유효할 수 있어(§44-1), 재사용 분기가 먼저면 쿨다운을 그대로 통과한다.
    remaining = cooldown_remaining("krx")
    if remaining > 0:
        raise SourceAuthError("krx", f"로그인 쿨다운 중 — 재시도 생략({remaining:.0f}초 남음)")

    sess = getattr(auth, "_auth_session", None)
    if sess is not None and getattr(sess, "is_valid", lambda: False)():
        return sess

    try:
        built = _build_session()
    except Exception as e:  # noqa: BLE001
        exc = SourceAuthError("krx", f"인증 세션 생성 실패: {e}")
        note_failure(exc)
        logger.warning("%s", exc)
        raise exc from e
    if built is None:
        exc = SourceAuthError("krx", "인증 세션 생성 실패(빈 세션)")
        note_failure(exc)
        logger.warning("%s", exc)
        raise exc
    clear_cooldown("krx")
    return built


def has_krx_auth() -> bool:
    """KRX 인증 세션을 확보할 수 있는지. preflight 용 — 예외를 던지지 않는다."""
    try:
        return _session() is not None
    except DataSourceError:
        return False
```

`has_krx_auth` 가 쓰는 `DataSourceError` 도 import 에 추가한다.

이어서 POST 공통 헬퍼를 `_session` 아래에 추가한다:

```python
def _krx_rows(sess, payload: dict, key: str, label: str, timeout: float = 20) -> list[dict]:
    """KRX MDC POST 1회. 실패는 원인별 DataSourceError 로 raise 한다.

    KRX 는 에러 코드 체계가 없다. §44-1 에서 관측한 차단 동작(**HTTP 200 인데 본문이
    JSON 이 아닌 HTML 안내 페이지**)을 인증 실패로 판별한다. 이 휴리스틱이 빗나가면
    Unavailable 로 분류돼 60초 쿨다운이 걸린다 — 오분류여도 재시도 폭주는 막힌다.

    정상 JSON 이면서 리스트가 비어 있는 것은 **실패가 아니다**(휴장일·미상장).
    호출자가 직전 영업일로 소급하도록 빈 리스트를 그대로 돌려준다.

    쿨다운은 **여기서만** 걸고, **여기서 검사도 한다** — §44-1 의 실제 형태는 세션
    쿠키는 살아 있고 POST 응답만 HTML 로 오는 것이라, `_session()` 의 검사만으로는
    "유효 세션 재사용" 분기를 타고 넘어가 차단된 POST 가 계속 나간다. 7일 소급 루프의
    연속 POST 까지 막으려면 매 왕복 직전에 검사해야 한다.
    """
    def _fail(exc: DataSourceError) -> DataSourceError:
        note_failure(exc)
        return exc

    remaining = cooldown_remaining("krx")
    if remaining > 0:
        raise SourceAuthError("krx", f"{label} 쿨다운 중 — 조회 생략({remaining:.0f}초 남음)")

    try:
        resp = sess.post(_JSON_URL, data=payload, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise _fail(SourceUnavailableError("krx", f"{label} 연결 실패: {e}")) from e

    status = getattr(resp, "status_code", 200)
    if status >= 500:
        raise _fail(SourceUnavailableError("krx", f"{label} 서버 오류: HTTP {status}"))
    if status >= 400:
        raise _fail(SourceRequestError("krx", f"{label} 요청 오류: HTTP {status}"))

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise _fail(
            SourceAuthError("krx", f"{label} 응답이 JSON 이 아니다(차단 추정): {e}")
        ) from e

    if not isinstance(data, dict) or key not in data:
        # Schema 는 cooldown=None 이라 _fail 을 거쳐도 쿨다운이 걸리지 않는다(의도).
        raise _fail(
            SourceSchemaError("krx", f"{label} 응답에 '{key}' 키가 없다: {str(data)[:120]}")
        )
    return data.get(key) or []
```

기존 `TestSessionFailureCooldown` 3건을 레지스트리 기준으로 고친다 — `krx_index._session_fail_until = 0.0` 을 `clear_cooldown("krx")` 로, "None 반환" 단언을 `pytest.raises(SourceAuthError)` 로 바꾸고, `_build_session` 을 목으로 주입한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `docker compose exec web pytest tests/test_krx_index.py -v`
Expected: PASS. 기존 테스트 중 목 세션이 실제와 다른 키(예: `market_caps` 인데 `{"output": ...}`)를 돌려주던 것이 있으면 `SourceSchemaError` 로 실패한다 — **목을 실제 키(`OutBlock_1`·`block1`·`output`)에 맞춰 고친다.** 프로덕션 코드를 되돌리지 말 것.

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/krx_index.py backend/tests/test_krx_index.py
git commit -m "feat: KRX 전송 계층을 원인별 예외로 전환하고 미설정·실패를 분리

- HTTP 200 인데 비JSON 이면 차단(Auth), 5xx 는 Unavailable, 기대 키 부재는 Schema 로 가른다. 정상 JSON + 빈 리스트는 휴장일이므로 실패가 아니다
- _session 이 미설정(None)과 로그인 실패(SourceAuthError)를 가른다 — 둘 다 None 이던 것이 §44-1 재발 경로였다
- 모듈 전역 쿨다운을 원인별 레지스트리로 이관

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `krx_index` 집계 계층 — 전량 실패면 raise

**Files:**
- Modify: `backend/app/services/data/krx_index.py` — `index_members`, `all_listed_stocks`, `market_caps`, `sector_map`, `etf_leverage_exposure`
- Test: `backend/tests/test_krx_index.py`

**Interfaces:**
- Consumes: Task 2 의 `_krx_rows`, `_session`; Task 1 의 `representative`
- Produces: 위 5개 공개 함수의 계약 변경 — 전량 실패 시 `DataSourceError` raise, 부분 실패는 기존대로 값 반환

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_krx_index.py` 에 추가한다:

```python
class TestAggregateFailure:
    """집계 계층 — '성공'은 응답 수신이지 데이터 획득이 아니다."""

    @staticmethod
    def _always_fail_session():
        class _S:
            def post(self, *a, **kw):
                raise OSError("connection reset")
        return _S()

    @staticmethod
    def _empty_ok_session(key):
        """정상 JSON + 빈 리스트만 돌려주는 세션(휴장일 상황)."""
        class _R:
            status_code = 200

            def json(self):
                return {key: []}

        class _S:
            def post(self, *a, **kw):
                return _R()
        return _S()

    def test_전량_실패면_index_members가_raise(self, monkeypatch):
        monkeypatch.setattr(krx_index, "_session", lambda: self._always_fail_session())
        with pytest.raises(SourceUnavailableError):
            krx_index.index_members(date(2026, 8, 3))

    def test_정상응답인데_전부_빈값이면_빈_리스트(self, monkeypatch):
        """핵심 회귀 방지 — 휴장일 전량 빈 응답을 실패로 오인하면 안 된다."""
        monkeypatch.setattr(krx_index, "_session", lambda: self._empty_ok_session("output"))
        assert krx_index.index_members(date(2026, 8, 3)) == []

    def test_중간_실패_후_성공하면_예외_없이_반환(self, monkeypatch):
        calls = {"n": 0}

        class _R:
            status_code = 200

            def json(self):
                return {"output": [{"ISU_SRT_CD": "005930"}]}

        class _S:
            def post(self, *a, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("일시 오류")
                return _R()

        monkeypatch.setattr(krx_index, "_session", lambda: _S())
        assert krx_index.index_members(date(2026, 8, 3)) == ["005930"]

    def test_미인증이면_예외가_아니라_빈_리스트(self, monkeypatch):
        """미설정은 실패가 아니다 — preflight 가 필요한 곳에서 따로 막는다."""
        monkeypatch.setattr(krx_index, "_session", lambda: None)
        assert krx_index.index_members(date(2026, 8, 3)) == []

    def test_market_caps_전량_실패면_raise(self, monkeypatch):
        monkeypatch.setattr(krx_index, "_session", lambda: self._always_fail_session())
        with pytest.raises(SourceUnavailableError):
            krx_index.market_caps(date(2026, 8, 3))

    def test_sector_map_전량_실패면_raise(self, monkeypatch):
        monkeypatch.setattr(krx_index, "_session", lambda: self._always_fail_session())
        with pytest.raises(SourceUnavailableError):
            krx_index.sector_map(date(2026, 8, 3))

    def test_all_listed_stocks_실패면_raise(self, monkeypatch):
        monkeypatch.setattr(krx_index, "_session", lambda: self._always_fail_session())
        with pytest.raises(SourceUnavailableError):
            krx_index.all_listed_stocks()

    def test_원인_혼재시_Auth가_대표(self, monkeypatch):
        calls = {"n": 0}

        class _Html:
            status_code = 200

            def json(self):
                raise ValueError("not json")

        class _S:
            def post(self, *a, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("일시 오류")
                return _Html()

        monkeypatch.setattr(krx_index, "_session", lambda: _S())
        with pytest.raises(SourceAuthError):
            krx_index.index_members(date(2026, 8, 3))
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `docker compose exec web pytest tests/test_krx_index.py::TestAggregateFailure -v`
Expected: FAIL — 현재는 예외 대신 빈 리스트를 반환하므로 `DID NOT RAISE`

- [ ] **Step 3: 구현 작성**

`index_members` 의 루프를 아래로 교체한다(다른 4개 함수도 같은 형태로 고친다):

```python
    codes: list[str] = []
    errors: list[DataSourceError] = []
    ok = False  # '성공' = 예외 없이 응답을 받음(데이터 획득이 아니다)
    # 휴장일 빈 응답 대비 최대 6일(주말+연휴) 직전 영업일까지 스냅.
    for back in range(7):
        dd = (as_of - timedelta(days=back)).strftime("%Y%m%d")
        payload = {
            "bld": _BLD_INDEX_CONSTITUENTS, "locale": "ko_KR",
            "trdDd": dd, "money": "1", "csvxls_isNo": "false", **params,
        }
        try:
            rows = _krx_rows(sess, payload, "output", f"{index} 구성종목", timeout=15)
        except DataSourceError as e:
            logger.warning("KRX %s 구성종목 조회 실패(%s): %s", index, dd, e)
            errors.append(e)
            continue
        ok = True
        codes = [str(r.get("ISU_SRT_CD")).zfill(6) for r in rows if r.get("ISU_SRT_CD")]
        if codes:
            break

    # 정상 응답이 한 번도 없었으면 실패다. 정상 응답이 있었는데 전부 비었으면
    # 진짜 휴장/미상장이므로 빈 값을 그대로 돌려준다.
    # 쿨다운은 _krx_rows 가 이미 걸었다 — 여기서 또 걸지 않는다.
    if not ok and errors:
        raise representative(errors)
```

`market_caps`·`sector_map` 은 안쪽에 시장 루프(`STK`/`KSQ`)가 하나 더 있다. `ok`/`errors` 는 바깥 날짜 루프 기준으로 누적하되, 안쪽 두 요청 중 **하나라도** 예외 없이 응답하면 `ok = True` 다. `market_caps` 전문:

```python
    caps: dict[str, int] = {}
    errors: list[DataSourceError] = []
    ok = False
    for back in range(7):  # 휴장일 빈 응답 대비 직전 영업일 소급
        dd = (as_of - timedelta(days=back)).strftime("%Y%m%d")
        for mkt in ("STK", "KSQ"):
            payload = {
                "bld": _BLD_MARKET_CAP, "locale": "ko_KR",
                "mktId": mkt, "trdDd": dd, "money": "1", "csvxls_isNo": "false",
            }
            try:
                rows = _krx_rows(sess, payload, "OutBlock_1", f"시가총액({mkt})")
            except DataSourceError as e:
                logger.warning("KRX 시가총액 조회 실패(%s %s): %s", mkt, dd, e)
                errors.append(e)
                continue
            ok = True
            for r in rows:
                code = str(r.get("ISU_SRT_CD") or "").strip().zfill(6)
                raw = str(r.get("MKTCAP") or "").replace(",", "").strip()
                if code and raw.isdigit():
                    caps[code] = int(raw)
        if caps:
            break

    # 쿨다운은 _krx_rows 가 이미 걸었다 — 여기서 또 걸지 않는다.
    if not ok and errors:
        raise representative(errors)
```

`sector_map` 은 루프 범위가 10일이고 키가 `block1`, 파싱이 `IDX_IND_NM` 인 것만 다르고 구조는 위와 같다. 주의: `sector_map` 의 PIT 스냅샷 조기 반환(`as_of` 가 주어지고 스냅샷이 있으면 그것을 반환)은 KRX 를 타지 않으므로 **그대로 둔다**.

`all_listed_stocks` 는 단일 조회다. 루프가 없으므로 `_krx_rows(sess, payload, "block1", "전종목 목록")` 의 예외를 그대로 전파한다(쿨다운은 `_krx_rows` 가 이미 걸었다).

`etf_leverage_exposure` 는 7일 루프가 있으므로 `index_members` 와 동일한 `ok`/`errors` 패턴을 쓴다(키는 `output`).

각 함수의 docstring 에서 "실패 시 빈 리스트/빈 dict" 문구를 **"전량 실패 시 DataSourceError. 미인증 시 빈 값"** 으로 고친다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `docker compose exec web pytest tests/test_krx_index.py -v`
Expected: PASS 전부

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/krx_index.py backend/tests/test_krx_index.py
git commit -m "feat: KRX 집계 계층은 전량 실패일 때만 예외를 올린다

- '성공'은 예외 없이 응답을 받은 것이지 데이터를 얻은 것이 아니다. 정상 응답이 한 번도 없으면 실패, 정상 응답이 있었는데 전부 비었으면 휴장일이므로 빈 값
- 임계를 비율이 아니라 전량으로 둬 튜닝 파라미터를 만들지 않으면서 §44-1(사실상 전량 실패)을 잡는다
- 원인이 섞이면 representative 로 대표를 고른다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `opendart` 전송 계층 — status 코드 기반 원인 분류

**Files:**
- Modify: `backend/app/services/data/opendart.py:75-102` (`_get`), `:105-141` (`corp_code_map`)
- Test: `backend/tests/test_opendart.py` (없으면 생성)

**Interfaces:**
- Consumes: Task 1 의 예외들, `classify_httpx`, `note_failure`, `cooldown_remaining`, `seconds_until_midnight`
- Produces: `_get(path, params) -> dict | None` — 미설정·`013` 이면 `None`, 그 외 실패는 raise / `corp_code_map() -> dict[str, str] | None` — 미설정이면 `None`, 실패·빈 매핑이면 raise

- [ ] **Step 1: 실패 테스트 작성**

`backend/tests/test_opendart.py` 에 추가한다(파일이 없으면 생성):

```python
"""OpenDART 클라이언트 — status 코드 원인 분류 검증(네트워크 목)."""
import pytest

from app.services.data import opendart
from app.services.data.errors import (
    SourceAuthError,
    SourceQuotaError,
    SourceRequestError,
    SourceUnavailableError,
    clear_cooldown,
)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(opendart, "is_enabled", lambda: True)
    clear_cooldown("dart")
    yield
    clear_cooldown("dart")


def _mock_status(monkeypatch, status: str):
    """지정 status 를 돌려주는 httpx.Client 목."""
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": status, "message": "목"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(opendart.httpx, "Client", _Client)


class TestStatusClassification:
    @pytest.mark.parametrize("status", ["010", "011", "012"])
    def test_키_문제는_Auth(self, monkeypatch, status):
        _mock_status(monkeypatch, status)
        with pytest.raises(SourceAuthError):
            opendart._get("fnlttSinglAcnt.json", {})

    @pytest.mark.parametrize("status", ["020", "021"])
    def test_한도_초과는_Quota(self, monkeypatch, status):
        _mock_status(monkeypatch, status)
        with pytest.raises(SourceQuotaError):
            opendart._get("fnlttSinglAcnt.json", {})

    def test_일일한도는_다음_자정까지_쿨다운(self, monkeypatch):
        """실제 자정까지의 초를 쓰면 자정 직전에 실행될 때 흔들리므로 고정한다."""
        from app.services.data.errors import cooldown_remaining

        monkeypatch.setattr(opendart, "seconds_until_midnight", lambda: 7200.0)
        _mock_status(monkeypatch, "020")
        with pytest.raises(SourceQuotaError):
            opendart._get("fnlttSinglAcnt.json", {})
        assert 7100 < cooldown_remaining("dart") <= 7200  # 클래스 기본값(300s)이 아니다

    @pytest.mark.parametrize("status", ["100", "101"])
    def test_잘못된_요청은_Request(self, monkeypatch, status):
        _mock_status(monkeypatch, status)
        with pytest.raises(SourceRequestError):
            opendart._get("fnlttSinglAcnt.json", {})

    def test_시스템_점검은_Unavailable(self, monkeypatch):
        _mock_status(monkeypatch, "800")
        with pytest.raises(SourceUnavailableError):
            opendart._get("fnlttSinglAcnt.json", {})

    def test_미지_코드는_보수적으로_Unavailable(self, monkeypatch):
        _mock_status(monkeypatch, "777")
        with pytest.raises(SourceUnavailableError):
            opendart._get("fnlttSinglAcnt.json", {})

    def test_무자료_013은_예외가_아니라_None(self, monkeypatch):
        """소스가 '없다'고 정상 응답한 것 — 과거 구간엔 이런 종목이 흔하다."""
        _mock_status(monkeypatch, "013")
        assert opendart._get("fnlttSinglAcnt.json", {}) is None

    def test_정상_000은_데이터_반환(self, monkeypatch):
        _mock_status(monkeypatch, "000")
        assert opendart._get("fnlttSinglAcnt.json", {})["status"] == "000"

    def test_미설정이면_예외가_아니라_None(self, monkeypatch):
        monkeypatch.setattr(opendart, "is_enabled", lambda: False)
        assert opendart._get("fnlttSinglAcnt.json", {}) is None
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `docker compose exec web pytest tests/test_opendart.py::TestStatusClassification -v`
Expected: FAIL — 현재는 모든 status 에서 `None` 을 반환하므로 `DID NOT RAISE`

- [ ] **Step 3: 구현 작성**

`opendart.py` 상단 import 에 추가:

```python
from app.services.data.errors import (
    DataSourceError,
    SourceAuthError,
    SourceQuotaError,
    SourceRequestError,
    SourceSchemaError,
    SourceUnavailableError,
    classify_httpx,
    cooldown_remaining,
    note_failure,
    representative,
    seconds_until_midnight,
)
```

(`representative` 는 Task 5 의 집계 루프가 쓴다.)

status 매핑 상수를 `_STATUS_NO_DATA` 옆에 추가한다:

```python
#: OpenDART status 코드 → 실패 원인. 013(무자료)·000(정상)은 여기 없다(실패가 아니다).
#: 목록에 없는 코드는 보수적으로 Unavailable 로 본다(재시도 가능 쪽).
_STATUS_CAUSE: dict[str, type[DataSourceError]] = {
    "010": SourceAuthError,          # 등록되지 않은 키
    "011": SourceAuthError,          # 사용할 수 없는 키
    "012": SourceAuthError,          # 접근할 수 없는 IP
    "014": SourceRequestError,       # 파일이 존재하지 않음
    "020": SourceQuotaError,         # 요청 제한 초과(일 20,000건)
    "021": SourceQuotaError,         # 조회 가능한 회사 개수 초과
    "100": SourceRequestError,       # 필드의 부적절한 값
    "101": SourceRequestError,       # 부적절한 접근
    "800": SourceUnavailableError,   # 시스템 점검
    "900": SourceUnavailableError,   # 정의되지 않은 오류
}
```

`_get` 을 아래로 교체한다:

```python
def _get(path: str, params: dict) -> dict | None:
    """OpenDART REST 호출 공통부.

    **미설정과 무자료(013)만 None** 이다 — 둘 다 실패가 아니다(전자는 애초에 안 물어본
    것, 후자는 소스가 '없다'고 정상 응답한 것). 그 외 실패는 원인별 예외를 던진다.
    특히 일일 20,000건 한도 초과(020)가 무자료와 같은 None 이던 것이 위험했다 —
    한도를 소진하면 전 종목이 조용히 '재무 정보 없음'이 됐다.
    """
    if not is_enabled():
        logger.debug("OpenDART 미활성(API 키 없음) — %s 조회 건너뜀", path)
        return None

    remaining = cooldown_remaining("dart")
    if remaining > 0:
        raise SourceQuotaError("dart", f"쿨다운 중 — 조회 생략({remaining:.0f}초 남음)")

    url = f"{settings.OPENDART_BASE_URL}/{path}"
    q = {"crtfc_key": settings.OPENDART_API_KEY, **params}
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, params=q)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        base = classify_httpx("dart", e)
        # 어느 엔드포인트였는지를 메시지에 넣는다(args 를 직접 건드리지 않고 재구성).
        exc = type(base)("dart", f"{path} — {base.detail}", retry_after=base.retry_after)
        note_failure(exc)
        logger.warning("OpenDART 호출 실패: %s", _mask_key(str(exc)))
        raise exc from e

    status = str(data.get("status"))
    if status == _STATUS_OK:
        return data
    if status == _STATUS_NO_DATA:
        return None  # 무자료 — 실패가 아니다

    cls = _STATUS_CAUSE.get(status, SourceUnavailableError)
    # 일일 한도는 자정에 리셋되므로 그때까지 재조회해봐야 확정적으로 실패한다.
    retry_after = seconds_until_midnight() if status == "020" else None
    exc = cls("dart", f"{path} status={status} msg={data.get('message')}", retry_after=retry_after)
    note_failure(exc)
    logger.warning("%s", _mask_key(exc))
    raise exc
```

`corp_code_map` 의 두 `except` 블록과 빈 매핑 처리를 고친다:

```python
    except Exception as e:  # noqa: BLE001
        exc = classify_httpx("dart", e)
        note_failure(exc)
        logger.warning("OpenDART corpCode 다운로드 실패: %s", _mask_key(exc))
        raise exc from e
```

```python
    except ET.ParseError as e:
        raise SourceSchemaError("dart", f"corpCode XML 파싱 실패: {e}") from e
    if not mapping:
        # 다운로드·파싱은 됐는데 상장사가 0개면 스키마가 바뀐 것이다. 빈 매핑을
        # 그대로 돌려주면 전 종목이 조용히 '재무 정보 없음'이 된다.
        raise SourceSchemaError("dart", "corpCode 에 상장사가 0개 — 응답 스키마 변경 의심")
    logger.info("OpenDART corpCode 매핑 로드: %d개 상장사", len(mapping))
    return mapping
```

`_mask_key` 가 예외 객체를 받도록 `str()` 처리를 확인하고, 필요하면 `_mask_key(str(exc))` 로 호출한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `docker compose exec web pytest tests/test_opendart.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/opendart.py backend/tests/test_opendart.py
git commit -m "feat: OpenDART status 코드를 원인별 예외로 분류

- 미설정·무자료(013)만 None 으로 남기고 나머지는 raise. 일일 20,000건 한도 초과(020)가 무자료와 같은 None 이던 것이 가장 위험했다 — 한도 소진 시 전 종목이 조용히 '재무 정보 없음'이 됐다
- 020 은 자정 리셋이므로 retry_after 로 다음 자정까지 쿨다운
- corpCode 가 파싱은 됐는데 상장사 0개면 스키마 변경으로 보고 SourceSchemaError

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `opendart` 집계 계층 + `kofia` 전송 계층

**Files:**
- Modify: `backend/app/services/data/opendart.py:503-508` (`cached_corp_code_map`), `:651-706` (`metrics_by_symbol`), `:883-910` (`pead_sue_by_symbol`)
- Modify: `backend/app/services/data/kofia.py:123-142` (`_rows`), `:145-199` (`fetch_*` docstring)
- Test: `backend/tests/test_opendart.py`, `backend/tests/test_kofia.py`

**Interfaces:**
- Consumes: Task 1 의 `representative`, `SourceUnavailableError`, `SourceSchemaError`, `classify_httpx`; Task 4 의 `_get`
- Produces: `metrics_by_symbol`·`pead_sue_by_symbol` — 성공 0 & 실패 ≥1 이면 raise / `kofia._rows` — 실패 시 raise

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_opendart.py` 에 추가:

```python
class TestAggregateFailure:
    """'성공'은 응답 수신이지 데이터 획득이 아니다 — 과거 구간 무자료 종목이 흔하다."""

    @pytest.fixture(autouse=True)
    def _corp_map(self, monkeypatch):
        monkeypatch.setattr(
            opendart, "cached_corp_code_map",
            lambda: {"005930": "00126380", "000660": "00164779"},
        )

    def test_전_종목_무자료면_빈_dict(self, monkeypatch):
        """실패가 아니다 — 정상 백테스트가 죽으면 안 된다."""
        monkeypatch.setattr(opendart, "annual_metrics", lambda c, y: {"roe": None})
        out = opendart.metrics_by_symbol(["005930", "000660"], date(2026, 8, 3))
        assert out == {}

    def test_전_종목_실패면_raise(self, monkeypatch):
        def _boom(corp, year):
            raise SourceUnavailableError("dart", "타임아웃")

        monkeypatch.setattr(opendart, "annual_metrics", _boom)
        with pytest.raises(SourceUnavailableError):
            opendart.metrics_by_symbol(["005930", "000660"], date(2026, 8, 3))

    def test_일부만_실패하면_부분_결과(self, monkeypatch):
        def _half(corp, year):
            if corp == "00126380":
                return {"roe": 0.12}
            raise SourceUnavailableError("dart", "타임아웃")

        monkeypatch.setattr(opendart, "annual_metrics", _half)
        out = opendart.metrics_by_symbol(["005930", "000660"], date(2026, 8, 3))
        assert "005930" in out and "000660" not in out
```

`from datetime import date` 를 import 에 추가한다.

`backend/tests/test_kofia.py` 에 추가:

```python
class TestTransportErrors:
    def test_연결_실패는_Unavailable(self, monkeypatch):
        def _boom(*a, **kw):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(kofia.httpx, "post", _boom)
        with pytest.raises(SourceUnavailableError):
            kofia._rows(kofia._OBJ_CREDIT, date(2026, 7, 1), date(2026, 7, 31), "신용융자")

    def test_ds1_키_부재는_Schema(self, monkeypatch):
        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"unexpected": []}

        monkeypatch.setattr(kofia.httpx, "post", lambda *a, **kw: _Resp())
        with pytest.raises(SourceSchemaError):
            kofia._rows(kofia._OBJ_CREDIT, date(2026, 7, 1), date(2026, 7, 31), "신용융자")

    def test_ds1_빈_리스트는_예외가_아니다(self, monkeypatch):
        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"ds1": []}

        monkeypatch.setattr(kofia.httpx, "post", lambda *a, **kw: _Resp())
        assert kofia._rows(kofia._OBJ_CREDIT, date(2026, 7, 1), date(2026, 7, 31), "신용융자") == []
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `docker compose exec web pytest tests/test_opendart.py::TestAggregateFailure tests/test_kofia.py::TestTransportErrors -v`
Expected: FAIL — 현재는 예외 대신 빈 값을 반환

- [ ] **Step 3: 구현 작성**

`kofia.py` — import 에 `from app.services.data.errors import SourceSchemaError, classify_httpx, note_failure` 를 추가하고 `_rows` 를 교체한다:

```python
def _rows(obj_nm: str, start: date, end: date, label: str) -> list[dict]:
    """FreeSIS 통계표 1건을 조회해 원본 행 리스트를 반환한다.

    실패는 원인별 DataSourceError 로 raise 한다. 정상 응답이면서 행이 비어 있는 것은
    실패가 아니다(조회 구간에 자료가 없는 정상 상황).
    """
    if start > end:
        raise ValueError(f"start 가 end 보다 늦다: {start} > {end}")
    payload = {
        "dmSearch": {
            "tmpV40": "1",
            "tmpV41": "1",
            "tmpV45": start.strftime("%Y%m%d"),
            "tmpV46": end.strftime("%Y%m%d"),
            "OBJ_NM": obj_nm,
        }
    }
    try:
        resp = httpx.post(_URL, json=payload, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        exc = classify_httpx("kofia", e)
        note_failure(exc)
        logger.warning("FreeSIS %s 조회 실패(%s~%s): %s", label, start, end, exc)
        raise exc from e

    if not isinstance(data, dict) or "ds1" not in data:
        raise SourceSchemaError("kofia", f"{label} 응답에 'ds1' 키가 없다: {str(data)[:120]}")
    return data.get("ds1") or []
```

`classify_httpx(source, exc)` 의 두 번째 인자는 **예외 객체**다(문자열이 아니다). 라벨·구간은 별도 로그 인자로 남긴다.

`fetch_credit_balance`·`fetch_market_funds` 의 docstring 에서 "실패 시 예외 대신 빈 리스트를 반환한다"·"실패하면 경고만 남기고 빈 리스트를 반환한다" 문구를 **"전송 실패는 DataSourceError 로 전파된다"** 로 고친다.

`opendart.py` — `cached_corp_code_map` 은 `corp_code_map()` 의 예외를 그대로 전파한다(캐시하지 않으므로 다음 호출에서 재시도된다). 변경 없음을 주석으로 명시한다.

`metrics_by_symbol` 의 종목 루프를 아래로 고친다:

```python
    out: dict[str, dict[str, float | None]] = {}
    errors: list[DataSourceError] = []
    ok = 0  # '성공' = 예외 없이 응답을 받은 종목 수(무자료 포함)
    for raw in codes:
        code = str(raw).strip().zfill(6)
        corp = corp_map.get(code)
        if not corp:
            continue
        try:
            if use_ttm:
                m = dict(ttm_metrics(corp, year, reprt_code))
                prev = ttm_metrics(corp, year - 1, reprt_code)
                prev2 = ttm_metrics(corp, year - 2, reprt_code)
            else:
                m = dict(annual_metrics(corp, year))
                prev = annual_metrics(corp, year - 1)
                prev2 = annual_metrics(corp, year - 2)
        except DataSourceError as e:
            errors.append(e)
            continue
        ok += 1
        m["op_growth"] = _yoy_growth(m.get("op_income"), prev.get("op_income"))
        m["net_growth"] = _yoy_growth(m.get("net_income"), prev.get("net_income"))
        m["turnaround"] = _turnaround_flag(m.get("net_income"), prev.get("net_income"))
        m["f_score"] = piotroski_f_score(m, prev)
        m["loss_years_3"] = _count_losses(
            [m.get("net_income"), prev.get("net_income"), prev2.get("net_income")]
        )
        if any(v is not None for v in m.values()):
            out[code] = m

    # 전 종목이 '무자료'라 out 이 비는 것은 실패가 아니다(과거 구간엔 흔하다).
    # 응답을 한 번도 못 받은 경우에만 실패로 본다. 쿨다운은 _get 이 이미 걸었다.
    if ok == 0 and errors:
        raise representative(errors)
    if errors:
        logger.error(
            "OpenDART 부분 실패 — %d/%d 종목 조회 실패(대표: %s)",
            len(errors), len(codes), errors[0],
        )
    return out
```

`pead_sue_by_symbol` 도 같은 `ok`/`errors` 패턴으로 고친다(`_pead_sue_one` 호출을 `try` 로 감싼다).

- [ ] **Step 4: 테스트 통과 확인**

Run: `docker compose exec web pytest tests/test_opendart.py tests/test_kofia.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/opendart.py backend/app/services/data/kofia.py backend/tests/test_opendart.py backend/tests/test_kofia.py
git commit -m "feat: DART 집계 계층과 KOFIA 전송 계층을 예외 전파로 전환

- 전 종목 무자료로 결과가 비는 것은 실패가 아니다(과거 구간엔 흔하다). 응답을 한 번도 못 받았을 때만 raise
- 부분 실패는 ERROR 로그로 드러내고 부분 결과를 돌려준다
- KOFIA 는 아직 호출자가 없어 계약 변경이 무위험

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: 호출자 — 전파 그룹, preflight, API 503 매핑

**Files:**
- Modify: `backend/app/api/routes/backtests.py:305-325` (`_build_pit_pool`)
- Modify: `backend/app/main.py:57` 부근 (예외 핸들러 추가)
- Modify: `backend/app/services/recommend.py:132`, `backend/app/services/screener.py:109` (docstring 만 — 이미 잡지 않으므로 코드 변경 없음을 확인)
- Test: `backend/tests/test_backtests_pit.py` (없으면 생성), `backend/tests/test_main_error_handler.py`

**Interfaces:**
- Consumes: Task 2 의 `has_krx_auth`, Task 1 의 `DataSourceError`
- Produces: `require_krx_auth() -> None` (in `krx_index.py`), `DataSourceError` → HTTP 503 핸들러

**설계 스펙과의 차이:** 스펙은 `require_krx_auth()` 를 `errors.py` 에 두라고 적었지만, KRX 전용 함수를 소스 중립적인 예외 모듈에 두면 응집도가 나빠지고 순환 import 를 부른다. `krx_index.py` 에 둔다.

- [ ] **Step 1: 실패 테스트 작성**

`backend/tests/test_backtests_pit.py`:

```python
"""PIT 백테스트 preflight — 인증 없이 19개월치를 다 돌기 전에 막는다."""
from datetime import date

import pytest

from app.services.data import krx_index
from app.services.data.errors import SourceAuthError


class TestPreflight:
    def test_미인증이면_즉시_차단(self, monkeypatch):
        monkeypatch.setattr(krx_index, "has_krx_auth", lambda: False)
        with pytest.raises(SourceAuthError):
            krx_index.require_krx_auth()

    def test_인증되면_통과(self, monkeypatch):
        monkeypatch.setattr(krx_index, "has_krx_auth", lambda: True)
        krx_index.require_krx_auth()  # 예외 없음
```

`backend/tests/test_main_error_handler.py`:

```python
"""외부 소스 장애는 서버 버그가 아니므로 500 이 아니라 503 이어야 한다."""
from fastapi.testclient import TestClient

from app.main import app
from app.services.data.errors import SourceAuthError


def test_DataSourceError는_503(monkeypatch):
    @app.get("/__test_datasource_error")
    async def _boom():
        raise SourceAuthError("krx", "차단")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/__test_datasource_error")
    assert resp.status_code == 503
    assert resp.json()["source"] == "krx"
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `docker compose exec web pytest tests/test_backtests_pit.py tests/test_main_error_handler.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'require_krx_auth'`, 503 대신 500

- [ ] **Step 3: 구현 작성**

`krx_index.py` 에 추가한다:

```python
def require_krx_auth() -> None:
    """KRX 인증이 **필수** 인 실행의 진입점에서 호출한다(PIT 유니버스 백테스트 등).

    미설정 자체는 실패가 아니지만(개발환경에서 앱은 떠야 한다), PIT 유니버스처럼
    없으면 결과가 무의미해지는 필수 입력에는 다르다. 인증 없이 돌리면 모든 조회가
    빈 값을 주고 백테스트가 **빈 패널 위에서 '성공'** 한다(§44-1 과 동일한 결과).
    필수/선택을 소스가 아니라 용도로 가르기 위한 사전 검사.
    """
    if not has_krx_auth():
        raise SourceAuthError(
            "krx", "KRX 인증이 필요한 실행인데 KRX_ID/KRX_PW 가 없거나 로그인에 실패했다"
        )
```

`backtests.py` 의 `_build_pit_pool` — `source == "fixed"` 조기 반환 **뒤**, 월별 루프 **앞**에 preflight 를 넣는다:

```python
    from app.services.data import krx_index

    # 인증 없이 돌면 월별 조회가 전부 빈 값을 주고 백테스트가 빈 패널 위에서
    # '성공'한다(§44-1). 19개월치를 다 돌기 전에 막는다.
    krx_index.require_krx_auth()
```

기존 `if caps:` 가드(`backtests.py:321-323`)는 그대로 둔다 — 이제 `market_caps` 가 전량 실패 시 예외를 던지므로, 이 가드가 덮는 것은 "정상 응답인데 빈 결과"뿐이고 그건 원래 의도된 폴백이다.

`main.py` 의 전역 `Exception` 핸들러 **위에** 전용 핸들러를 추가한다(FastAPI 는 더 구체적인 타입을 우선 매칭한다):

```python
@app.exception_handler(DataSourceError)
async def data_source_error_handler(request: Request, exc: DataSourceError) -> JSONResponse:
    """외부 데이터 소스 장애는 서버 버그가 아니다 — 500 이 아니라 503."""
    logger.error("외부 데이터 소스 실패 (%s): %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "외부 데이터 소스를 사용할 수 없습니다.",
            "source": exc.source,
            "cause": type(exc).__name__,
            "retryable": exc.retryable,
        },
    )
```

`from app.services.data.errors import DataSourceError` 를 import 에 추가한다.

`recommend.py:132`·`screener.py:109` 는 이미 예외를 잡지 않으므로 코드 변경이 없다. 각 함수 docstring 에 "OpenDART 전량 실패 시 DataSourceError 가 전파돼 API 가 503 을 반환한다"를 한 줄 추가한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `docker compose exec web pytest tests/test_backtests_pit.py tests/test_main_error_handler.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add backend/app/services/data/krx_index.py backend/app/api/routes/backtests.py backend/app/main.py backend/app/services/recommend.py backend/app/services/screener.py backend/tests/test_backtests_pit.py backend/tests/test_main_error_handler.py
git commit -m "feat: PIT 백테스트 preflight 와 DataSourceError 503 매핑

- 미설정 자체는 실패가 아니지만 PIT 유니버스는 없으면 결과가 무의미하다. 용도 기준으로 갈라 진입점에서 막는다 — 19개월치를 다 돌기 전에
- 외부 소스 장애는 서버 버그가 아니므로 전역 Exception 핸들러의 500 이 아니라 503 + source·원인을 반환

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: 호출자 — 명시적 저하 그룹과 bare except 제거

**Files:**
- Modify: `backend/app/api/routes/backtests.py:174-190`, `backend/app/services/metrics/factors.py:677-695`, `backend/app/services/symbols.py:124-133`, `backend/app/services/data/ingest.py:32-37`, `backend/app/services/backtest/portfolio.py:794` 부근, `backend/app/services/news.py`
- Test: `backend/tests/test_caller_degradation.py`

**Interfaces:**
- Consumes: Task 1 의 `DataSourceError`
- Produces: 없음(호출자 정리로 종료)

- [ ] **Step 1: 실패 테스트 작성**

`backend/tests/test_caller_degradation.py`:

```python
"""명시적 저하 경로 — 외부 장애는 삼키되 우리 쪽 버그는 삼키지 않는다."""
import pytest

from app.services import symbols
from app.services.data import krx_index
from app.services.data.errors import SourceUnavailableError


class TestSymbolCatalogDegradation:
    """종목명 카탈로그는 없어도 매매가 계속돼야 한다 — 저하는 유지, 은폐만 제거."""

    def test_외부_장애는_삼키고_계속_진행한다(self, monkeypatch):
        def _boom():
            raise SourceUnavailableError("krx", "타임아웃")

        monkeypatch.setattr(krx_index, "all_listed_stocks", _boom)
        catalog, external_ok = symbols._build_catalog()
        assert external_ok is False  # KRX 실패가 드러난다
        assert isinstance(catalog, list)  # 폴백 소스로 진행

    def test_우리_쪽_버그는_전파된다(self, monkeypatch):
        """except Exception 이던 시절엔 TypeError 까지 삼켜 은신처가 됐다."""
        def _bug():
            raise TypeError("잘못된 인자")

        monkeypatch.setattr(krx_index, "all_listed_stocks", _bug)
        with pytest.raises(TypeError):
            symbols._build_catalog()
```

`ingest.build_universe` 에도 같은 형태의 테스트 2건을 추가한다. `build_universe(db, index)` 는
async 이고 DB 세션을 받으므로 `pytest.mark.asyncio` 와 기존 테스트의 DB 픽스처를 재사용한다
(`tests/` 에서 `build_universe` 를 이미 쓰는 테스트가 있으면 그 픽스처를 그대로 따른다):

```python
@pytest.mark.asyncio
async def test_build_universe_는_우리_쪽_버그를_전파한다(monkeypatch, db_session):
    def _bug(*a, **kw):
        raise TypeError("잘못된 인자")

    monkeypatch.setattr(krx_index, "index_members", _bug)
    with pytest.raises(TypeError):
        await ingest.build_universe(db_session)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `docker compose exec web pytest tests/test_caller_degradation.py -v`
Expected: FAIL — 현재는 `except Exception` 이라 `TypeError` 도 삼켜져 `DID NOT RAISE`

- [ ] **Step 3: 구현 작성**

아래 6곳의 `except Exception` 을 `except DataSourceError` 로 좁히고 로그를 승격한다. 각 파일에 `from app.services.data.errors import DataSourceError` 를 추가한다.

`backtests.py:174-190` (중립화):

```python
    if neutralize in ("size", "size_sector"):
        try:
            caps = krx_index.market_caps(as_of_date)
        except DataSourceError as e:
            # 중립화 축 생략은 §20 에 설계된 저하다(순수 팩터 그대로). 다만 조용히
            # 넘어가면 요청한 중립화가 적용됐는지 알 수 없으므로 ERROR 로 남긴다.
            logger.error("사이즈 중립화 생략 — 시가총액 조회 실패: %s", e)
            caps = {}
```

`sector_map` 쪽도 동일하게 고친다(`logger.error("섹터 중립화 생략 — 업종분류 조회 실패: %s", e)`).

`factors.py:677-695` 도 같은 형태로 고친다.

`symbols.py:132`:

```python
    except DataSourceError as e:
        logger.error("종목 카탈로그: KRX 전종목 목록 조회 실패 — 폴백 소스로 진행: %s", e)
```

`ingest.py:36`:

```python
    except DataSourceError as e:
        logger.error("KOSPI200 구성종목 조회 실패(그 부분만 제외): %s", e)
```

`portfolio.py:794` 부근의 `sector_map` 호출과 `news.py` 의 종목명 해석 경로도 동일하게 좁힌다. `news.py` 의 **피드별** `except Exception`(`news.py:218`)은 외부 RSS 파싱이라 `DataSourceError` 가 아니므로 **그대로 둔다**.

- [ ] **Step 4: 테스트 통과 확인**

Run: `docker compose exec web pytest tests/test_caller_degradation.py -v`
Expected: PASS

- [ ] **Step 5: 전체 테스트 스위트 확인**

Run: `docker compose exec web pytest`
Expected: PASS. 실패가 있으면 목이 실제 응답 키와 어긋난 경우이므로 **목을 고친다**(프로덕션 코드를 되돌리지 말 것).

- [ ] **Step 6: 커밋**

```bash
git add backend/app/api/routes/backtests.py backend/app/services/metrics/factors.py backend/app/services/symbols.py backend/app/services/data/ingest.py backend/app/services/backtest/portfolio.py backend/app/services/news.py backend/tests/test_caller_degradation.py
git commit -m "refactor: 명시적 저하 경로의 bare except 를 DataSourceError 로 좁힌다

- 저하 자체는 유지한다(종목명·중립화 축·섹터 리포트가 없어도 매매·백테스트는 성립한다). 다만 WARNING 을 ERROR 로 올려 드러낸다
- except Exception 이 TypeError 같은 우리 쪽 버그까지 삼키고 있었다 — 이번 작업에서 가장 조용했던 실패일 수 있다
- news.py 의 피드별 예외는 RSS 파싱이라 DataSourceError 가 아니므로 그대로 둔다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: 문서 반영

**Files:**
- Modify: `docs/improvements.md` (§44-1 "남은 한계" 갱신 + 새 항목)

**Interfaces:**
- Consumes: Task 1~7 의 결과
- Produces: 없음

- [ ] **Step 1: §44-1 갱신**

"**남은 한계**" 문단 뒤에 한 줄 추가한다:

```markdown
**후속(이 계획으로 해소)**: 실패가 조용했던 구조 자체를 없앴다 — `docs/superpowers/specs/2026-08-04-external-api-silent-failure-design.md` 참고.
```

- [ ] **Step 2: 새 항목 추가**

`docs/improvements.md` 끝에 다음을 추가한다(번호는 파일의 마지막 항목 다음 번호로 맞춘다):

```markdown
## NN. 외부 데이터 소스의 조용한 실패 — 해소 ✅

§44-1 의 근본 구조. `krx_index`·`opendart`·`kofia` 는 외부 호출이 실패해도 예외 대신
빈 값을 반환했다(세 모듈 합쳐 49곳). 문제는 실패가 감춰진 것 자체가 아니라 **실패한
빈 값과 정상적으로 빈 값이 같은 값**이라 호출자가 구분할 수 없었다는 점이다.
`opendart._get` 은 미설정·네트워크 실패·에러 status·무자료의 **네 가지가 전부 None**
이었고, 특히 일일 20,000건 한도 초과(`020`)가 "조회된 데이터 없음"(`013`)과 구분되지
않아 한도를 소진하면 전 종목이 조용히 '재무 정보 없음'이 됐다.

**경계를 셋으로 갈랐다.** 실패(raise) / 데이터 없음(정상 빈 값) / 미설정(통과).
이 경계가 관념이 아니라 코드로 판별된다는 근거가 있다 — KRX 차단 시 응답은 JSON 이
아닌 HTML 이라 파싱이 예외를 던지고, 진짜 휴장일은 정상 JSON + 빈 `output` 이라 예외가
없다. DART 는 status 코드가 응답에 있는데 **읽고도 로그로만 흘리고 있었다**.

**예외는 소스가 아니라 원인별**(Auth·Quota·Unavailable·Schema·Request)로 나눴다.
호출자의 관심사가 "KRX 냐 DART 냐"가 아니라 "재시도해도 되나, 사람이 고쳐야 하나"이고,
두 축을 다 타입으로 만들면 조합이 15개가 되기 때문이다(소스는 `source` 속성).
나누는 값은 이름이 아니라 정책이 달라진다는 데 있다 — 쿨다운이 Auth 300s(재시도해도
차단만 악화), Unavailable 60s, Schema·Request 는 없음(기다리면 문제를 은폐)으로 갈린다.

**집계 임계는 비율이 아니라 전량**(성공 0 & 실패 ≥1)이다. 튜닝 파라미터를 만들지
않으면서 §44-1(사실상 전량 실패)을 잡는다. 여기서 '성공'은 **응답 수신이지 데이터
획득이 아니다** — 과거 구간일수록 DART 무자료 종목이 많아, 획득 기준으로 잡으면 정상
백테스트가 죽는다.

**미설정은 용도로 갈랐다.** `KRX_ID/PW` 가 없으면 `index_members` 가 `[]` 를 반환해
§44-1 과 결과가 동일해진다. 미설정 자체를 예외로 만들면 키 없는 개발환경이 깨지므로,
PIT 유니버스 백테스트 진입점에서 `require_krx_auth()` 로 사전 검사한다.

**부수 효과**: 명시적 저하 경로의 `except Exception` 3곳을 `except DataSourceError` 로
좁혔다. 외부 장애뿐 아니라 `TypeError` 같은 우리 쪽 버그까지 삼키고 있었다.

**미검증 잔여**: KRX 가 §44-1 로 차단된 상태라 전송 계층 변경을 **실제 KRX 응답으로
확인하지 못했다**. 가짜 세션 단위 테스트까지만 통과했고, 차단 해제 후 통합 확인이 필요하다.

**비목표(후속)**: 자동 백오프 재시도(`retryable` 속성만 깔았다), 영속 캐시로 호출량
감축, 소스별 연속 실패 알림.

설계: `docs/superpowers/specs/2026-08-04-external-api-silent-failure-design.md`
```

- [ ] **Step 3: 커밋**

```bash
git add docs/improvements.md
git commit -m "docs: 외부 데이터 소스 조용한 실패 제거 작업 기록

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## 실행 후 확인

전체 스위트: `docker compose exec web pytest`

**실제 KRX 응답으로는 검증할 수 없다.** §44-1 차단이 해제된 뒤 다음을 한 번 돌려야 한다:

```bash
docker compose exec web python -c "
from datetime import date
from app.services.data import krx_index
print(len(krx_index.index_members(date(2026, 7, 1))))
"
```

기대: 200 내외. 0이 나오면서 예외가 없다면 경계 정의가 틀린 것이므로 재검토한다.
