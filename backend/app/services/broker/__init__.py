"""브로커 추상화 패키지 — 증권사 공통 인터페이스와 팩토리.

엔진·라우트는 구체 클라이언트(KisClient/TossClient) 대신 이 패키지의
BrokerClient 인터페이스와 make_broker* 팩토리에만 의존한다.
"""
from app.services.broker.base import (
    Balance,
    BrokerClient,
    BrokerError,
    Fill,
    OrderResult,
    Quote,
)
from app.services.broker.factory import (
    DEFAULT_BROKER,
    SUPPORTED_BROKERS,
    make_broker,
    make_broker_for_user,
    resolve_credentials,
    resolve_quote_client,
    user_has_credentials,
    user_has_toss_quote,
)

__all__ = [
    "Balance",
    "BrokerClient",
    "BrokerError",
    "Fill",
    "OrderResult",
    "Quote",
    "DEFAULT_BROKER",
    "SUPPORTED_BROKERS",
    "make_broker",
    "make_broker_for_user",
    "resolve_credentials",
    "resolve_quote_client",
    "user_has_credentials",
    "user_has_toss_quote",
]
