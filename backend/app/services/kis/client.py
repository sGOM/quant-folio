"""한국투자증권 KIS Developers API 클라이언트.

- 기본값은 모의투자(vts) 도메인. KIS_ENV=prod 일 때만 실전.
- 접근토큰은 발급당 24시간 유효하며 발급 횟수 제한(분당 1회)이 있으므로
  Redis 에 캐싱해 재사용한다.
- app_key/secret 은 호출자가 복호화해 전달하며, 로깅하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

import httpx

from app.core.config import settings
from app.core.redis import redis_client
from app.services.broker.base import Balance, BrokerError, Fill, OrderResult, Quote

logger = logging.getLogger(__name__)

_TOKEN_TTL_SAFETY = 60  # 만료 직전 안전 마진(초)
_TOKEN_LOCK_TTL = 10    # 발급 락 TTL(초)
_TOKEN_LOCK_WAIT = 8    # 락 대기 동안 캐시 재확인 횟수

# KIS REST 유량제한(EGW00201 "초당 거래건수 초과") 회피용 최소 호출 간격(초).
# 모의투자(vts)는 실전보다 한도가 엄격하다. 여러 종목을 연속 조회하는 리밸런싱
# (현재가 20건 버스트)에서 실측으로 EGW00201 이 재현돼, 계좌 단위로 호출을 직렬화한다.
_RATE_MIN_INTERVAL_VTS = 0.5    # 모의투자 ~2건/초
_RATE_MIN_INTERVAL_PROD = 0.12  # 실전 한도(~20건/초) 대비 보수적으로 ~8건/초
# 유량제한에 걸렸을 때(EGW00201) 재시도 횟수·백오프. 전역 throttle 로도 못 막는
# 교차 프로세스(web·engine·worker 동시 접근) 경합의 보완책.
_RATE_RETRY = 3
_RATE_BACKOFF = 0.6  # 초


class _RateLimiter:
    """프로세스 전역 계좌 단위 rate limiter — KIS 초당 호출 한도(EGW00201) 회피.

    같은 계좌(appkey)로 나가는 모든 KIS REST 호출에 최소 간격을 강제한다. 각 호출자에게
    min_interval 간격의 '슬롯'을 순차 배정하고 락은 즉시 풀어, 슬롯까지만 대기시킨다.
    프로세스 내 버스트(리밸런싱 연속 시세 조회 등)를 막는 게 목적이며, 여러 프로세스가
    같은 계좌를 동시에 쓰는 교차 프로세스 경합까지는 막지 않는다(재시도가 보완).
    """

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_at)
            self._next_at = slot + self._min_interval
            wait = slot - now
        if wait > 0:
            await asyncio.sleep(wait)


# 계좌(KIS_ENV+appkey)별 limiter. 프로세스 전역 공유(모든 KisClient 인스턴스가 재사용).
_rate_limiters: dict[str, _RateLimiter] = {}


def _get_rate_limiter(app_key: str) -> _RateLimiter:
    key = f"{settings.KIS_ENV}:{app_key}"
    limiter = _rate_limiters.get(key)
    if limiter is None:
        interval = (
            _RATE_MIN_INTERVAL_VTS
            if settings.is_paper_trading
            else _RATE_MIN_INTERVAL_PROD
        )
        limiter = _RateLimiter(interval)
        _rate_limiters[key] = limiter
    return limiter


def _is_rate_limit_error(exc: Exception) -> bool:
    """KIS 유량제한(EGW00201) 오류인지."""
    return "EGW00201" in str(exc)


class KisError(BrokerError):
    """KIS API 호출 실패. BrokerError 하위 — `except BrokerError` 로도 잡힌다."""


class KisClient:
    """사용자별 KIS 자격증명으로 동작하는 클라이언트."""

    def __init__(self, app_key: str, app_secret: str, account_no: str | None = None):
        self._app_key = app_key
        self._app_secret = app_secret
        self._account_no = account_no
        self._base_url = settings.kis_base_url

    async def _throttle(self) -> None:
        """이 계좌의 전역 rate limiter 를 통과(초당 호출 한도 준수)."""
        await _get_rate_limiter(self._app_key).acquire()

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
        await self._throttle()
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

    async def _request_json(
        self, method: str, url: str, *, headers: dict, err_label: str,
        params: dict | None = None, json: dict | None = None,
    ) -> dict:
        """공통 KIS REST 호출 — HTTP/`rt_cd` 오류를 KisError 로 정규화하고,
        유량제한(EGW00201)이면 짧은 백오프로 몇 회 재시도한다.

        원래 시세 조회(get_current_price)에만 있던 재시도를 주문·체결조회·잔고조회에도
        적용한 공통 경로 — 전역 throttle 로도 못 막는 교차 프로세스(web·engine·worker
        동시 접근) 경합에서 이 엔드포인트들도 EGW00201 을 맞을 수 있는데, 재시도가
        없으면 주문·체결·잔고 확인이 실패해 시세 조회보다 파급이 크다.
        """
        last_exc: Exception | None = None
        for attempt in range(_RATE_RETRY):
            await self._throttle()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.request(method, url, headers=headers, params=params, json=json)
            if resp.status_code != 200:
                exc = KisError(f"{err_label} 실패: HTTP {resp.status_code} {resp.text[:200]}")
                if _is_rate_limit_error(exc) and attempt < _RATE_RETRY - 1:
                    last_exc = exc
                    await asyncio.sleep(_RATE_BACKOFF * (attempt + 1))
                    continue
                raise exc
            data = resp.json()
            if data.get("rt_cd") != "0":
                # EGW00201(유량제한)은 msg_cd 에 담기고 msg1 은 한글 문구뿐이라, 재시도
                # 판정을 위해 msg_cd 를 메시지에 포함해야 _is_rate_limit_error 가 잡는다.
                exc = KisError(
                    f"{err_label} 오류: [{data.get('msg_cd', '')}] {data.get('msg1', data)}"
                )
                if _is_rate_limit_error(exc) and attempt < _RATE_RETRY - 1:
                    last_exc = exc
                    await asyncio.sleep(_RATE_BACKOFF * (attempt + 1))
                    continue
                raise exc
            return data
        raise last_exc or KisError(f"{err_label} 실패")

    async def get_current_price(self, symbol: str) -> dict:
        """국내주식 현재가 시세 조회 (FHKST01010100)."""
        url = f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        headers = await self._auth_headers("FHKST01010100")
        data = await self._request_json(
            "GET", url, headers=headers, params=params, err_label="시세 조회",
        )
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
        await self._throttle()
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
        data = await self._request_json(
            "POST", url, headers=headers, json=body, err_label="주문",
        )
        out = data.get("output", {})
        return OrderResult(
            order_id=out.get("ODNO"),
            order_org_no=out.get("KRX_FWDG_ORD_ORGNO"),
            raw=data,
        )

    async def get_order_execution(self, kis_order_id: str, symbol: str | None = None) -> Fill:
        """당일 주문체결 조회로 실제 체결수량·평균체결가를 조회한다.

        시장가 주문이라도 실제 체결가는 신호 시점가와 다르므로,
        체결 기록·평균단가 계산에는 반드시 이 값을 사용해야 한다.
        반환: Fill(filled_qty, avg_price, fully_filled, raw).
        """
        from app.services.market import now_kst

        cano, acnt_prdt = self._account_parts()
        tr_id = "VTTC8001R" if settings.is_paper_trading else "TTTC8001R"
        # 컨테이너 시계는 UTC 라 datetime.now() 는 KST 자정~09:00 사이 '전날'이 된다.
        # 체결 조회일이 하루 어긋나면 그 시간대 체결이 0건으로 조회되므로 KST 로 고정한다.
        today = now_kst().strftime("%Y%m%d")
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
        data = await self._request_json(
            "GET", url, headers=headers, params=params, err_label="체결 조회",
        )

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
        data = await self._request_json(
            "GET", url, headers=headers, params=params, err_label="잔고 조회",
        )
        return Balance(positions=data.get("output1", []), summary=data.get("output2", []))
