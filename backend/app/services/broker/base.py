"""브로커 추상화 — 증권사 API(KIS, 토스 등)의 공통 인터페이스.

엔진·라우트는 이 정규화된 인터페이스만 의존하고, 실제 증권사별 구현은
factory 가 사용자 설정(User.broker)에 따라 주입한다. 새 증권사를 붙일 때는
BrokerClient 를 구현하는 클라이언트만 추가하면 된다.

설계 메모:
- 반환값은 증권사별 원시 dict 대신 정규화 dataclass 로 통일한다.
  (raw 필드에 원본을 보존해 디버깅·증권사별 추가정보 접근은 가능)
- 가격·수량은 부동소수점 오차를 피하기 위해 Decimal 을 사용한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable


class BrokerError(Exception):
    """브로커 API 호출 실패(증권사 공통). 기존 KisError 를 대체하는 상위 개념."""


@dataclass
class Quote:
    """정규화된 현재가 시세.

    증권사에 따라 일부 필드(등락/고저/시가/거래량)는 제공되지 않을 수 있으며,
    그 경우 0 으로 채운다(price 는 항상 필수).
    """

    symbol: str
    price: Decimal
    change: Decimal = Decimal("0")
    change_rate: Decimal = Decimal("0")
    volume: int = 0
    high: Decimal = Decimal("0")
    low: Decimal = Decimal("0")
    open: Decimal = Decimal("0")
    # 통화 코드(KRW/USD 등). 해외주식은 소수 가격이 흔해 표시 포맷에 사용한다.
    currency: str = "KRW"
    # 거래정지·VI 등으로 지금 체결이 불가한 상태인지. 이 값을 제공하지 않는 브로커는
    # False 로 남는다 — 정지를 '모름'이 아니라 '아님'으로 두는 낙관적 기본값이므로,
    # 주문 차단을 이 필드에만 의존시키지 말 것(engine/halt.py 의 시장 CB 판정과 병행).
    halted: bool = False
    # 증권사 원본 종목상태 코드(KIS `iscd_stat_cls_code`: 58=거래정지 등). 감사·디버깅용.
    status_code: str = ""


# KIS `iscd_stat_cls_code` 중 체결 불가로 취급할 코드.
# 51 관리종목·52 투자위험·53 투자경고·54 투자주의는 거래가 되므로 제외한다.
# 주의: KIS 문서 기준값이며 실계좌 응답으로 교차 확인이 필요하다.
HALT_STATUS_CODES = frozenset({"58"})


def is_halted_status(temp_stop_yn: str | None, status_code: str | None) -> bool:
    """증권사 원본 필드로 체결 불가 여부를 판정한다.

    브로커 응답 → `Quote.halted` 정규화의 단일 출처. 엔진(`engine/halt.py`)이
    이 결과를 받아 시장 CB 를 판정하므로 판정 기준을 두 곳에 두지 않는다.

    :param temp_stop_yn: KIS 임시정지여부('Y'/'N'). VI 발동 중에도 Y 로 온다.
    :param status_code: KIS 종목상태구분코드(`iscd_stat_cls_code`).
    """
    if str(temp_stop_yn or "").strip().upper() == "Y":
        return True
    return str(status_code or "").strip() in HALT_STATUS_CODES


@dataclass
class OrderResult:
    """주문 접수 결과."""

    order_id: str | None
    # KIS 주문조직번호(KRX_FWDG_ORD_ORGNO). 체결통보 매칭 정확도를 높이기 위해 보존한다.
    # 증권사가 제공하지 않으면(토스 등) None.
    order_org_no: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Fill:
    """주문 체결 조회 결과.

    시장가라도 실제 체결가는 신호 시점가와 다르므로, 체결 기록·평균단가
    계산에는 반드시 이 값을 사용한다.
    """

    filled_qty: int
    avg_price: Decimal | None
    fully_filled: bool
    raw: dict | None = None


@dataclass
class Balance:
    """잔고 조회 결과(증권사 원형을 느슨하게 보존)."""

    positions: list[dict] = field(default_factory=list)
    summary: dict | list = field(default_factory=dict)

    def positions_normalized(self) -> list[dict]:
        """증권사별 원시 positions 를 {symbol, qty, avg_price} 리스트로 정규화한다.

        KIS(pdno/hldg_qty/pchs_avg_pric)와 토스(symbol/qty/avg_price)의 키 차이를
        흡수한다. 잔고 조회를 신뢰 소스로 쓰는 곳(정합·포지션 조회)이 공유하는 단일 진입점.
        보유수량 0 이하는 제외한다.
        """
        from decimal import Decimal, InvalidOperation

        out: list[dict] = []
        for p in self.positions:
            code = p.get("pdno") or p.get("symbol")
            if not code:
                continue
            try:
                qty = int(float(p.get("hldg_qty") or p.get("qty") or 0))
            except (ValueError, TypeError):
                qty = 0
            if qty <= 0:
                continue
            avg_raw = p.get("pchs_avg_pric") or p.get("avg_price")
            try:
                avg = Decimal(str(avg_raw)) if avg_raw not in (None, "", "0") else None
            except (InvalidOperation, ValueError):
                avg = None
            out.append({"symbol": code, "qty": qty, "avg_price": avg})
        return out


# 정규화된 주문 구분(증권사별 코드 매핑은 각 클라이언트가 담당).
OrderSideStr = str   # "buy" | "sell"
OrderTypeStr = str   # "market" | "limit"


@runtime_checkable
class BrokerClient(Protocol):
    """증권사 클라이언트 공통 인터페이스.

    구현체는 사용자별 자격증명으로 생성되며, 토큰 캐싱·인증은 내부에서 처리한다.
    """

    async def verify_connection(self) -> bool:
        """자격증명 유효성 검증(보통 토큰 발급으로 확인)."""
        ...

    async def get_quote(self, symbol: str) -> Quote:
        """현재가 시세 조회."""
        ...

    async def place_order(
        self,
        symbol: str,
        side: OrderSideStr,
        qty: int,
        price: int = 0,
        order_type: OrderTypeStr = "market",
    ) -> OrderResult:
        """현금 주문. side='buy'|'sell', order_type='market'|'limit'."""
        ...

    async def get_order_execution(
        self, order_id: str, symbol: str | None = None
    ) -> Fill:
        """주문 체결 조회로 실제 체결수량·평균체결가를 얻는다."""
        ...

    async def get_balance(self) -> Balance:
        """주식 잔고 조회."""
        ...
