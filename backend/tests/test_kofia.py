"""FreeSIS 증시자금 클라이언트(app/services/data/kofia.py) 단위 테스트.

외부 호출은 httpx.post 를 대역으로 바꿔 검증한다(네트워크 의존 없음).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.data import kofia


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _row(ymd: str, t5, t6, t7, **extra) -> dict:
    return {"TMPV1": ymd, "TMPV5": t5, "TMPV6": t6, "TMPV7": t7, **extra}


def _patch(monkeypatch, payload: dict, captured: dict | None = None):
    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        if captured is not None:
            captured["url"] = url
            captured["json"] = json
        return _Resp(payload)

    monkeypatch.setattr(kofia.httpx, "post", fake_post)


def test_행을_날짜_오름차순으로_파싱한다(monkeypatch):
    _patch(monkeypatch, {"ds1": [
        _row("20260730", 1_724_914_408_635, 103_788_544_148, 8.6),
        _row("20260729", 1_199_966_208_631, 61_133_143_844, 5.0),
    ]})
    out = kofia.fetch_market_funds(date(2026, 7, 29), date(2026, 7, 30))
    assert [m.as_of for m in out] == [date(2026, 7, 29), date(2026, 7, 30)]
    assert out[1].receivable == pytest.approx(1_724_914_408_635)
    assert out[1].forced_sale_amount == pytest.approx(103_788_544_148)
    assert out[1].forced_sale_ratio == pytest.approx(8.6)


def test_의미가_확정되지_않은_컬럼은_raw_로_보존한다(monkeypatch):
    # TMPV2~TMPV4 는 의미 미확정이라 필드명을 붙이지 않지만 잃어서도 안 된다.
    _patch(monkeypatch, {"ds1": [
        _row("20260730", 1, 2, 3.0, TMPV2=104_658_382_450_971, TMPV3=44_158_649_671_631),
    ]})
    out = kofia.fetch_market_funds(date(2026, 7, 30), date(2026, 7, 30))
    assert out[0].raw["TMPV2"] == 104_658_382_450_971
    assert out[0].raw["TMPV3"] == 44_158_649_671_631


def test_반대매매_비중은_전일_미수금_기준이라는_관계가_유지된다(monkeypatch):
    # 컬럼 식별의 근거가 된 관계(41/41 성립). 회귀 방지용으로 고정한다.
    _patch(monkeypatch, {"ds1": [
        _row("20260729", 1_199_966_208_631, 61_133_143_844, 5.0),
        _row("20260730", 1_724_914_408_635, 103_788_544_148, 8.6),
    ]})
    prev, cur = kofia.fetch_market_funds(date(2026, 7, 29), date(2026, 7, 30))
    calc = cur.forced_sale_amount / prev.receivable * 100
    assert calc == pytest.approx(cur.forced_sale_ratio, abs=0.1)


def test_조회_실패는_예외가_아니라_빈_리스트다(monkeypatch):
    # 취약성 관측이 없다고 매매·백테스트가 멈추면 안 된다.
    def boom(*a, **kw):
        raise RuntimeError("네트워크 장애")

    monkeypatch.setattr(kofia.httpx, "post", boom)
    assert kofia.fetch_market_funds(date(2026, 7, 1), date(2026, 7, 31)) == []


def test_응답에_ds1_이_없어도_빈_리스트다(monkeypatch):
    _patch(monkeypatch, {})
    assert kofia.fetch_market_funds(date(2026, 7, 1), date(2026, 7, 31)) == []


def test_날짜가_깨진_행은_건너뛴다(monkeypatch):
    _patch(monkeypatch, {"ds1": [
        _row("", 1, 2, 3.0),
        _row("2026073", 1, 2, 3.0),
        _row("20260730", 1, 2, 3.0),
    ]})
    out = kofia.fetch_market_funds(date(2026, 7, 1), date(2026, 7, 31))
    assert [m.as_of for m in out] == [date(2026, 7, 30)]


def test_숫자가_없으면_0_이_아니라_None_이다(monkeypatch):
    # 결측을 0 으로 채우면 '반대매매가 전혀 없었다'는 거짓 관측이 된다.
    _patch(monkeypatch, {"ds1": [_row("20260730", None, "", "N/A")]})
    m = kofia.fetch_market_funds(date(2026, 7, 30), date(2026, 7, 30))[0]
    assert (m.receivable, m.forced_sale_amount, m.forced_sale_ratio) == (None, None, None)


def test_구간이_뒤집히면_거부한다(monkeypatch):
    _patch(monkeypatch, {"ds1": []})
    with pytest.raises(ValueError):
        kofia.fetch_market_funds(date(2026, 7, 31), date(2026, 7, 1))


def test_요청에_조회구간과_통계표_코드가_실린다(monkeypatch):
    captured: dict = {}
    _patch(monkeypatch, {"ds1": []}, captured)
    kofia.fetch_market_funds(date(2008, 10, 1), date(2008, 10, 31))
    search = captured["json"]["dmSearch"]
    assert (search["tmpV45"], search["tmpV46"]) == ("20081001", "20081031")
    assert search["OBJ_NM"] == kofia._OBJ_MARKET_FUNDS


class TestCreditBalance:
    def test_신용융자_합계와_구성요소를_파싱한다(self, monkeypatch):
        _patch(monkeypatch, {"ds1": [
            {"TMPV1": "20260730", "TMPV2": 32_150_000_000_000,
             "TMPV3": 25_510_000_000_000, "TMPV4": 6_640_000_000_000,
             "TMPV9": 25_820_000_000_000},
        ]})
        c = kofia.fetch_credit_balance(date(2026, 7, 30), date(2026, 7, 30))[0]
        assert c.as_of == date(2026, 7, 30)
        assert c.total == pytest.approx(32_150_000_000_000)
        assert c.part_a == pytest.approx(25_510_000_000_000)
        assert c.part_b == pytest.approx(6_640_000_000_000)

    def test_합계가_구성요소의_합이라는_관계가_유지된다(self, monkeypatch):
        # 컬럼 식별의 근거(42/42 성립). 회귀 방지용으로 고정한다.
        _patch(monkeypatch, {"ds1": [
            {"TMPV1": "20260601", "TMPV2": 37_680_000_000_000,
             "TMPV3": 27_850_000_000_000, "TMPV4": 9_840_000_000_000},
        ]})
        c = kofia.fetch_credit_balance(date(2026, 6, 1), date(2026, 6, 1))[0]
        assert c.part_a + c.part_b == pytest.approx(c.total, rel=1e-3)

    def test_의미가_확정되지_않은_잔여_컬럼은_raw_로_보존한다(self, monkeypatch):
        _patch(monkeypatch, {"ds1": [
            {"TMPV1": "20260730", "TMPV2": 1, "TMPV3": 1, "TMPV4": 0,
             "TMPV9": 25_820_000_000_000},
        ]})
        c = kofia.fetch_credit_balance(date(2026, 7, 30), date(2026, 7, 30))[0]
        assert c.raw["TMPV9"] == 25_820_000_000_000

    def test_조회_실패는_예외가_아니라_빈_리스트다(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("네트워크 장애")

        monkeypatch.setattr(kofia.httpx, "post", boom)
        assert kofia.fetch_credit_balance(date(2026, 7, 1), date(2026, 7, 31)) == []

    def test_구간이_뒤집히면_거부한다(self, monkeypatch):
        _patch(monkeypatch, {"ds1": []})
        with pytest.raises(ValueError):
            kofia.fetch_credit_balance(date(2026, 7, 31), date(2026, 7, 1))

    def test_신용융자는_증시자금과_다른_통계표를_조회한다(self, monkeypatch):
        captured: dict = {}
        _patch(monkeypatch, {"ds1": []}, captured)
        kofia.fetch_credit_balance(date(2026, 7, 1), date(2026, 7, 31))
        assert captured["json"]["dmSearch"]["OBJ_NM"] == kofia._OBJ_CREDIT
        assert kofia._OBJ_CREDIT != kofia._OBJ_MARKET_FUNDS
