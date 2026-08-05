"""PIT 백테스트 preflight — KRX 인증 없이 19개월치를 다 돌기 전에 막는다.

미설정(KRX_ID/PW 없음) 자체는 실패가 아니라 데이터 계층을 그냥 통과한다(개발환경에서
앱은 떠야 하므로). 그래서 "없으면 결과가 무의미해지는" 용도에서는 호출자가 진입점에서
명시적으로 확인해야 한다 — 그러지 않으면 모든 월이 0종목이 되고 백테스트가 빈 패널
위에서 '성공'한다(로드맵 §44-1 에서 실제로 관측된 사고).

실 KRX 로그인은 절대 하지 않는다 — `has_krx_auth` 를 monkeypatch 로 대역화한다.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.api.routes import backtests as bt_mod
from app.services.data import krx_index
from app.services.data.errors import SourceAuthError


class TestRequireKrxAuth:
    """preflight 자체 — 미설정/로그인 실패를 실행 시작 전에 예외로 바꾼다."""

    def test_미인증이면_즉시_차단(self, monkeypatch):
        monkeypatch.setattr(krx_index, "has_krx_auth", lambda: False)
        with pytest.raises(SourceAuthError):
            krx_index.require_krx_auth()

    def test_인증되면_통과(self, monkeypatch):
        monkeypatch.setattr(krx_index, "has_krx_auth", lambda: True)
        krx_index.require_krx_auth()  # 예외 없음


class TestBuildPitPoolPreflight:
    """`_build_pit_pool` 진입점 배선 — 월별 루프에 들어가기 전에 막혀야 한다."""

    @staticmethod
    def _config(source: str) -> dict:
        return {"selection": {"universe_rule": {"source": source}}}

    def test_미인증이면_월별_조회_전에_막힌다(self, monkeypatch):
        monkeypatch.setattr(krx_index, "has_krx_auth", lambda: False)

        def _never(*args, **kwargs):
            raise AssertionError("preflight 가 막았어야 하는데 월별 조회가 나갔다")

        monkeypatch.setattr(krx_index, "index_members", _never)

        with pytest.raises(SourceAuthError):
            bt_mod._build_pit_pool(
                self._config("KOSPI200"), date(2024, 1, 1), date(2025, 7, 1)
            )

    def test_fixed_유니버스는_인증_검사를_하지_않는다(self, monkeypatch):
        """고정 유니버스는 KRX 를 아예 쓰지 않으므로 preflight 대상이 아니다."""

        def _never_auth():
            raise AssertionError("fixed 유니버스에는 KRX 인증 검사가 필요 없다")

        monkeypatch.setattr(krx_index, "has_krx_auth", _never_auth)

        assert bt_mod._build_pit_pool(
            self._config("fixed"), date(2024, 1, 1), date(2024, 2, 1)
        ) == (None, None)

    def test_인증되면_월별_조회가_진행된다(self, monkeypatch):
        monkeypatch.setattr(krx_index, "has_krx_auth", lambda: True)
        monkeypatch.setattr(krx_index, "index_members", lambda as_of, idx: ["005930"])

        union, provider = bt_mod._build_pit_pool(
            self._config("KOSPI200"), date(2024, 1, 1), date(2024, 2, 1)
        )

        assert union == ["005930"]
        assert provider(date(2024, 2, 10)) == ["005930"]
