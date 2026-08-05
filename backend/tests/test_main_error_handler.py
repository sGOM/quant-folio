"""외부 데이터 소스 장애는 서버 버그가 아니므로 500 이 아니라 503 이어야 한다.

전역 `Exception` 핸들러만 있으면 `DataSourceError` 가 500(Internal Server Error)으로
나가 "우리 코드가 깨졌다"로 읽힌다. 실제로는 KRX/DART/KOFIA 가 일시적으로 응답하지
않는 **의존성 장애**이므로 503 + 어떤 소스가 무슨 원인으로 실패했는지를 돌려준다.

실 외부 호출은 없다 — 앱에 임시 라우트를 달아 예외만 발생시킨다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.data.errors import SourceAuthError, SourceUnavailableError

_AUTH_PATH = "/__test_datasource_auth"
_UNAVAILABLE_PATH = "/__test_datasource_unavailable"
_BUG_PATH = "/__test_internal_bug"


@pytest.fixture()
def client():
    """테스트 전용 라우트를 임시로 달았다가 뗀다(앱 전역 오염 방지)."""

    @app.get(_AUTH_PATH, include_in_schema=False)
    async def _boom_auth():
        raise SourceAuthError("krx", "차단")

    @app.get(_UNAVAILABLE_PATH, include_in_schema=False)
    async def _boom_unavailable():
        raise SourceUnavailableError("dart", "연결 실패")

    @app.get(_BUG_PATH, include_in_schema=False)
    async def _boom_bug():
        raise RuntimeError("우리 쪽 버그")

    added = {_AUTH_PATH, _UNAVAILABLE_PATH, _BUG_PATH}
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) not in added
        ]
        app.openapi_schema = None


def test_DataSourceError는_503과_원인을_반환한다(client):
    resp = client.get(_AUTH_PATH)

    assert resp.status_code == 503
    body = resp.json()
    assert body["source"] == "krx"
    assert body["cause"] == "SourceAuthError"
    assert body["retryable"] is False


def test_재시도_가능한_원인은_retryable_True(client):
    resp = client.get(_UNAVAILABLE_PATH)

    assert resp.status_code == 503
    body = resp.json()
    assert body["source"] == "dart"
    assert body["cause"] == "SourceUnavailableError"
    assert body["retryable"] is True


def test_그_밖의_예외는_여전히_500(client):
    """전용 핸들러가 전역 핸들러를 잠식하지 않는지(우리 쪽 버그는 계속 500)."""
    assert client.get(_BUG_PATH).status_code == 500
