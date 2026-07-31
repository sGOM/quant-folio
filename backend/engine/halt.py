"""거래정지·시장 서킷브레이커(CB) 감지 — 주문 차단 게이트의 판정 로직.

2026-07 KRX 폭락 구간에서 시장 CB 가 9회 발동해 매번 20~30분 체결이 불가했고,
재개 직후 호가는 붕괴 상태였다. 그럼에도 엔진에는 정지 상태 개념이 없어 러너가
그대로 주문을 냈다. 이 모듈은 그 공백을 메운다.

## 두 층
- **종목 정지**: KIS `inquire-price` 의 `temp_stop_yn`(임시정지, VI 포함)·
  `iscd_stat_cls_code`(58=거래정지)를 `Quote.halted` 로 정규화해 받아, 해당 종목
  주문만 건너뛴다. 판정 자체는 브로커 응답이므로 이 모듈이 관여하지 않는다.
- **시장 CB**: 시장 전체 CB 는 개별 종목 필드로 직접 오지 않는다. CB 중에는 전
  종목이 동시에 멈추므로, **동시 정지 비율**로 간접 판정한다. 이것이 이 모듈의 본체다.

## 왜 상태기계인가 — 재개 직후가 더 위험하다
CB 해제 즉시 주문을 재개하면 호가가 붕괴된 구간에 시장가가 꽂힌다. 그래서 정지가
풀려도 곧바로 NORMAL 로 돌아가지 않고 COOLDOWN 을 거친다.

    NORMAL ──정지비율≥cb_ratio──▶ HALTED ──정지 해소──▶ COOLDOWN ──유예 경과──▶ NORMAL
                                     ▲                      │
                                     └──── 정지 재발 ────────┘

## 오탐 방어 (설계상 가장 중요한 부분)
정상장에서 CB 로 오판하면 매매가 통째로 멈춘다. 따라서 판정은 보수적으로 건다.
- `min_sample` 미만의 관측으로는 **절대 판정하지 않는다**. 2~3 종목 전략에서
  한 종목이 개별 VI 로 멈춘 것을 시장 CB 로 읽는 사고를 막는 장치다.
- 관측은 `observe_ttl_sec` 안에 들어온 것만 유효하다. 오래된 관측이 남아
  정지 상태를 실제보다 길게 유지하지 않도록 한다.

시각은 호출자가 단조시계(`time.monotonic()`)로 주입한다 — 테스트 가능성과
시스템 시계 변경에 대한 내성을 위해서다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)

# 기본 파라미터 — 보수적으로 잡았다. 근거는 위 '오탐 방어' 참고.
DEFAULT_CB_RATIO = 0.6
DEFAULT_MIN_SAMPLE = 5
DEFAULT_COOLDOWN_SEC = 180.0
DEFAULT_OBSERVE_TTL_SEC = 120.0


class MarketState(StrEnum):
    """시장 매매 가능 상태."""

    NORMAL = "normal"
    HALTED = "halted"      # 시장 CB 판정 — 주문 전면 중단
    COOLDOWN = "cooldown"  # 정지는 풀렸으나 호가 안정 대기 — 주문 중단 유지


@dataclass
class MarketHaltMonitor:
    """동시 정지 비율로 시장 CB 를 판정하는 상태기계.

    I/O 를 하지 않는 순수 로직이다(`docs/CONVENTIONS.md` — 검증 로직은 순수 함수로).
    """

    cb_ratio: float = DEFAULT_CB_RATIO
    min_sample: int = DEFAULT_MIN_SAMPLE
    cooldown_sec: float = DEFAULT_COOLDOWN_SEC
    observe_ttl_sec: float = DEFAULT_OBSERVE_TTL_SEC

    # symbol -> (관측시각, 정지여부)
    _obs: dict[str, tuple[float, bool]] = field(default_factory=dict, repr=False)
    _state: MarketState = MarketState.NORMAL
    # COOLDOWN 진입 시각(NORMAL 복귀 판정 기준). 그 외 상태에서는 None.
    _cooldown_from: float | None = None

    @property
    def state(self) -> MarketState:
        """마지막으로 평가된 상태(평가를 유발하지 않는다)."""
        return self._state

    def observe(self, symbol: str, halted: bool, now: float) -> None:
        """종목의 정지 여부 관측을 기록한다. 판정은 `evaluate` 에서 한다."""
        self._obs[symbol] = (now, halted)

    def halted_ratio(self, now: float) -> tuple[float, int]:
        """유효 관측 기준 (정지비율, 표본수). 표본이 없으면 (0.0, 0)."""
        fresh = [
            h for (ts, h) in self._obs.values()
            if now - ts <= self.observe_ttl_sec
        ]
        if not fresh:
            return 0.0, 0
        return sum(fresh) / len(fresh), len(fresh)

    def evaluate(self, now: float) -> MarketState:
        """현재 관측으로 상태를 갱신해 반환한다.

        표본이 `min_sample` 미만이면 **판정을 보류**하고 기존 상태를 유지한다
        (정보 부족을 정지 근거로 삼지 않는다). 다만 이미 COOLDOWN 중이면
        시간 경과에 따른 NORMAL 복귀는 표본과 무관하게 진행한다.
        """
        ratio, n = self.halted_ratio(now)
        cb_now = n >= self.min_sample and ratio >= self.cb_ratio

        if cb_now:
            if self._state is not MarketState.HALTED:
                logger.warning(
                    "시장 CB 판정 — 정지비율 %.0f%% (표본 %d종목) → 주문 중단",
                    ratio * 100, n,
                )
            self._state = MarketState.HALTED
            self._cooldown_from = None
            return self._state

        if self._state is MarketState.HALTED:
            # 정지가 풀렸다 — 곧바로 재개하지 않고 유예에 들어간다.
            logger.warning(
                "시장 CB 해소 — %.0f초 유예 후 주문 재개(재개 직후 호가 붕괴 회피)",
                self.cooldown_sec,
            )
            self._state = MarketState.COOLDOWN
            self._cooldown_from = now
            return self._state

        if self._state is MarketState.COOLDOWN:
            started = self._cooldown_from
            if started is None or now - started >= self.cooldown_sec:
                logger.info("유예 종료 — 주문 재개")
                self._state = MarketState.NORMAL
                self._cooldown_from = None
            return self._state

        return self._state

    def can_trade(self, now: float) -> bool:
        """지금 주문을 내도 되는지. `evaluate` 를 수행한 뒤 판단한다."""
        return self.evaluate(now) is MarketState.NORMAL


# 엔진 프로세스 전역 모니터.
#
# 러너마다 따로 두면 2~3 종목 전략은 `min_sample` 을 영영 못 채워 시장 CB 를 판정할
# 수 없다. 시장 상태는 전략별 속성이 아니라 프로세스 공통 관측이므로, 모든 러너의
# 관측을 한 곳에 모아 표본을 키운다.
_MONITOR = MarketHaltMonitor()


def get_monitor() -> MarketHaltMonitor:
    """엔진 프로세스 전역 시장 정지 모니터."""
    return _MONITOR
