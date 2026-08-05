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
    stop_aggregate,
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


class TestStopAggregate:
    """집계 루프 단락 판단 — 쿨다운뿐 아니라 '쿨다운이 없는 체계적 실패'도 멈춘다.

    Schema·Request 는 기다려도 해결되지 않아 쿨다운이 없다(설계). 그런데 집계 단락이
    쿨다운만 보면, DART 포맷이 바뀐 날 전 종목 루프가 끝까지 돌며 종목당 3회씩 호출을
    소진한 뒤에야 raise 한다 — 일일 20,000건 한도가 있고, 리밸런싱일마다 부르는 백테스트
    한 건이면 한도를 통째로 태울 수 있다.
    """

    def setup_method(self):
        clear_cooldown("dart")

    def teardown_method(self):
        clear_cooldown("dart")

    def test_실패가_없으면_계속한다(self):
        assert stop_aggregate("dart", [], ok=0) is False

    def test_쿨다운_중이면_멈춘다(self):
        note_failure(SourceQuotaError("dart", "한도"))
        assert stop_aggregate("dart", [SourceUnavailableError("dart", "x")], ok=5) is True

    def test_스키마_오류가_연속이고_성공이_없으면_멈춘다(self):
        """스키마 불일치는 종목별 사정이 아니라 응답 형식 문제다 — 체계적이다."""
        errs = [SourceSchemaError("dart", f"키 없음 {i}") for i in range(3)]
        assert stop_aggregate("dart", errs, ok=0) is True

    def test_한_번이라도_성공했으면_스키마_오류여도_계속한다(self):
        """성공이 있었다면 형식은 맞다 — 나머지는 종목별 사정이므로 부분 결과를 지킨다."""
        errs = [SourceSchemaError("dart", f"키 없음 {i}") for i in range(3)]
        assert stop_aggregate("dart", errs, ok=1) is False

    def test_스키마가_아닌_실패는_임계에_도달해도_계속한다(self):
        """Request 는 종목별로 날 수 있다 — 앞 몇 개가 실패했다고 나머지를 포기하면
        돌려줄 수 있었던 부분 결과를 잃는다."""
        errs = [SourceRequestError("dart", f"잘못된 인자 {i}") for i in range(5)]
        assert stop_aggregate("dart", errs, ok=0) is False

    def test_임계_미만이면_계속한다(self):
        errs = [SourceSchemaError("dart", "키 없음")]
        assert stop_aggregate("dart", errs, ok=0) is False
