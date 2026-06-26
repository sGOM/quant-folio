"""한국투자증권 KIS Developers API 클라이언트.

- 기본값은 모의투자(vts) 도메인. KIS_ENV=prod 일 때만 실전.
- 접근토큰은 발급당 24시간 유효하며 발급 횟수 제한(분당 1회)이 있으므로
  Redis 에 캐싱해 재사용한다.
- app_key/secret 은 호출자가 복호화해 전달하며, 로깅하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import httpx

from app.core.config import settings
from app.core.redis import redis_client
from app.services.broker.base import Balance, BrokerError, Fill, OrderResult, Quote

logger = logging.getLogger(__name__)

_TOKEN_TTL_SAFETY = 60  # 만료 직전 안전 마진(초)
_TOKEN_LOCK_TTL = 10    # 발급 락 TTL(초)
_TOKEN_LOCK_WAIT = 8    # 락 대기 동안 캐시 재확인 횟수


class KisError(BrokerError):
    """KIS API 호출 실패. BrokerError 하위 — `except BrokerError` 로도 잡힌다."""


class KisClient:
    """사용자별 KIS 자격증명으로 동작하는 클라이언트."""

    def __init__(self, app_key: str, app_secret: str, account_no: str | None = None):
        self._app_key = app_key
        self._app_secret = app_secret
        self._account_no = account_no
        self._base_url = settings.kis_base_url

    # 자격증명별로 분리된 토큰 캐시 키 (시크릿 자체는 저장하지 않음)
    def _token_cache_key(self) -> str:
        # app_key 는 식별자 용도로만 사용 (시크릿 아님)
        return f"kis:token:{settings.KIS_ENV}:{self._app_key}"

    async def _request_token(self) -> tuple[str, int]:
        """신규 접근토큰 발급. (token, expires_in) 반환."""
        url = f"{self._base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            raise KisError(f"토큰 발급 실패: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 86400))
        if not token:
            raise KisError(f"토큰 응답에 access_token 없음: {data}")
        return token, expires_in

    async def get_access_token(self) -> str:
        """캐시된 토큰을 반환하거나 신규 발급.

        KIS 토큰 발급은 분당 1회 제한이 있으므로, 여러 전략이 동시에 기동해도
        Redis 분산 락(single-flight)으로 단 한 번만 발급하고 나머지는 캐시를 재사용한다.
        """
        cache_key = self._token_cache_key()
        cached = await redis_client.get(cache_key)
        if cached:
            return cached

        lock_key = f"{cache_key}:lock"
        got_lock = await redis_client.set(lock_key, "1", nx=True, ex=_TOKEN_LOCK_TTL)
        if not got_lock:
            # 다른 호출자가 발급 중 — 캐시가 채워질 때까지 짧게 대기하며 재확인.
            for _ in range(_TOKEN_LOCK_WAIT):
                await asyncio.sleep(0.5)
                cached = await redis_client.get(cache_key)
                if cached:
                    return cached
            # 대기 후에도 없으면 락을 빼앗아 직접 발급 시도(교착 방지).
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return cached
            token, expires_in = await self._request_token()
            ttl = max(expires_in - _TOKEN_TTL_SAFETY, 60)
            await redis_client.set(cache_key, token, ex=ttl)
            logger.info("KIS 토큰 신규 발급 (env=%s, ttl=%ss)", settings.KIS_ENV, ttl)
            return token
        finally:
            if got_lock:
                await redis_client.delete(lock_key)

    async def _auth_headers(self, tr_id: str) -> dict[str, str]:
        token = await self.get_access_token()
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    async def get_current_price(self, symbol: str) -> dict:
        """국내주식 현재가 시세 조회 (FHKST01010100)."""
        url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = await self._auth_headers("FHKST01010100")
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            raise KisError(f"시세 조회 실패: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise KisError(f"시세 조회 오류: {data.get('msg1', data)}")
        return data["output"]

    @staticmethod
    def _dec(o: dict, key: str) -> Decimal:
        try:
            return Decimal(str(o.get(key) or 0))
        except (ValueError, ArithmeticError):
            return Decimal("0")

    async def get_quote(self, symbol: str) -> Quote:
        """정규화된 현재가 시세(BrokerClient 인터페이스)."""
        o = await self.get_current_price(symbol)
        try:
            volume = int(float(o.get("acml_vol") or 0))
        except (TypeError, ValueError):
            volume = 0
        return Quote(
            symbol=symbol,
            price=self._dec(o, "stck_prpr"),
            change=self._dec(o, "prdy_vrss"),
            change_rate=self._dec(o, "prdy_ctrt"),
            volume=volume,
            high=self._dec(o, "stck_hgpr"),
            low=self._dec(o, "stck_lwpr"),
            open=self._dec(o, "stck_oprc"),
            currency="KRW",  # KIS 는 국내주식 전용
        )

    async def verify_connection(self) -> bool:
        """토큰 발급으로 자격증명 유효성 검증."""
        await self.get_access_token()
        return True

    # ───────────────────── 계좌번호 파싱 ─────────────────────
    def _account_parts(self) -> tuple[str, str]:
        """'50012345-01' → (CANO='50012345', ACNT_PRDT_CD='01')."""
        if not self._account_no:
            raise KisError("계좌번호가 등록되지 않았습니다.")
        raw = self._account_no.replace("-", "").strip()
        if len(raw) < 10:
            raise KisError(f"계좌번호 형식 오류: {self._account_no}")
        return raw[:8], raw[8:10]

    async def _hashkey(self, body: dict) -> str:
        """주문 body 의 hashkey 발급 (변조 방지)."""
        url = f"{self._base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise KisError(f"hashkey 발급 실패: HTTP {resp.status_code}")
        return resp.json()["HASH"]

    # ───────────────────── 주문 ─────────────────────
    # 정규화 주문구분 → KIS ORD_DVSN 코드.
    _ORD_DVSN = {"limit": "00", "market": "01"}

    async def place_order(
        self, symbol: str, side: str, qty: int, price: int = 0, order_type: str = "market"
    ) -> OrderResult:
        """현금 주문. side='buy'|'sell', order_type='market'|'limit'.

        시장가면 price 는 0. 모의/실전은 tr_id 로 분기.
        반환: OrderResult(order_id=KRX 주문번호, raw=원본).
        """
        ord_dvsn = self._ORD_DVSN.get(order_type, order_type)  # 하위호환: 코드 직접 전달 허용
        cano, acnt_prdt = self._account_parts()
        is_buy = side == "buy"
        if settings.is_paper_trading:
            tr_id = "VTTC0802U" if is_buy else "VTTC0801U"
        else:
            tr_id = "TTTC0802U" if is_buy else "TTTC0801U"

        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": str(int(price)),
        }
        hashkey = await self._hashkey(body)
        headers = await self._auth_headers(tr_id)
        headers["hashkey"] = hashkey

        url = f"{self._base_url}/uapi/domestic-stock/v1/trading/order-cash"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise KisError(f"주문 실패: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise KisError(f"주문 거부: {data.get('msg1', data)}")
        out = data.get("output", {})
        return OrderResult(order_id=out.get("ODNO"), raw=data)

    async def get_order_execution(self, kis_order_id: str, symbol: str | None = None) -> Fill:
        """당일 주문체결 조회로 실제 체결수량·평균체결가를 조회한다.

        시장가 주문이라도 실제 체결가는 신호 시점가와 다르므로,
        체결 기록·평균단가 계산에는 반드시 이 값을 사용해야 한다.
        반환: Fill(filled_qty, avg_price, fully_filled, raw).
        """
        from datetime import datetime

        cano, acnt_prdt = self._account_parts()
        tr_id = "VTTC8001R" if settings.is_paper_trading else "TTTC8001R"
        today = datetime.now().strftime("%Y%m%d")
        headers = await self._auth_headers(tr_id)
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",  # 전체
            "INQR_DVSN": "00",
            "PDNO": symbol or "",
            "CCLD_DVSN": "00",
            "ORD_GNO_BRNO": "",
            "ODNO": kis_order_id,
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        url = f"{self._base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            raise KisError(f"체결 조회 실패: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise KisError(f"체결 조회 오류: {data.get('msg1', data)}")

        rows = [r for r in data.get("output1", []) if r.get("odno") == kis_order_id]
        if not rows:
            return Fill(filled_qty=0, avg_price=None, fully_filled=False, raw=data)
        row = rows[0]
        filled_qty = int(row.get("tot_ccld_qty") or 0)
        ord_qty = int(row.get("ord_qty") or 0)
        avg_raw = row.get("avg_prvs") or row.get("ccld_prvs") or "0"
        try:
            avg_price = Decimal(str(avg_raw)) if filled_qty > 0 else None
        except (ValueError, ArithmeticError):
            avg_price = None
        return Fill(
            filled_qty=filled_qty,
            avg_price=avg_price,
            fully_filled=ord_qty > 0 and filled_qty >= ord_qty,
            raw=row,
        )

    # ───────────────────── 잔고 ─────────────────────
    async def get_balance(self) -> Balance:
        """주식 잔고 조회. Balance(positions, summary) 반환."""
        cano, acnt_prdt = self._account_parts()
        tr_id = "VTTC8434R" if settings.is_paper_trading else "TTTC8434R"
        headers = await self._auth_headers(tr_id)
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        url = f"{self._base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            raise KisError(f"잔고 조회 실패: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise KisError(f"잔고 조회 오류: {data.get('msg1', data)}")
        return Balance(positions=data.get("output1", []), summary=data.get("output2", []))
