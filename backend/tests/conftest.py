"""테스트 공통 설정 — 앱 모듈 import 전에 필수 환경변수를 주입하고, 여러 테스트가
공유하는 대역(Fake) 구현을 제공한다.

여기 모인 대역들(FakeRedis·FakeBroker·FakeDB)은 실제 I/O(레디스·증권사 API·DB)를
타지 않고 엔진 실행 경로를 종단으로 구동하기 위한 것이다. 개별 테스트가 각자
복사해 두던 대역을 한곳으로 모아 중복을 없애고 회귀 안전망을 공유한다.
"""
import base64
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef")
os.environ.setdefault(
    "CREDENTIAL_ENC_KEY", base64.urlsafe_b64encode(b"0" * 32).decode()
)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://quant:quant@localhost:5432/quant")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("KIS_ENV", "vts")

# ── KRX 자격증명은 테스트에서 반드시 비운다 ──────────────────────────────────
# pykrx 는 `pykrx.website.comm` 을 **임포트하는 시점에** KRX_ID/KRX_PW 로 실제 로그인을
# 시도하는 모듈 전역 부작용이 있다. 개발 컨테이너에는 secrets/krx_id.txt 가
# KRX_ID_FILE 로 마운트돼 있어(docker-compose.yml), 여기서 비우지 않으면 테스트가 매
# 실행마다 진짜 KRX 에 로그인하고 **계정 ID 를 테스트 출력에 남긴다**(과거 실제 유출).
# CI 는 자격증명을 주입하지 않으므로, 비워야 로컬 스위트가 CI 와 같은 조건에서 검증된다
# — 자격증명 유무로 통과/실패가 갈리는 테스트를 로컬에서 놓치지 않기 위함.
# KRX 인증이 성립한 상태를 검증해야 하는 테스트는 `krx_index.has_krx_auth`(또는
# `_session`)를 개별적으로 목 처리한다.
for _krx_field in ("KRX_ID", "KRX_PW"):
    os.environ.pop(f"{_krx_field}_FILE", None)
    os.environ[_krx_field] = ""

# ── OpenDART API 키도 같은 이유로 비운다 ────────────────────────────────────
# 개발 컨테이너에는 secrets/opendart_api_key.txt 가 OPENDART_API_KEY_FILE 로 마운트돼
# 있어(docker-compose.yml), 비우지 않으면 `opendart.is_enabled()` 가 True 가 되어
# 일부 테스트가 **진짜 OpenDART 를 호출한다**. 실제로 test_caller_degradation 의
# 중립화 테스트들이 삼성전자 2020~2023 연간 원계정을 매 실행마다 망으로 받아오고
# 있었다(일 20,000건 한도 소모 + 비결정적 테스트). 로컬 스토어 배선(dart_financials)
# 이후로는 그 결과가 실 DB 에까지 적재돼 드러났다. CI 는 키를 주입하지 않으므로,
# 비워야 로컬 스위트가 CI 와 같은 조건에서 검증된다.
# 활성 상태를 검증해야 하는 테스트는 `opendart.is_enabled` 를 개별적으로 목 처리한다
# (test_opendart.py 가 이미 그렇게 한다).
os.environ.pop("OPENDART_API_KEY_FILE", None)
os.environ["OPENDART_API_KEY"] = ""

# ── 앱 모듈 import 는 반드시 위 환경변수 주입 이후에 한다 ──────────────────────
import json  # noqa: E402
import operator as _pyop  # noqa: E402
from decimal import Decimal  # noqa: E402

from sqlalchemy.sql.operators import and_ as _and_op  # noqa: E402
from sqlalchemy.sql.operators import in_op as _in_op  # noqa: E402

from app.services.broker.base import (  # noqa: E402
    Balance,
    BrokerError,
    Fill,
    OrderResult,
    Quote,
)


# ─────────────────────────── FakeRedis ───────────────────────────
class FakeRedis:
    """set/get/delete/publish 만 지원하는 인메모리 Redis 대역(발행 기록 보관).

    분산 락(SET NX)·마지막 실행일·레짐 상태·이벤트 발행을 실제 Redis 없이 재현한다.
    엔진 헬스·알림 dedup 검증(test_engine_health)과 종단 통합 테스트(test_engine_e2e)가
    공유한다.
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.store:
            return None
        self.store[k] = v
        return True

    async def delete(self, k):
        self.store.pop(k, None)

    async def incr(self, k):
        self.store[k] = str(int(self.store.get(k, "0")) + 1)
        return int(self.store[k])

    async def expire(self, k, seconds):
        return True  # TTL 자체는 이 대역의 관심사가 아님(고정 윈도우 카운트 로직만 검증)

    async def publish(self, channel, data):
        self.published.append((channel, data))
        return 1

    def alerts(self) -> list[dict]:
        # publish_event 는 사용자별 채널 + 공용 채널 양쪽에 발행하므로, 중복 카운트를
        # 피하려 공용 채널("engine:events")만 집계한다.
        from app.core.channels import ENGINE_EVENTS_CHANNEL

        out = []
        for ch, data in self.published:
            if ch != ENGINE_EVENTS_CHANNEL:
                continue
            try:
                payload = json.loads(data)
            except (ValueError, TypeError):
                continue
            if payload.get("type") == "alert":
                out.append(payload)
        return out


# ─────────────────────────── FakeBroker ───────────────────────────
class FakeBroker:
    """BrokerClient 프로토콜(app.services.broker.base)을 구현하는 증권사 대역.

    KisClient/TossClient 와 동일한 시그니처(verify_connection·get_quote·place_order·
    get_order_execution·get_balance)를 제공해 엔진이 실제 증권사 없이 종단으로 구동되게 한다.

    :param quotes: {종목코드: 가격} 고정 시세. get_quote 가 이 값을 반환한다.
    :param fill_ratio: 체결 비율(1.0=전량 즉시 체결, 0.5=절반만 체결). 부분체결을 흉내낸다.
    :param fill_slippage: 체결가에 곱할 계수(1.0=시세 그대로, 1.001=+0.1% 불리 체결).
    :param balance_positions: get_balance 가 돌려줄 잔고 포지션(원시 dict 리스트).
    """

    def __init__(
        self,
        quotes: dict[str, Decimal | int | float] | None = None,
        *,
        fill_ratio: float = 1.0,
        fill_slippage: float = 1.0,
        balance_positions: list[dict] | None = None,
    ):
        self._quotes = {k: Decimal(str(v)) for k, v in (quotes or {}).items()}
        self._fill_ratio = fill_ratio
        self._fill_slippage = Decimal(str(fill_slippage))
        self._balance_positions = balance_positions or []
        # 접수한 주문 기록(order_id → 체결 산출용 메타). 감사·검증에 쓴다.
        self.orders: dict[str, dict] = {}
        self._seq = 0

    async def verify_connection(self) -> bool:
        return True

    async def get_quote(self, symbol: str) -> Quote:
        if symbol not in self._quotes:
            raise BrokerError(f"시세 없음: {symbol}")
        return Quote(symbol=symbol, price=self._quotes[symbol])

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: int = 0,
        order_type: str = "market",
    ) -> OrderResult:
        self._seq += 1
        order_id = f"FAKE{self._seq:06d}"
        # 시장가(price=0)면 주입 시세로 체결가를 잡고, 지정가면 그 가격을 쓴다.
        base = self._quotes.get(symbol, Decimal(str(price or 0)))
        fill_price = (base * self._fill_slippage).quantize(Decimal("0.0001"))
        filled = int(qty * self._fill_ratio)
        self.orders[order_id] = {
            "symbol": symbol,
            "side": side,
            "qty": int(qty),
            "filled_qty": filled,
            "avg_price": fill_price,
        }
        return OrderResult(order_id=order_id, raw={"symbol": symbol})

    async def get_order_execution(
        self, order_id: str, symbol: str | None = None
    ) -> Fill:
        meta = self.orders.get(order_id)
        if meta is None:
            raise BrokerError(f"주문 없음: {order_id}")
        filled = meta["filled_qty"]
        return Fill(
            filled_qty=filled,
            avg_price=meta["avg_price"] if filled > 0 else None,
            fully_filled=filled >= meta["qty"],
        )

    async def get_balance(self) -> Balance:
        return Balance(positions=list(self._balance_positions), summary={})


# ─────────────────────────── FakeDB (인메모리 세션) ───────────────────────────
#
# executor/fills/risk/base_runner 가 쓰는 AsyncSession 연산(add·commit·refresh·
# scalar·scalars·execute)을 인메모리 저장소로 대역한다. select 문의 대상 엔티티와
# WHERE 등가/부등/IN 조건을 해석해 필터링하므로, 실제 DB 없이도 Order/Execution/
# Position 레코드가 경로 전반에서 정확히 생성·조회되는지를 종단으로 검증할 수 있다.
#
# 한계(의도된 것): OR·JOIN 등 복합 WHERE 는 best-effort 로 무시(무제약 취급)한다.
# 리스크 한도(RiskLimit) 미설정 시 risk 모듈이 이 복합 쿼리에 도달하기 전에
# 조기 반환하므로, 종단 테스트 경로에서 실제로 실행되는 단순 쿼리만 정확히 지원한다.


class _Store:
    """FakeDB 인스턴스들이 공유하는 레코드 저장소(엔티티 클래스 → 행 리스트)."""

    def __init__(self):
        self.rows: dict[type, list] = {}
        self._id = 0

    def next_id(self) -> int:
        self._id += 1
        return self._id

    def all(self, cls) -> list:
        return list(self.rows.get(cls, []))


class _ScalarResult:
    """db.scalars(...) 결과 대역 — 반복·all() 을 지원한다."""

    def __init__(self, rows: list):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def all(self) -> list:
        return list(self._rows)


class _ExecResult:
    """db.execute(...) 결과 대역 — 종단 경로에서는 비어 있는 결과만 필요하다."""

    def __init__(self, rows: list):
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._rows)


def _extract_predicates(clause) -> list[tuple[str, object, object]]:
    """WHERE 절에서 (컬럼명, 연산자, 값) 등가/부등/IN 조건을 뽑는다(AND 만, best-effort).

    OR·해석 불가 절은 무시한다(무제약). 종단 경로의 단순 쿼리를 정확히 재현하는 것이 목적.
    """
    if clause is None:
        return []
    clauses = getattr(clause, "clauses", None)
    if clauses is not None:
        # BooleanClauseList — AND 만 전개하고 OR 등은 무시(무제약).
        if getattr(clause, "operator", None) is not _and_op:
            return []
        out: list[tuple[str, object, object]] = []
        for c in clauses:
            out.extend(_extract_predicates(c))
        return out
    left = getattr(clause, "left", None)
    col = getattr(left, "name", None)
    if col is None:
        return []
    op = getattr(clause, "operator", None)
    val = getattr(getattr(clause, "right", None), "value", None)
    return [(col, op, val)]


def _row_matches(row, preds: list[tuple[str, object, object]]) -> bool:
    for col, op, val in preds:
        cur = getattr(row, col, None)
        if op is _in_op:
            if cur not in (val or []):
                return False
        elif op is _pyop.gt:
            if not (cur is not None and cur > val):
                return False
        elif op is _pyop.ge:
            if not (cur is not None and cur >= val):
                return False
        else:  # eq(기본) 및 미지원 연산자는 등가로 취급
            if cur != val:
                return False
    return True


class FakeDB:
    """AsyncSession 대역. `async with AsyncSessionLocal() as db:` 형태로 쓰인다.

    같은 _Store 를 공유하는 FakeDB 인스턴스들은 하나의 논리적 DB 를 본다(여러 세션이
    같은 데이터를 읽고 쓰는 실제 동작을 재현). commit/rollback 은 즉시 반영 모델이라 no-op.
    """

    def __init__(self, store: _Store):
        self._store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj) -> None:
        cls = type(obj)
        if getattr(obj, "id", None) is None:
            try:
                obj.id = self._store.next_id()
            except Exception:  # noqa: BLE001 - id 없는 엔티티(방어)
                pass
        self._store.rows.setdefault(cls, []).append(obj)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def delete(self, obj) -> None:
        rows = self._store.rows.get(type(obj))
        if rows is not None and obj in rows:
            rows.remove(obj)

    async def refresh(self, obj) -> None:
        return None

    async def flush(self) -> None:
        return None

    def _match(self, stmt) -> list:
        entity = stmt.column_descriptions[0]["entity"]
        rows = self._store.all(entity)
        preds = _extract_predicates(stmt.whereclause)
        return [r for r in rows if _row_matches(r, preds)]

    async def scalar(self, stmt):
        rows = self._match(stmt)
        return rows[0] if rows else None

    async def scalars(self, stmt) -> _ScalarResult:
        return _ScalarResult(self._match(stmt))

    async def execute(self, stmt) -> _ExecResult:
        # 종단 경로에서 execute 로 흐르는 것은 JOIN 이 있는 당일손익 쿼리뿐이며,
        # 리스크 한도 미설정 시 이 경로에 도달하지 않는다. 안전하게 빈 결과를 준다.
        return _ExecResult([])


def make_session_factory(store: _Store):
    """AsyncSessionLocal 대체용 팩토리. 호출 시 store 를 공유하는 FakeDB 를 돌려준다."""
    return lambda: FakeDB(store)
