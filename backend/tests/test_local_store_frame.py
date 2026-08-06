"""로컬 영구 저장소의 원장·조회 계약 검증.

실제 DB 를 쓰지 않는다. 원장은 InMemoryLedger, 로컬 저장소는 dict 대역으로 갈음하고
"몇 번 외부를 호출했는가"만 본다 — 이 계층의 책임이 그것뿐이기 때문이다.
"""
from app.services.data.store.ledger import InMemoryLedger, LedgerEntry


def test_인메모리_원장은_기록이_없으면_None_을_준다():
    ledger = InMemoryLedger()
    assert ledger.get("fundamentals", "20190312|KOSPI") is None


def test_인메모리_원장은_기록을_되돌려준다():
    ledger = InMemoryLedger()
    ledger.put("fundamentals", "20190312|KOSPI", row_count=812, final=True)
    assert ledger.get("fundamentals", "20190312|KOSPI") == LedgerEntry(
        row_count=812, final=True
    )


def test_인메모리_원장은_0행_기록과_미기록을_구분한다():
    """이 구분이 이 저장소의 존재 이유다 — 휴장일과 미적재가 같은 값이면 안 된다."""
    ledger = InMemoryLedger()
    ledger.put("fundamentals", "20190101|KOSPI", row_count=0, final=True)
    assert ledger.get("fundamentals", "20190101|KOSPI") == LedgerEntry(
        row_count=0, final=True
    )
    assert ledger.get("fundamentals", "20190102|KOSPI") is None


def test_인메모리_원장은_같은_키를_덮어쓴다():
    ledger = InMemoryLedger()
    ledger.put("index_ohlcv", "1001|20260806", row_count=1, final=False)
    ledger.put("index_ohlcv", "1001|20260806", row_count=1, final=True)
    assert ledger.get("index_ohlcv", "1001|20260806").final is True
