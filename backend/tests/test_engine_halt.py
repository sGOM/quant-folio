"""거래정지·시장 CB 판정(engine/halt.py, broker/base.is_halted_status) 단위 테스트.

I/O 가 없는 순수 로직이므로 DB·Redis·브로커 없이 검증한다. 시각은 호출자가
주입하는 단조시계 값이라 sleep 없이 시간 경과를 재현할 수 있다.
"""
from __future__ import annotations

from decimal import Decimal

from app.services.broker.base import Quote, is_halted_status
from engine.halt import MarketHaltMonitor, MarketState


class TestIsHaltedStatus:
    def test_임시정지_Y면_정지로_본다(self):
        assert is_halted_status("Y", "") is True

    def test_소문자_공백_섞여도_정지로_본다(self):
        assert is_halted_status(" y ", None) is True

    def test_임시정지_N이고_정상코드면_정지가_아니다(self):
        assert is_halted_status("N", "00") is False

    def test_거래정지_코드58은_정지로_본다(self):
        assert is_halted_status("N", "58") is True

    def test_관리종목_투자경고는_거래가_되므로_정지가_아니다(self):
        # 51 관리종목 · 53 투자경고는 매매 자체는 가능하다 — 정지로 오판하면
        # 정상 거래가 막힌다.
        assert is_halted_status("N", "51") is False
        assert is_halted_status("N", "53") is False

    def test_값이_없으면_정지가_아니다(self):
        assert is_halted_status(None, None) is False

    def test_Quote_기본값은_정지_아님(self):
        # 정지 여부를 제공하지 않는 브로커(토스 등)는 이 기본값으로 남는다.
        q = Quote(symbol="005930", price=Decimal("1"))
        assert q.halted is False
        assert q.status_code == ""


def _monitor(**kw) -> MarketHaltMonitor:
    base = dict(cb_ratio=0.6, min_sample=5, cooldown_sec=180.0, observe_ttl_sec=120.0)
    return MarketHaltMonitor(**{**base, **kw})


class TestMarketHaltMonitor:
    def test_관측이_없으면_정상이다(self):
        m = _monitor()
        assert m.evaluate(now=0.0) is MarketState.NORMAL
        assert m.halted_ratio(now=0.0) == (0.0, 0)

    def test_표본이_부족하면_전부_정지여도_판정을_보류한다(self):
        # 오탐 방어의 핵심 — 2~3 종목 전략에서 개별 VI 를 시장 CB 로 읽으면 안 된다.
        m = _monitor()
        for i in range(4):  # min_sample=5 미만
            m.observe(f"00000{i}", True, now=0.0)
        assert m.evaluate(now=0.0) is MarketState.NORMAL

    def test_표본이_차고_정지비율이_임계이상이면_시장CB로_판정한다(self):
        m = _monitor()
        for i in range(5):
            m.observe(f"00000{i}", True, now=0.0)
        assert m.evaluate(now=0.0) is MarketState.HALTED
        assert m.can_trade(now=0.0) is False

    def test_정지비율이_임계미만이면_정상이다(self):
        m = _monitor()
        for i in range(5):  # 5종목 중 2종목만 정지 = 40% < 60%
            m.observe(f"00000{i}", i < 2, now=0.0)
        ratio, n = m.halted_ratio(now=0.0)
        assert (round(ratio, 2), n) == (0.4, 5)
        assert m.evaluate(now=0.0) is MarketState.NORMAL

    def test_정지가_풀리면_곧바로_재개하지_않고_유예를_거친다(self):
        m = _monitor()
        for i in range(5):
            m.observe(f"00000{i}", True, now=0.0)
        assert m.evaluate(now=0.0) is MarketState.HALTED

        for i in range(5):  # 정지 해소
            m.observe(f"00000{i}", False, now=10.0)
        assert m.evaluate(now=10.0) is MarketState.COOLDOWN
        assert m.can_trade(now=10.0) is False

        # 유예 중에는 여전히 막힌다
        assert m.evaluate(now=10.0 + 179.0) is MarketState.COOLDOWN
        # 유예가 지나면 재개
        assert m.evaluate(now=10.0 + 180.0) is MarketState.NORMAL
        assert m.can_trade(now=10.0 + 180.0) is True

    def test_유예_중_정지가_재발하면_다시_중단한다(self):
        m = _monitor()
        for i in range(5):
            m.observe(f"00000{i}", True, now=0.0)
        m.evaluate(now=0.0)
        for i in range(5):
            m.observe(f"00000{i}", False, now=10.0)
        assert m.evaluate(now=10.0) is MarketState.COOLDOWN

        for i in range(5):  # 재발
            m.observe(f"00000{i}", True, now=20.0)
        assert m.evaluate(now=20.0) is MarketState.HALTED
        # 재발 후 다시 풀리면 유예는 그 시점부터 새로 센다
        for i in range(5):
            m.observe(f"00000{i}", False, now=30.0)
        assert m.evaluate(now=30.0) is MarketState.COOLDOWN
        assert m.evaluate(now=30.0 + 179.0) is MarketState.COOLDOWN
        assert m.evaluate(now=30.0 + 180.0) is MarketState.NORMAL

    def test_TTL_지난_관측은_표본에서_빠진다(self):
        # 오래된 관측이 남아 정지 상태를 실제보다 길게 유지하면 안 된다.
        m = _monitor()
        for i in range(5):
            m.observe(f"00000{i}", True, now=0.0)
        assert m.halted_ratio(now=0.0) == (1.0, 5)
        assert m.halted_ratio(now=121.0) == (0.0, 0)

    def test_TTL로_표본이_비면_유예_복귀는_계속_진행된다(self):
        # 표본 부족은 '판정 보류'지만, 이미 들어간 COOLDOWN 의 시간 경과 복귀까지
        # 막으면 관측이 끊긴 순간 영구히 주문이 잠긴다.
        m = _monitor()
        for i in range(5):
            m.observe(f"00000{i}", True, now=0.0)
        m.evaluate(now=0.0)
        for i in range(5):
            m.observe(f"00000{i}", False, now=1.0)
        assert m.evaluate(now=1.0) is MarketState.COOLDOWN
        # 이후 관측이 전혀 없어도(TTL 만료) 유예가 지나면 재개된다
        assert m.evaluate(now=1.0 + 180.0) is MarketState.NORMAL

    def test_같은_종목_재관측은_최신값으로_덮어쓴다(self):
        m = _monitor()
        for i in range(5):
            m.observe(f"00000{i}", True, now=0.0)
        m.observe("000000", False, now=1.0)
        ratio, n = m.halted_ratio(now=1.0)
        assert n == 5
        assert round(ratio, 2) == 0.8
