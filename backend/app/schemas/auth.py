"""인증/사용자 관련 Pydantic 스키마."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    broker: Literal["kis", "toss"] = "kis"
    kis_account_no: str | None = None
    has_kis_credentials: bool = False
    # 통합 시세(토스) 연동 여부 — 국내·해외 시세를 토스로 통합 조회할 수 있는지.
    has_toss_quote: bool = False


class KisCredentialsIn(BaseModel):
    """증권사 API 자격증명 등록.

    broker 에 따라 필드 의미가 달라진다:
      - kis : app_key / app_secret / 계좌번호(CANO-PRDT)
      - toss: client_id / client_secret / accountSeq
    필드명은 호환을 위해 kis_* 를 유지한다.
    """
    broker: Literal["kis", "toss"] = "kis"
    kis_app_key: str = Field(min_length=1)
    kis_app_secret: str = Field(min_length=1)
    kis_account_no: str = Field(min_length=1, max_length=32)
