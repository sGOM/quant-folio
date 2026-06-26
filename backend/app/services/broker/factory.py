"""브로커 팩토리 — 사용자 설정(User.broker)에 따라 적절한 클라이언트를 생성한다.

자격증명 우선순위:
  1) 사용자가 앱에서 등록한 DB 값(암호화 저장) — 멀티 유저 운영의 정식 경로.
  2) 없으면 .env 의 기본 자격증명(KIS_APP_KEY 등) 폴백 — 단일 운영자(개인) 편의.

자격증명 컬럼/환경변수의 의미는 브로커별로 다음과 같이 대응한다:
- kis : app_key / app_secret / 계좌번호(CANO-PRDT)
- toss: client_id / client_secret / accountSeq
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.security import decrypt_secret
from app.services.broker.base import BrokerClient, BrokerError

if TYPE_CHECKING:
    from app.models import User

# 지원 브로커 목록(스키마 검증·UI 와 공유).
SUPPORTED_BROKERS = ("kis", "toss")
DEFAULT_BROKER = "kis"


def make_broker(
    broker: str, app_key: str, app_secret: str, account_no: str | None = None
) -> BrokerClient:
    """복호화된 자격증명으로 브로커 클라이언트를 생성한다."""
    # 순환 임포트 방지를 위해 함수 내부에서 임포트.
    if broker == "kis":
        from app.services.kis import KisClient

        return KisClient(app_key=app_key, app_secret=app_secret, account_no=account_no)
    if broker == "toss":
        from app.services.broker.toss import TossClient

        return TossClient(app_key=app_key, app_secret=app_secret, account_no=account_no)
    raise BrokerError(f"지원하지 않는 브로커: {broker}")


def _env_credentials(broker: str) -> tuple[str, str, str | None] | None:
    """.env 의 기본 자격증명을 (app_key, app_secret, account_no) 로 반환(없으면 None)."""
    if broker == "kis" and settings.KIS_APP_KEY and settings.KIS_APP_SECRET:
        return settings.KIS_APP_KEY, settings.KIS_APP_SECRET, settings.KIS_ACCOUNT_NO or None
    if broker == "toss" and settings.TOSS_APP_KEY and settings.TOSS_APP_SECRET:
        return settings.TOSS_APP_KEY, settings.TOSS_APP_SECRET, settings.TOSS_ACCOUNT_NO or None
    return None


def resolve_credentials(user: "User") -> tuple[str, str, str, str | None] | None:
    """사용자의 유효 자격증명을 (broker, app_key, app_secret, account_no) 로 해석한다.

    DB 등록값을 우선하고, 없으면 .env 폴백을 사용한다. 둘 다 없으면 None.
    """
    broker = getattr(user, "broker", None) or DEFAULT_BROKER
    if user.kis_app_key and user.kis_app_secret:
        return (
            broker,
            decrypt_secret(user.kis_app_key),
            decrypt_secret(user.kis_app_secret),
            user.kis_account_no,
        )
    env = _env_credentials(broker)
    if env:
        return (broker, env[0], env[1], env[2])
    return None


def user_has_credentials(user: "User") -> bool:
    """사용자가 사용할 수 있는 자격증명(DB 또는 .env 폴백)이 있는지."""
    return resolve_credentials(user) is not None


def make_broker_for_user(user: "User") -> BrokerClient:
    """사용자의 유효 자격증명(DB 우선, .env 폴백)으로 브로커 클라이언트를 만든다.

    :raises BrokerError: 사용 가능한 자격증명이 전혀 없는 경우
    """
    resolved = resolve_credentials(user)
    if resolved is None:
        raise BrokerError("증권사 API 자격증명이 등록되지 않았습니다.")
    broker, app_key, app_secret, account_no = resolved
    return make_broker(broker, app_key, app_secret, account_no)


def resolve_quote_client(user: "User") -> BrokerClient | None:
    """통합 시세(국내+해외)용 토스 클라이언트를 만든다(없으면 None).

    토스는 국내·해외 시세를 모두 제공하므로, 토스 자격증명이 있으면 주문 브로커와
    무관하게 시세 조회를 토스로 통합한다. 우선순위: 사용자 toss_*(DB) → .env TOSS_*.
    호출자(시세 라우트)는 None 이면 주문 브로커(make_broker_for_user)로 폴백한다.
    """
    from app.services.broker.toss import TossClient

    if user.toss_app_key and user.toss_app_secret:
        return TossClient(
            app_key=decrypt_secret(user.toss_app_key),
            app_secret=decrypt_secret(user.toss_app_secret),
            account_no=user.toss_account_no,
        )
    env = _env_credentials("toss")
    if env:
        return TossClient(app_key=env[0], app_secret=env[1], account_no=env[2])
    return None


def user_has_toss_quote(user: "User") -> bool:
    """사용자가 통합 시세(토스) 자격증명(DB 또는 .env)을 보유하는지."""
    return resolve_quote_client(user) is not None
