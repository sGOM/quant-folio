"""브로커 추상화 검증 — 팩토리, env 폴백, 토스 파싱/매핑, KIS 주문구분 매핑."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.security import encrypt_secret
from app.services.broker import (
    BrokerError,
    Fill,
    OrderResult,
    Quote,
    make_broker,
    resolve_credentials,
    user_has_credentials,
)
from app.services.broker import factory
from app.services.broker.toss import TossClient
from app.services.kis import KisClient


def _user(broker="kis", key=None, secret=None, acct=None):
    """테스트용 최소 User 더블(필요 속성만)."""
    return SimpleNamespace(
        broker=broker, kis_app_key=key, kis_app_secret=secret, kis_account_no=acct
    )


# ───────────────────── 팩토리 ─────────────────────
def test_factory_returns_kis():
    c = make_broker("kis", "key", "secret", "50012345-01")
    assert isinstance(c, KisClient)


def test_factory_returns_toss():
    c = make_broker("toss", "cid", "csecret", "12345")
    assert isinstance(c, TossClient)


def test_factory_rejects_unknown():
    with pytest.raises(BrokerError):
        make_broker("daishin", "k", "s", "1")


# ───────────────────── 자격증명 해석(DB 우선 → env 폴백) ─────────────────────
def test_resolve_prefers_db_creds():
    u = _user("kis", encrypt_secret("DBKEY"), encrypt_secret("DBSEC"), "50012345-01")
    assert resolve_credentials(u) == ("kis", "DBKEY", "DBSEC", "50012345-01")


def test_resolve_env_fallback_kis(monkeypatch):
    monkeypatch.setattr(factory.settings, "KIS_APP_KEY", "ENVKEY")
    monkeypatch.setattr(factory.settings, "KIS_APP_SECRET", "ENVSEC")
    monkeypatch.setattr(factory.settings, "KIS_ACCOUNT_NO", "11112222-01")
    u = _user("kis")  # DB 자격증명 없음
    assert resolve_credentials(u) == ("kis", "ENVKEY", "ENVSEC", "11112222-01")
    assert user_has_credentials(u) is True


def test_resolve_env_fallback_toss(monkeypatch):
    monkeypatch.setattr(factory.settings, "TOSS_APP_KEY", "TID")
    monkeypatch.setattr(factory.settings, "TOSS_APP_SECRET", "TSEC")
    monkeypatch.setattr(factory.settings, "TOSS_ACCOUNT_NO", "999")
    u = _user("toss")
    assert resolve_credentials(u) == ("toss", "TID", "TSEC", "999")


def test_resolve_none_when_no_creds(monkeypatch):
    monkeypatch.setattr(factory.settings, "KIS_APP_KEY", "")
    monkeypatch.setattr(factory.settings, "KIS_APP_SECRET", "")
    u = _user("kis")
    assert resolve_credentials(u) is None
    assert user_has_credentials(u) is False


def test_make_broker_for_user_env_fallback(monkeypatch):
    monkeypatch.setattr(factory.settings, "TOSS_APP_KEY", "TID")
    monkeypatch.setattr(factory.settings, "TOSS_APP_SECRET", "TSEC")
    monkeypatch.setattr(factory.settings, "TOSS_ACCOUNT_NO", "999")
    client = factory.make_broker_for_user(_user("toss"))
    assert isinstance(client, TossClient)


# ───────────────────── KIS 주문구분 매핑 ─────────────────────
def test_kis_order_dvsn_mapping():
    assert KisClient._ORD_DVSN["market"] == "01"
    assert KisClient._ORD_DVSN["limit"] == "00"


# ───────────────────── 토스 파싱/매핑 ─────────────────────
class _Recorder:
    """TossClient._request 를 대체해 호출 인자를 기록하고 캔드 응답을 반환."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __call__(self, method, path, *, with_account=False, params=None, json=None):
        self.calls.append(
            {"method": method, "path": path, "with_account": with_account,
             "params": params, "json": json}
        )
        return self.response


@pytest.fixture
def toss():
    return TossClient("cid", "csecret", "12345")


async def test_toss_get_quote(toss, monkeypatch):
    rec = _Recorder([{"symbol": "005930", "lastPrice": "81500", "currency": "KRW"}])
    monkeypatch.setattr(toss, "_request", rec)

    q = await toss.get_quote("005930")
    assert isinstance(q, Quote)
    assert q.price == Decimal("81500")
    assert rec.calls[0]["params"] == {"symbols": "005930"}


async def test_toss_get_quote_bad_payload(toss, monkeypatch):
    monkeypatch.setattr(toss, "_request", _Recorder([]))
    with pytest.raises(BrokerError):
        await toss.get_quote("005930")


async def test_toss_place_order_market(toss, monkeypatch):
    rec = _Recorder({"orderId": "T-123"})
    monkeypatch.setattr(toss, "_request", rec)

    res = await toss.place_order("005930", "buy", 10, price=0, order_type="market")
    assert isinstance(res, OrderResult)
    assert res.order_id == "T-123"
    body = rec.calls[0]["json"]
    assert body["side"] == "BUY"
    assert body["orderType"] == "MARKET"
    assert body["quantity"] == 10
    assert "price" not in body  # 시장가엔 가격 미포함
    assert rec.calls[0]["with_account"] is True


async def test_toss_place_order_limit_includes_price(toss, monkeypatch):
    rec = _Recorder({"orderId": "T-9"})
    monkeypatch.setattr(toss, "_request", rec)

    await toss.place_order("AAPL", "sell", 3, price=200, order_type="limit")
    body = rec.calls[0]["json"]
    assert body["side"] == "SELL"
    assert body["orderType"] == "LIMIT"
    assert body["price"] == 200


async def test_toss_place_order_rejects_unknown_side(toss, monkeypatch):
    monkeypatch.setattr(toss, "_request", _Recorder({"orderId": "x"}))
    with pytest.raises(BrokerError):
        await toss.place_order("005930", "hold", 1)


async def test_toss_get_order_execution(toss, monkeypatch):
    rec = _Recorder({
        "status": "FILLED",
        "execution": {"filledQuantity": "10", "averageFilledPrice": "81450"},
    })
    monkeypatch.setattr(toss, "_request", rec)

    fill = await toss.get_order_execution("T-123", "005930")
    assert isinstance(fill, Fill)
    assert fill.filled_qty == 10
    assert fill.avg_price == Decimal("81450")
    assert fill.fully_filled is True


async def test_toss_get_order_execution_partial(toss, monkeypatch):
    rec = _Recorder({
        "status": "PARTIAL_FILLED",
        "execution": {"filledQuantity": "4", "averageFilledPrice": "81000"},
    })
    monkeypatch.setattr(toss, "_request", rec)

    fill = await toss.get_order_execution("T-1")
    assert fill.filled_qty == 4
    assert fill.fully_filled is False


async def test_toss_account_header_required():
    """계좌(accountSeq) 미등록이면 계좌 필요한 호출에서 에러."""
    c = TossClient("cid", "csecret", account_no=None)
    with pytest.raises(BrokerError):
        await c._headers(with_account=True)
