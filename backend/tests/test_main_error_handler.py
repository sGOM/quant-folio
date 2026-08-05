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
from app.services.data.errors import (
    SourceAuthError,
    SourceRequestError,
    SourceSchemaError,
    SourceUnavailableError,
)

_AUTH_PATH = "/__test_datasource_auth"
_UNAVAILABLE_PATH = "/__test_datasource_unavailable"
_REQUEST_PATH = "/__test_datasource_request"
_SCHEMA_PATH = "/__test_datasource_schema"
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

    @app.get(_REQUEST_PATH, include_in_schema=False)
    async def _boom_request():
        raise SourceRequestError("krx", "잘못된 파라미터")

    @app.get(_SCHEMA_PATH, include_in_schema=False)
    async def _boom_schema():
        raise SourceSchemaError("dart", "예상한 키가 없다")

    @app.get(_BUG_PATH, include_in_schema=False)
    async def _boom_bug():
        raise RuntimeError("우리 쪽 버그")

    added = {_AUTH_PATH, _UNAVAILABLE_PATH, _REQUEST_PATH, _SCHEMA_PATH, _BUG_PATH}
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


def test_우리가_잘못_보낸_요청은_503이_아니라_500(client):
    """`SourceRequestError` 는 정의상 **우리 쪽 버그**다 — 외부는 멀쩡하다.

    이걸 503("외부 소스를 사용할 수 없습니다")으로 내보내면 원인 귀속이 뒤집힌다.
    상태 코드만 보고 대응하는 알림·온콜은 "외부 장애, 복구 대기"로 오판해 정작 필요한
    코드 핫픽스를 미루게 된다. Task 1~5 가 만든 5갈래 원인 분류를 HTTP 계층에서 다시
    뭉개지 않기 위한 계약.
    """
    resp = client.get(_REQUEST_PATH)

    assert resp.status_code == 500
    body = resp.json()
    assert body["source"] == "krx"
    assert body["cause"] == "SourceRequestError"
    assert body["retryable"] is False


def test_해석_불가_응답은_502(client):
    """`SourceSchemaError` 는 상류가 계약을 바꿔 우리가 파싱하지 못하는 상태다.

    외부는 응답했으므로 '사용 불가(503)'가 아니고, 우리 요청도 틀리지 않았으므로
    500 도 아니다. 게이트웨이가 상류에서 유효하지 않은 응답을 받은 502 가 맞다.
    """
    resp = client.get(_SCHEMA_PATH)

    assert resp.status_code == 502
    assert resp.json()["cause"] == "SourceSchemaError"


def test_그_밖의_예외는_여전히_500이며_원인_정보가_없다(client):
    """전용 핸들러가 전역 핸들러를 잠식하지 않는지.

    `SourceRequestError` 도 500 이므로 상태 코드만으로는 둘을 못 가른다 — 외부 소스
    유래인지는 `source`/`cause` 필드 유무로 갈린다. 그래서 그것까지 확인한다.
    """
    resp = client.get(_BUG_PATH)

    assert resp.status_code == 500
    assert "source" not in resp.json()
