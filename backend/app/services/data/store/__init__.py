"""확정 과거 데이터의 로컬 영구 저장소.

설계: docs/superpowers/specs/2026-08-06-local-persistent-store-design.md

공개 진입점은 frame.cached_frame 하나다. 나머지 모듈(ledger·daily·periods·indexes·
dart_store)은 그 뒤에서 테이블별 읽기/쓰기를 담당한다.
"""
from app.services.data.store.ledger import (
    InMemoryLedger,
    Ledger,
    LedgerEntry,
    SqlLedger,
    default_ledger,
)

__all__ = [
    "Ledger",
    "LedgerEntry",
    "InMemoryLedger",
    "SqlLedger",
    "default_ledger",
]
