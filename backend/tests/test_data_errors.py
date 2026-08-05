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
