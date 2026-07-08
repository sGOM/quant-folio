"""종목 카탈로그·이름맵 — KRX MDC 병합과 외부 실패 시 자가복구(비캐시) 검증.

체결/주문 로그가 종목코드만 뜨던 회귀(외부 소스 전부 실패 → seed 만 → 영구 캐시)를
방지한다. FDR/pykrx 가 이 환경에서 불안정하므로, KRX MDC(all_listed_stocks) 를 1차
소스로 병합하고, 외부가 모두 실패하면 seed 결과를 캐시하지 않아 다음 호출에 복구된다.
"""
import pytest

from app.services import symbols


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    symbols._cache = None
    # FDR 는 이 테스트에서 항상 실패(무시)로 고정 — KRX 소스 경로만 검증.
    monkeypatch.setattr(
        "FinanceDataReader.StockListing",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("FDR down")),
    )
    yield
    symbols._cache = None


def _patch_krx(monkeypatch, stocks):
    import app.services.data.krx_index as ki
    monkeypatch.setattr(ki, "all_listed_stocks", lambda: stocks)


def test_catalog_merges_krx_and_caches(monkeypatch):
    _patch_krx(monkeypatch, [
        {"code": "060310", "name": "3S", "market": "KOSDAQ"},
        {"code": "005930", "name": "삼성전자", "market": "KOSPI"},  # seed 와 중복(영문명 보존)
    ])
    m = symbols.get_name_map()
    assert m["060310"] == "3S"          # seed 밖 종목도 이름 확보
    assert m["005930"] == "삼성전자"
    assert symbols._cache is not None    # 외부 성공 → 캐시됨
    # 내장 영문명은 보존.
    en = {i["code"]: i["name_en"] for i in symbols.get_catalog()}
    assert en["005930"] == "Samsung Electronics"


def test_catalog_not_cached_when_all_external_fail(monkeypatch):
    _patch_krx(monkeypatch, [])  # KRX 실패(빈 목록), FDR 도 실패(fixture)
    m = symbols.get_name_map()
    # seed 만 존재하고 캐시는 보류(자가복구) — 다음 호출에 재시도 가능.
    assert m["005930"] == "삼성전자"
    assert symbols._cache is None

    # 이후 KRX 가 복구되면 다음 호출에서 정상 병합·캐시.
    _patch_krx(monkeypatch, [{"code": "060310", "name": "3S", "market": "KOSDAQ"}])
    m2 = symbols.get_name_map()
    assert m2["060310"] == "3S"
    assert symbols._cache is not None
