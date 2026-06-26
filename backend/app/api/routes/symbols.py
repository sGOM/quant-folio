"""종목 검색 라우트 — 코드/한글/영문으로 KRX 종목을 찾는다."""
import asyncio

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user
from app.models import User
from app.schemas.symbol import SymbolOut
from app.services.symbols import search_symbols

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


@router.get("/search", response_model=list[SymbolOut])
async def search(
    q: str = Query(..., min_length=1, max_length=40, description="검색어(코드/한글/영문)"),
    limit: int = Query(20, ge=1, le=50),
    _: User = Depends(get_current_user),
):
    """종목을 검색한다. 카탈로그 빌드(최초 1회)는 블로킹이라 스레드풀에서 실행한다."""
    return await asyncio.to_thread(search_symbols, q, limit)
