"""토스증권 Open API 클라이언트 (BrokerClient 구현).

토스 API 특성(2026-06 공개 기준):
- 인증: OAuth2 Client Credentials (`POST /oauth2/token`, form-encoded).
- 계좌/주문 호출에는 `X-Tossinvest-Account` 헤더(accountSeq)가 추가로 필요하다.
  본 시스템은 사용자 자격증명을 (client_id, client_secret, accountSeq) 로 보고
  기존 암호화 컬럼(app_key/app_secret/account_no)을 재사용해 저장한다.
- **REST 전용 — WebSocket 실시간 시세 미지원.** 따라서 엔진은 토스 사용자에 대해
  WS 피드를 띄우지 않고 runner 의 REST 폴링(get_quote)으로 현재가를 얻는다.
  토스가 향후 WS 를 제공하면 별도 WS 클라이언트를 PriceFeed 에 연동한다.
- 모의투자(sandbox) 환경이 없으므로 토스 자격증명은 항상 실거래로 동작한다.
- 그룹별 rate limit 이 있어 429 시 Retry-After 를 존중해 1회 재시도한다.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import httpx

from app.core.config import settings
from app.core.redis import redis_client
from app.services.broker.base import (
    Balance,
    BrokerError,
    Fill,
    OrderResult,
    Quote,
)

logger = logging.getLogger(__name__)

_TOKEN_TTL_SAFETY = 60   # 만료 직전 안전 마진(초)
_TOKEN_LOCK_TTL = 10     # 발급 락 TTL(초)
_TOKEN_LOCK_WAIT = 8     # 락 대기 동안 캐시 재확인 횟수
_MAX_RETRY_AFTER = 5     # 429 재시도 시 대기 상한(초)


class TossError(BrokerError):
    """토스 API 호출 실패. BrokerError 하위 — `except BrokerError` 로도 잡힌다."""


# 정규화 주문구분 → 토스 enum.
_SIDE = {"buy": "BUY", "sell": "SELL"}
_ORDER_TYPE = {"market": "MARKET", "limit": "LIMIT"}


class TossClient:
    """사용자별 토스 자격증명으로 동작하는 클라이언트.

    :param app_key: 토스 client_id (식별자 — 시크릿 아님)
    :param app_secret: 토스 client_secret
    :param account_no: 토스 accountSeq (X-Tossinvest-Account 헤더 값)
    """

    def __init__(self, app_key: str, app_secret: str, account_no: str | None = None):
        self._client_id = app_key
        self._client_secret = app_secret
        self._account = account_no
        self._base_url = settings.TOSS_BASE_URL.rstrip("/")

    # ───────────────────── 토큰 ─────────────────────
    def _token_cache_key(self) -> str:
        # client_id 는 식별자 용도로만 사용(시크릿 자체는 저장하지 않음).
        return f"toss:token:{self._client_id}"

    async def _request_token(self) -> tuple[str, int]:
        url = f"{self._base_url}/oauth2/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, data=data)
        if resp.status_code != 200:
            raise TossError(f"토큰 발급 실패: HTTP {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        token = body.get("access_token")
        expires_in = int(body.get("expires_in", 86400))
        if not token:
            raise TossError(f"토큰 응답에 access_token 없음: {body}")
        return token, expires_in

    async def get_access_token(self) -> str:
        """캐시된 토큰을 반환하거나 신규 발급(Redis 분산 락으로 single-flight)."""
        cache_key = self._token_cache_key()
        cached = await redis_client.get(cache_key)
        if cached:
            return cached

        lock_key = f"{cache_key}:lock"
        got_lock = await redis_client.set(lock_key, "1", nx=True, ex=_TOKEN_LOCK_TTL)
        if not got_lock:
            for _ in range(_TOKEN_LOCK_WAIT):
                await asyncio.sleep(0.5)
                cached = await redis_client.get(cache_key)
                if cached:
                    return cached
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return cached
            token, expires_in = await self._request_token()
            ttl = max(expires_in - _TOKEN_TTL_SAFETY, 60)
            await redis_client.set(cache_key, token, ex=ttl)
            logger.info("토스 토큰 신규 발급 (ttl=%ss)", ttl)
            return token
        finally:
            if got_lock:
                await redis_client.delete(lock_key)

    async def _headers(self, *, with_account: bool = False) -> dict[str, str]:
        # 계좌 필요한 호출은 토큰 발급 전에 먼저 검증(불필요한 발급 방지·빠른 실패).
        if with_account and not self._account:
            raise TossError("토스 계좌(accountSeq)가 등록되지 않았습니다.")
        token = await self.get_access_token()
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        if with_account:
            headers["X-Tossinvest-Account"] = str(self._account)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        with_account: bool = False,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        """토스 REST 호출. 429 면 Retry-After 만큼(상한 내) 대기 후 1회 재시도."""
        url = f"{self._base_url}{path}"
        for attempt in range(2):
            headers = await self._headers(with_account=with_account)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.request(
                    method, url, headers=headers, params=params, json=json
                )
            if resp.status_code == 429 and attempt == 0:
                wait = _retry_after_seconds(resp)
                logger.warning("토스 rate limit(429) — %.1fs 후 재시도 %s", wait, path)
                await asyncio.sleep(wait)
                continue
            if resp.status_code not in (200, 201):
                raise TossError(
                    f"{method} {path} 실패: HTTP {resp.status_code} {resp.text[:200]}"
                )
            return resp.json() if resp.content else {}
        raise TossError(f"{method} {path} 실패: 429 재시도 후에도 실패")

    # ───────────────────── 인터페이스 구현 ─────────────────────
    async def verify_connection(self) -> bool:
        """토큰 발급으로 자격증명 유효성 검증."""
        await self.get_access_token()
        return True

    async def get_quote(self, symbol: str) -> Quote:
        """현재가 시세. 토스 /prices 는 lastPrice 만 제공하므로 등락·고저·시가는 0.

        응답 형식: {"result": [{"symbol", "lastPrice", "currency", ...}]}.
        """
        data = await self._request("GET", "/api/v1/prices", params={"symbols": symbol})
        # 토스는 {"result": [...]} 로 감싸 반환한다(list/prices/data 형태도 방어적으로 지원).
        if isinstance(data, list):
            items = data
        else:
            items = data.get("result") or data.get("prices") or data.get("data") or []
        row = next((p for p in items if p.get("symbol") == symbol), items[0] if items else None)
        if not row or row.get("lastPrice") in (None, ""):
            raise TossError(f"시세 응답 형식 오류: {data}")
        try:
            price = Decimal(str(row["lastPrice"]))
        except (ValueError, ArithmeticError) as e:
            raise TossError(f"시세 가격 파싱 실패: {row}") from e
        return Quote(symbol=symbol, price=price, currency=str(row.get("currency") or "KRW"))

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: int = 0,
        order_type: str = "market",
    ) -> OrderResult:
        """현금 주문. side='buy'|'sell', order_type='market'|'limit'."""
        toss_side = _SIDE.get(side)
        toss_type = _ORDER_TYPE.get(order_type)
        if toss_side is None:
            raise TossError(f"알 수 없는 주문 방향: {side}")
        if toss_type is None:
            raise TossError(f"알 수 없는 주문 유형: {order_type}")
        body: dict = {
            "symbol": symbol,
            "side": toss_side,
            "orderType": toss_type,
            "quantity": int(qty),
        }
        if toss_type == "LIMIT":
            body["price"] = int(price)
        data = await self._request(
            "POST", "/api/v1/orders", with_account=True, json=body
        )
        order_id = data.get("orderId") or data.get("order_id")
        if not order_id:
            raise TossError(f"주문 응답에 orderId 없음: {data}")
        return OrderResult(order_id=str(order_id), raw=data)

    async def get_order_execution(
        self, order_id: str, symbol: str | None = None
    ) -> Fill:
        """주문 상세 조회로 실제 체결수량·평균체결가를 얻는다."""
        data = await self._request(
            "GET", f"/api/v1/orders/{order_id}", with_account=True
        )
        execution = data.get("execution") or {}
        try:
            filled_qty = int(float(execution.get("filledQuantity") or 0))
        except (TypeError, ValueError):
            filled_qty = 0
        avg_raw = execution.get("averageFilledPrice")
        try:
            avg_price = (
                Decimal(str(avg_raw)) if (filled_qty > 0 and avg_raw) else None
            )
        except (ValueError, ArithmeticError):
            avg_price = None
        status = str(data.get("status") or "").upper()
        return Fill(
            filled_qty=filled_qty,
            avg_price=avg_price,
            fully_filled=status == "FILLED",
            raw=data,
        )

    async def get_balance(self) -> Balance:
        """주식 잔고 조회(/holdings)."""
        data = await self._request("GET", "/api/v1/holdings", with_account=True)
        items = data.get("items") or []
        summary = {k: v for k, v in data.items() if k != "items"}
        return Balance(positions=items, summary=summary)


def _retry_after_seconds(resp: httpx.Response) -> float:
    """429 응답의 Retry-After(초) 를 상한 내에서 파싱. 없으면 1초."""
    raw = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Reset")
    try:
        wait = float(raw) if raw is not None else 1.0
    except (TypeError, ValueError):
        wait = 1.0
    return max(0.5, min(wait, _MAX_RETRY_AFTER))
