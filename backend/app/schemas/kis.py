"""KIS 연동 응답 스키마."""
from pydantic import BaseModel


class KisHealth(BaseModel):
    """증권사 연동 검증 결과."""
    broker: str = "kis"
    env: str
    is_paper_trading: bool
    token_issued: bool
    message: str


class QuoteOut(BaseModel):
    """현재가 조회 결과."""
    symbol: str
    price: float
    change: float
    change_rate: float
    volume: int
    high: float
    low: float
    open: float
    currency: str = "KRW"
