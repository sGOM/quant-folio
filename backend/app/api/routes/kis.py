"""증권사 연동 라우트 — 자격증명 등록, 연동 검증, 현재가 조회.

브로커(kis|toss)는 사용자 설정(User.broker)에 따라 팩토리가 주입한다.
경로 prefix(/api/kis)와 필드명(kis_*)은 하위호환을 위해 유지한다.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.security import encrypt_secret
from app.models import User
from app.schemas.auth import KisCredentialsIn, UserOut
from app.schemas.kis import KisHealth, QuoteOut
from app.services.broker import (
    BrokerClient,
    BrokerError,
    make_broker_for_user,
    resolve_quote_client,
    user_has_toss_quote,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/kis", tags=["kis"])


def _client_for(user: User) -> BrokerClient:
    """사용자의 암호화된 자격증명을 복호화해 브로커 클라이언트를 만든다.

    :raises HTTPException: 자격증명이 등록되지 않았으면 400
    """
    try:
        return make_broker_for_user(user)
    except BrokerError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


def _env_label(user: User) -> tuple[str, bool]:
    """(env 라벨, 모의투자 여부)를 브로커별로 반환한다.

    토스는 모의투자 환경이 없어 항상 실거래(prod)다.
    """
    if (user.broker or "kis") == "toss":
        return "prod", False
    return settings.KIS_ENV, settings.is_paper_trading


@router.put("/credentials", response_model=UserOut)
async def register_credentials(
    payload: KisCredentialsIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """증권사 app_key/secret/계좌 등록. 키·시크릿은 암호화 저장한다."""
    current.broker = payload.broker
    current.kis_app_key = encrypt_secret(payload.kis_app_key)
    current.kis_app_secret = encrypt_secret(payload.kis_app_secret)
    current.kis_account_no = payload.kis_account_no
    await db.commit()
    await db.refresh(current)
    return UserOut(
        id=current.id, email=current.email, broker=current.broker,
        kis_account_no=current.kis_account_no, has_kis_credentials=True,
        has_toss_quote=user_has_toss_quote(current),
    )


@router.put("/toss-quote", response_model=UserOut)
async def register_toss_quote(
    payload: KisCredentialsIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """통합 시세(국내+해외)용 토스 자격증명 등록. 주문 브로커(User.broker)와 독립이며,
    등록 후 워치리스트 시세 조회가 토스로 통합된다. payload.broker 는 무시한다.
    """
    current.toss_app_key = encrypt_secret(payload.kis_app_key)
    current.toss_app_secret = encrypt_secret(payload.kis_app_secret)
    current.toss_account_no = payload.kis_account_no
    await db.commit()
    await db.refresh(current)
    return UserOut(
        id=current.id, email=current.email, broker=current.broker,
        kis_account_no=current.kis_account_no,
        has_kis_credentials=bool(current.kis_app_key),
        has_toss_quote=user_has_toss_quote(current),
    )


@router.get("/health", response_model=KisHealth)
async def kis_health(current: User = Depends(get_current_user)):
    """등록된 자격증명으로 토큰을 발급해 증권사 연동을 검증한다."""
    client = _client_for(current)
    env, is_paper = _env_label(current)
    try:
        await client.verify_connection()
    except BrokerError as e:
        logger.warning("증권사 연동 검증 실패(broker=%s): %s", current.broker, e)
        return KisHealth(
            broker=current.broker, env=env, is_paper_trading=is_paper,
            token_issued=False, message=f"연동 실패: {e}",
        )
    return KisHealth(
        broker=current.broker, env=env, is_paper_trading=is_paper,
        token_issued=True, message="토큰 발급 성공 — 연동 정상",
    )


@router.get("/quote/{symbol}", response_model=QuoteOut)
async def get_quote(symbol: str, current: User = Depends(get_current_user)):
    """현재가 조회. 예: 005930(국내), AAPL(해외).

    통합 시세(토스) 자격증명이 있으면 토스로 국내·해외를 통합 조회하고,
    없으면 주문 브로커(User.broker)로 조회한다(국내 한정). 토스는 가격 외 항목이 0일 수 있음.
    """
    client = resolve_quote_client(current) or _client_for(current)
    try:
        q = await client.get_quote(symbol)
    except BrokerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    return QuoteOut(
        symbol=q.symbol,
        price=float(q.price),
        change=float(q.change),
        change_rate=float(q.change_rate),
        volume=q.volume,
        high=float(q.high),
        low=float(q.low),
        open=float(q.open),
        currency=q.currency,
    )
