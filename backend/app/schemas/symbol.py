"""종목 검색 스키마."""
from pydantic import BaseModel


class SymbolOut(BaseModel):
    """검색된 종목 1건."""
    code: str
    name: str
    name_en: str = ""
    market: str = ""
