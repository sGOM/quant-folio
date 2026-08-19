"""리밸런싱 실행기 — 단일 리밸런싱 전략을 구동.

흐름: 주기적으로(기본 60초) 발화 조건 확인 → due 시 universe 종가 시드 → 목표비중
산정 → 현재가·보유 조회 → 드리프트 밴드 초과분 주문(매도 우선 → 매수) → 마지막
실행일 기록. 주문 실행·멱등성·리스크는 기존 executor/risk 를 그대로 재사용한다.

상태(마지막 실행일)는 Redis 에 보관하고, 멱등성 키에 실행일을 넣어 같은 거래일
중복 발화 시에도 주문이 이중 생성되지 않게 한다(orders.idempotency_key UNIQUE).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.channels import mdd_state_key
from app.core.database import AsyncSessionLocal
from app.models import Order, OrderStatus, Position
from app.services.backtest.tracking import replay_cash_balance
from app.services.data.loader import (
    get_close_series,
    load_ohlcv,
    upsert_price_ticks,
)
from app.services.market import KST, is_business_day, is_market_open_async, now_kst
from app.services.metrics import (
    _approx_start,
    _fetch_index_ohlcv,
    _last_business_day,
    _ymd,
    compute_universe_scores,
)
from engine import risk
from engine.alerts import publish_alert
from engine.base_runner import BaseRunner, _position_lock
from engine.rebalance import (
    compute_rebalance_orders,
    compute_target_weights,
    is_rebalance_due,
)

# MDD 킬스위치·변동성 타겟팅 입력(§11)에서 실현손익을 반영한 체결로 인정하는 상태.
# tracking route(app.api.routes.tracking)와 동일 규약.
_FILLED_STATUSES = (OrderStatus.PARTIAL, OrderStatus.FILLED)

logger = logging.getLogger("engine.rebalance_runner")

_LAST_PREFIX = "rebalance:last:"
_REGIME_PREFIX = "rebalance:regime:"  # 직전 레짐 상태(risk-off 여부) 보관
_LAST_TTL = 60 * 60 * 24 * 90  # 90일(휴장 등 대비 여유)
# 시세 조회 실패 종목의 시가평가 대체값(마지막 로컬 종가)을 찾는 조회 창(일). 연휴·거래정지를
# 넘길 정도로 넉넉하되, 이보다 오래된 종가는 '마지막으로 알려진 시세'로 보기 어렵다.
_CLOSE_FALLBACK_DAYS = 30

# pykrx 종합지수 티커(레짐 필터 기준지수). 백테스트 라우트와 동일 규약.
_REGIME_INDEX_TICKER = {"KOSPI": "1001", "KOSDAQ": "2001"}


class RebalanceRunner(BaseRunner):
    """단일 리밸런싱 전략을 주기적으로 점검·실행하는 실행기.

    :param strategy_id: 구동할 전략 ID
    :param redis: 마지막 실행일 보관·분산 락·이벤트 발행용 Redis 클라이언트
    """

    _logger = logger
    _label = "리밸런싱 전략"
    _tick_word = "점검"
    _poll_interval = 60  # 초 — 발화 시점 점검 주기
    # compute_universe_scores 가 종목별로 pykrx/OpenDART 를 순차 조회하는데, 그중 일부
    # (pykrx)는 자체 타임아웃이 없어 KRX 응답 지연 시 무한 대기할 수 있다. 틱을 타임아웃으로
    # 감싸 특정 틱이 걸려도 다음 주기에 러너가 계속 진행되게 한다(단, to_thread 로 넘어간
    # 하위 스레드 자체는 취소되지 않고 백그라운드에서 계속 대기한다 — 이벤트 루프를 다시
    # 살리는 것이 목적이며 스레드 강제 종료는 아니다).
    _tick_timeout = 300  # 초

    def __init__(self, strategy_id, redis):
        super().__init__(strategy_id, redis)
        # PIT 유니버스 폴백 알림 dedup 플래그. _resolve_universe 는 한 틱에 여러 번
        # 호출될 수 있으므로, 폴백 진입 시 1회만 알림하고 PIT 조회가 다시 성공하면
        # 해제해 재발송 가능하게 한다(연속 실패 알림과 동일한 '전환 시점' dedup 규약).
        self._pit_fallback_active: bool = False
        # 패닉 오버레이 미지원 알림 dedup 플래그. config 는 러너 기동 시 1회만 적재되므로
        # (BaseRunner._load) 러너 수명 동안 1회만 알리면 충분하다.
        self._panic_alerted: bool = False
        # 섹터 집중 한도(risk_layer.max_sector_pct)용 종목→업종 매핑 캐시. 업종 분류는
        # 사실상 정적이라 러너 수명 동안 1회만 조회해 재사용한다(None=미조회, {}=조회 실패).
        self._sector_map: dict[str, str] | None = None

    def _log_start(self) -> None:
        rule = (self._cfg.get("selection", {}) or {}).get("universe_rule") or {}
        src = rule.get("source", "fixed")
        pool_desc = src if src != "fixed" else f"{len(self._cfg.get('universe', []))}종목"
        logger.info(
            "리밸런싱 전략 %d 실행 시작 (universe=%s, %s)",
            self.strategy_id, pool_desc, self._cfg.get("cadence"),
        )

    async def _tick_once(self) -> None:
        """정기 cadence 발화와 레짐 전환(청산/재진입)을 분리해 점검한다.

        백테스트 엔진과 동일 규약:
          - 청산: risk-off 이면 cadence 와 무관하게 즉시 전량 청산.
          - 재진입: risk-off→risk-on 회복 & 현금 상태이면 cadence 를 기다리지 않고 즉시 매수.
          - 정기 리밸런싱: is_rebalance_due 일 때만 실행하며, 이때만 마지막 실행일을 소비한다.
            레짐 재진입은 월간 스케줄(_set_last)을 소비하지 않아 정규 cadence 를 유지한다.
          - MDD 재진입: 킬스위치 쿨다운이 풀린 뒤 현금 상태이면 cadence 와 무관하게 즉시
            매수한다(백테스트 just_rearmed). 이것도 스케줄을 소비하지 않는다.

        예외로 panic_overlay 가 켜져 있으면(라이브 미구현) 이 전략의 매매를 중단한다 —
        MDD 킬스위치만 계속 동작한다. 아래 게이트 주석 참고.
        """
        now = now_kst()
        rf = self._cfg.get("regime_filter") or {}
        regime_enabled = bool(rf.get("enabled"))
        # async 래퍼로 1회만 판정(pykrx 영업일 조회의 이벤트 루프 블로킹 방지).
        # 같은 now 를 쓰므로 틱 내내 이 값을 재사용해도 판정이 달라지지 않는다.
        market_open = await is_market_open_async(now)
        logger.info(
            "전략 %d 틱 시작 (now=%s, regime_enabled=%s, market_open=%s)",
            self.strategy_id, now.isoformat(), regime_enabled, market_open,
        )

        # MDD 킬스위치(파국 백스톱) — 레짐/정기 리밸런싱보다 절대 우선(역할 분리: 레짐=추세
        # 오버레이, 킬스위치=고점 대비 낙폭 서킷브레이커). 장중에만 자산가치를 평가·청산한다.
        # 발동/쿨다운 중이면 레짐·cadence 로직을 건너뛰고 즉시 청산·현금 대피만 수행한다.
        # (변수명 risk_layer: engine.risk 모듈 import 를 가리지 않도록 한다.)
        risk_layer = self._cfg.get("risk_layer") or {}
        mdd_kill_pct = risk_layer.get("mdd_kill_pct")
        if mdd_kill_pct and market_open:
            killed = await self._evaluate_mdd_kill(
                float(mdd_kill_pct), int(risk_layer.get("mdd_rearm_days", 20) or 20)
            )
            if killed:
                logger.info(
                    "리밸런싱 전략 %d MDD 킬스위치 활성 — 즉시 청산·현금 대피(쿨다운 유지)",
                    self.strategy_id,
                )
                await self._rebalance_once(
                    now, risk_off=True, bar_tag="mdd", liq_kind="mdd"
                )
                return

        # 패닉 오버레이(config.panic_overlay)는 백테스트 엔진(portfolio.py 의 Arm→Confirm→
        # Fill 상태기계)에만 있고 라이브에는 구현이 없다. 조용히 무시하면 라이브가 사용자가
        # 검증한 백테스트보다 **항상 더 공격적**으로 돈다 — base_exposure(기본 0.70)를
        # 무시해 100% 투자로 굴고, event_only=True 면 "확인된 패닉 때만 진입, 평소 현금"
        # 이어야 할 전략이 상시 만기 투자 전략으로 뒤집힌다. 검증한 것과 다른 전략으로 실제
        # 자금을 움직이는 것이므로, 판단 근거를 못 얻었을 때와 같은 규약으로 무행동(보유
        # 유지 = 신규 매수보다 항상 덜 공격적)을 택하고 알린다.
        # MDD 킬스위치(위)만 예외로 계속 동작시킨다 — 배선 공백 때문에 파국 백스톱까지
        # 잃어서는 안 된다. 오버레이 자체가 아직 검증되지 않은 상태(docs/improvements.md
        # §47: 3차 시도까지 전 arm 이벤트 0건, 결론 보류)라 라이브 재구현이 아니라 거부를
        # 택했다. 키 부재/null 은 여기 걸리지 않는다(present-and-enabled 만 차단).
        if (self._cfg.get("panic_overlay") or {}).get("enabled"):
            await self._alert_panic_unsupported()
            # 거부 중에는 재진입하지 않으므로 미소비 재진입 요구를 남기지 않는다. 남겨두면
            # 나중에 panic_overlay 를 걷어내고 재기동했을 때 한참 지난 쿨다운 해제 요구로
            # 첫 틱에 오발화한다(상태를 쓰고 소비하지 않는 누수 방지).
            await self._clear_rearm_pending()
            return

        risk_off = False
        reentry = False
        # 레짐 전환 판정은 장중에만(주문 실행 가능 시점). 매 tick 확인해 회복 즉시 반응.
        if regime_enabled and market_open:
            logger.info("전략 %d 레짐 체크 진입", self.strategy_id)
            risk_off = await self._is_risk_off()
            logger.info("전략 %d 레짐 체크 완료: risk_off=%s", self.strategy_id, risk_off)
            prev = await self._get_regime()
            await self._set_regime(risk_off)
            # risk-off → risk-on 전환이고 현재 현금(보유 없음) 상태면 재진입 트리거.
            if not risk_off and prev is True and not await self._has_holdings():
                reentry = True

        # 청산: cadence 무관, 즉시. 마지막 실행일(월간 스케줄)은 소비하지 않는다.
        if risk_off:
            logger.info("리밸런싱 전략 %d 레짐 위험회피 — 즉시 청산 점검", self.strategy_id)
            await self._rebalance_once(now, risk_off=True, bar_tag="regime")
            return

        last = await self._get_last()
        due = is_rebalance_due(self._cfg, last, now)

        # MDD 쿨다운 해제 직후 강제 재진입(백테스트 just_rearmed 대응). 재가동 전환은
        # _evaluate_mdd_kill 이 rearm_pending 으로 남겨두므로, 장중·현금 상태이면 cadence 를
        # 기다리지 않고 즉시 매수한다(그러지 않으면 월간 cadence 에서 최대 한 달간 현금
        # 방치). risk_off 는 위에서 이미 걸러졌다 — 백테스트도 just_rearmed 재진입을
        # `not risk_off` 분기 안에서만 판정한다. 보유가 남아 있으면 재진입할 게 없으므로
        # 요구만 소비한다(백테스트의 `just_rearmed and not val` 과 동일).
        rearm = False
        if mdd_kill_pct and market_open:
            state = await self._get_mdd_state()
            if state and state.get("rearm_pending"):
                rearm = not await self._has_holdings()
                if not rearm:
                    await self._clear_rearm_pending()

        logger.info(
            "전략 %d 발화 판정: last=%s due=%s reentry=%s rearm=%s",
            self.strategy_id, last, due, reentry, rearm,
        )

        # 콜드 스타트 즉시 발화: initial_fill_immediate=true 이고 아직 한 번도 실행한 적이
        # 없으며(last 미기록 & 보유 없음) 장중이면, cadence 발화일/시각을 기다리지 않고 즉시
        # 1회 매수한다. 정규 스케줄을 소비(_set_last)해 이번 주기의 정기 리밸런싱을 대체하고
        # 중복 발화를 막는다. due 가 이미 True 면(발화일 도래) 굳이 부트스트랩할 필요 없다.
        bootstrap = (
            not due
            and bool(self._cfg.get("initial_fill_immediate"))
            and last is None
            and market_open
            and not await self._has_holdings()
        )

        if not (due or reentry or bootstrap or rearm):
            return

        # 부트스트랩은 정규 리밸런싱을 대체하므로 "rebal" 태그. 재진입은 같은 거래일에
        # 정기 발화·레짐 액션과 멱등성 키가 겹치지 않도록 사유별로 태그를 나눈다.
        if due or bootstrap:
            kind, bar_tag = ("정기 발화" if due else "콜드스타트 즉시 발화"), "rebal"
        elif reentry:
            kind, bar_tag = "레짐 재진입", "regime"
        else:
            kind, bar_tag = "MDD 쿨다운 해제 재진입", "rearm"
        logger.info("리밸런싱 전략 %d %s (%s)", self.strategy_id, kind, now.isoformat())
        await self._rebalance_once(now, risk_off=False, bar_tag=bar_tag)
        if due or bootstrap:  # 재진입은 월간 cadence 스케줄을 소비하지 않는다
            await self._set_last(now)
        if rearm:
            # 리밸런싱을 마쳤으면 소비한다. 예외로 여기까지 못 오면(틱 실패) 요구가 남아
            # 다음 틱에 재시도된다. 선정 결과가 비어 매수가 없었던 경우도 소비하는데,
            # 백테스트도 just_rearmed 를 그 봉에서 한 번 쓰고 버린다(동일 규약).
            await self._clear_rearm_pending()

    # ───────────────────── 마지막 실행일 상태 ─────────────────────
    def _last_key(self) -> str:
        return f"{_LAST_PREFIX}{self.strategy_id}"

    async def _get_last(self) -> datetime | None:
        raw = await self.redis.get(self._last_key())
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    async def _set_last(self, dt: datetime) -> None:
        await self.redis.set(self._last_key(), dt.isoformat(), ex=_LAST_TTL)

    # ───────────────────── 직전 레짐 상태(전환 판정용) ─────────────────────
    def _regime_key(self) -> str:
        return f"{_REGIME_PREFIX}{self.strategy_id}"

    async def _get_regime(self) -> bool | None:
        """직전 tick 의 risk-off 여부. 미기록(최초)이면 None."""
        raw = await self.redis.get(self._regime_key())
        if raw is None:
            return None
        return raw == "1"

    async def _set_regime(self, risk_off: bool) -> None:
        await self.redis.set(self._regime_key(), "1" if risk_off else "0", ex=_LAST_TTL)

    async def _has_holdings(self) -> bool:
        """이 전략의 보유 포지션(수량>0)이 하나라도 있는지 — PIT 후보풀과 무관하게 판정한다.

        예전엔 `_holdings(db, pool)` 로 PIT 후보풀 필터를 거쳤는데, 후보풀에서
        빠진 종목만 보유한 전략은 "보유 전무"로 오판돼 MDD rearm·재진입·bootstrap
        로직이 기존 포지션 위에 중복 진입을 시도할 수 있었다(§54). 백테스트의
        `not val` 판정(포지션 딕셔너리 자체를 봄, 후보풀과 무관)과 계약을 맞춘다.
        """
        async with AsyncSessionLocal() as db:
            exists = await db.scalar(
                select(Position.id)
                .where(
                    Position.user_id == self._user_id,
                    Position.strategy_id == self.strategy_id,
                    Position.qty > 0,
                )
                .limit(1)
            )
        return exists is not None

    # ───────────────────── MDD 킬스위치 상태(고점 HWM·발동) ─────────────────────
    #
    # 백테스트(app.services.backtest.portfolio)는 bar-by-bar 로 계좌 자산가치를 추적해
    # 고점(HWM) 대비 낙폭이 mdd_kill_pct 를 넘으면 전량 청산하고 mdd_rearm_days 거래일
    # 쿨다운 후 고점 기준선을 리셋해 재가동한다. 라이브 러너는 그 상태(HWM·발동 여부·
    # 발동일)를 틱 사이에 계속 들고 있어야 하므로 Redis 에 영속화한다(레짐/마지막 실행일과
    # 동일 prefix·TTL 규약). Redis 유실 시 상태가 초기화되어 HWM 이 현재 자산가치로
    # 재설정되므로 데이터 오류로 인한 오발동(전량 청산)은 발생하지 않는다(안전측: 새 낙폭이
    # 임계에 도달해야만 발동). 이는 레짐/마지막 실행일 상태와 동일한 내구성 특성이다.
    def _mdd_key(self) -> str:
        return mdd_state_key(self.strategy_id)

    async def _get_mdd_state(self) -> dict | None:
        """저장된 MDD 상태(hwm·killed·kill_date). 미기록/손상 시 None."""
        raw = await self.redis.get(self._mdd_key())
        if not raw:
            return None
        try:
            state = json.loads(raw)
            if not isinstance(state, dict) or "hwm" not in state:
                return None
            return state
        except (ValueError, TypeError):
            return None

    async def _set_mdd_state(
        self, hwm: float, killed: bool, kill_date: date | None,
        rearm_pending: bool = False,
    ) -> None:
        state = {
            "hwm": float(hwm),
            "killed": bool(killed),
            "kill_date": kill_date.isoformat() if kill_date else None,
            "rearm_pending": bool(rearm_pending),
        }
        await self.redis.set(self._mdd_key(), json.dumps(state), ex=_LAST_TTL)

    async def _clear_rearm_pending(self) -> None:
        """강제 재진입 요구(rearm_pending)를 소비한다 — 재진입 리밸런싱을 마친 뒤 호출."""
        state = await self._get_mdd_state()
        if not state or not state.get("rearm_pending"):
            return
        kd = state.get("kill_date")
        await self._set_mdd_state(
            float(state["hwm"]),
            bool(state.get("killed")),
            date.fromisoformat(kd) if kd else None,
            rearm_pending=False,
        )

    async def _live_equity(self) -> float:
        """라이브 계좌 자산가치 근사 = 체결 재생 현금잔고 + 보유 종목 시가평가(§11).

        라이브에는 별도의 현금 잔고 원장이 없어(리밸런싱은 config.capital 대비 목표비중으로
        수량을 정한다), 이 전략의 전체 체결(executions) 이력을 재생해 '지금' 시점의 현금
        잔고를 계산한다(tracking.replay_cash_balance — §5 에서 만든 실행 기반 재구성 로직
        재사용). 매도 체결의 현금흐름에는 매수원가와의 차익(실현손익)이 자연히 반영되므로,
        과거 라운드트립에서 확정된 실현손익이 자산가치에 포함된다:

            equity = 체결 재생 현금잔고 + Σ 현재 보유수량×현재가[시가평가]

        구 근사(capital + 미실현손익)는 확정 실현손익을 무시해 손실 라운드트립 이후에도
        자산가치가 낙관적으로 리셋되는 편향이 있었다(MDD 킬스위치 과소 발동 방향).

        평가 대상은 **이 전략의 보유 전체**이며 현재 후보풀(PIT 유니버스)로 거르지 않는다.
        지수 정기변경으로 후보풀에서 빠진 보유종목도 청산 전까지는 실제 자금이므로, 빼면
        재구성일에 자산가치가 불연속으로 급락해 고점(HWM)·낙폭 판정이 오염된다(_holdings
        의 universe 필터는 '이번에 매매할 후보'를 좁히는 것이라 목적이 다르다).

        시가평가 가격은 현재가(REST) → 로컬 종가(price_ticks 최신) → 평단가 순으로
        떨어진다. 평단가를 시세 대체값으로 쓰는 것은 보수적이지 않다 — 킬스위치가 필요한
        급락 국면일수록 평단가는 시가보다 높아 자산가치를 과대평가하고 발동을 늦춘다.
        그래서 마지막으로 알려진 실제 시장가(로컬 종가)를 먼저 쓰고, 그마저 없을 때만
        (편입 직후 등 종가 이력 자체가 없는 경우) 평단가로 떨어진다.
        """
        capital = float(self._cfg.get("capital", 10_000_000))
        async with AsyncSessionLocal() as db:
            orders = list(
                await db.scalars(
                    select(Order)
                    .options(selectinload(Order.executions))
                    .where(
                        Order.user_id == self._user_id,
                        Order.strategy_id == self.strategy_id,
                        Order.status.in_(_FILLED_STATUSES),
                    )
                )
            )
        executions = [
            {
                "symbol": o.symbol,
                "side": str(o.side),
                "qty": float(e.filled_qty),
                "price": float(e.filled_price),
                "fee": float(e.fee) if e.fee is not None else 0.0,
                "date": pd.Timestamp(e.executed_at.astimezone(KST).date()),
            }
            for o in orders
            for e in o.executions
            if float(e.filled_qty) > 0 and float(e.filled_price) > 0
        ]
        cash = replay_cash_balance(executions, capital)

        async with AsyncSessionLocal() as db:
            rows = await db.scalars(
                select(Position).where(
                    Position.user_id == self._user_id,
                    Position.strategy_id == self.strategy_id,
                    Position.qty > 0,
                )
            )
            positions = list(rows)
        if not positions:
            return cash
        prices = await self._quotes({p.symbol for p in positions})
        missing = [p.symbol for p in positions if p.symbol not in prices]
        last_closes = await self._last_known_closes(missing) if missing else {}
        market_value = 0.0
        for p in positions:
            price = prices.get(p.symbol)
            if price is None:
                price = last_closes.get(p.symbol)
            if price is None:
                # 시세·종가 이력 둘 다 없음 → 남은 값은 평단가뿐. 자산가치가 과대평가되는
                # 방향이므로(킬스위치 지연) 조용히 넘기지 않고 남긴다.
                price = p.avg_price
                logger.warning(
                    "전략 %d %s 현재가·로컬 종가 모두 없음 — 평단가로 평가(자산가치 과대평가 위험)",
                    self.strategy_id, p.symbol,
                )
            market_value += float(p.qty) * float(price)
        return cash + market_value

    async def _last_known_closes(self, symbols: list[str]) -> dict[str, float]:
        """로컬 price_ticks 의 마지막 종가를 종목별로 반환(외부 조회 없음).

        시세 조회 실패 종목의 시가평가 대체값. 시세 조회가 실패하는 국면(급락·장애)에
        외부를 한 번 더 두드리면 같은 이유로 또 실패하거나 틱을 지연시키므로 DB 만 읽는다.
        조회 창(_CLOSE_FALLBACK_DAYS)을 넘도록 종가가 없으면 그 종목은 빠진다.
        """
        end = datetime.now()
        start = end - timedelta(days=_CLOSE_FALLBACK_DAYS)
        out: dict[str, float] = {}
        async with AsyncSessionLocal() as db:
            for sym in symbols:
                series = await get_close_series(db, sym, start, end)
                if len(series):
                    out[sym] = float(series.iloc[-1])
        return out

    def _business_days_since(self, start: date, end: date) -> int:
        """start(제외)~end(포함) 사이 영업일 수. 백테스트의 (i − kill_idx) 와 동일 의미.

        킬 발동일 다음 거래일부터 카운트해, mdd_rearm_days 거래일이 경과하면 재가동한다.
        """
        if end <= start:
            return 0
        count = 0
        d = start + timedelta(days=1)
        while d <= end:
            if is_business_day(d):
                count += 1
            d += timedelta(days=1)
        return count

    async def _evaluate_mdd_kill(self, mdd_kill_pct: float, rearm_days: int) -> bool:
        """MDD 킬스위치 상태를 갱신하고 현재 '발동(현금 대피)' 여부를 반환한다.

        백테스트(portfolio.py)와 동일 규약·순서:
          1) 고점(HWM) 갱신: 현재 자산가치가 HWM 을 넘으면 상향.
          2) 쿨다운 재가동: 발동 상태에서 발동일 이후 rearm_days 거래일 경과 시 재가동
             (killed=False) + 고점 기준선을 현재 자산가치로 리셋.
          3) 발동 판정: 미발동 상태에서 고점 대비 낙폭이 −mdd_kill_pct 이하이면 발동
             (killed=True, 발동일=오늘).

        2)의 재가동 전환은 백테스트의 just_rearmed(portfolio.py) 에 대응한다. 백테스트는
        그 봉에서 곧바로 강제 재진입하지만, 라이브는 전환을 상태(rearm_pending)로 남겨
        _tick_once 가 소비한다 — 전환은 여기서 이미 Redis 에 커밋되므로, 인메모리 플래그로
        넘기면 그 뒤 틱이 실패(타임아웃·DataSourceError)했을 때 전환이 영영 사라져 다음
        cadence(월간이면 최대 한 달)까지 현금으로 방치된다. 소비는 재진입 리밸런싱을
        마친 뒤에만 일어난다(_clear_rearm_pending).
        """
        equity = await self._live_equity()
        today = now_kst().date()
        state = await self._get_mdd_state()
        if state is None:  # 최초/유실 → 현재 자산가치를 고점으로 초기화(안전측: 오발동 방지)
            hwm, killed, kill_date, rearm_pending = equity, False, None, False
        else:
            hwm = float(state["hwm"])
            killed = bool(state.get("killed"))
            kd = state.get("kill_date")
            kill_date = date.fromisoformat(kd) if kd else None
            rearm_pending = bool(state.get("rearm_pending"))

        if equity > hwm:
            hwm = equity
        # 쿨다운 경과 → 재가동(고점 기준선 리셋)
        # to_thread: 영업일 카운트가 날짜별 pykrx 조회(미캐시 시 네트워크)를 동반하므로
        # 이벤트 루프를 멈추지 않게 스레드로 오프로드한다.
        if killed and kill_date is not None and (
            await asyncio.to_thread(self._business_days_since, kill_date, today)
            >= rearm_days
        ):
            killed = False
            kill_date = None
            hwm = equity
            rearm_pending = True  # 백테스트 just_rearmed — 강제 재진입 요구를 남긴다
            logger.info(
                "리밸런싱 전략 %d MDD 킬스위치 쿨다운 경과 — 재가동(고점 리셋 %.0f), "
                "cadence 무관 재진입 예약",
                self.strategy_id, hwm,
            )
        # 발동 판정(미발동 상태에서만)
        if not killed and hwm > 0 and equity / hwm - 1.0 <= -mdd_kill_pct:
            killed = True
            kill_date = today
            rearm_pending = False  # 다시 청산하므로 미소비 재진입 요구는 무효
            drawdown_pct = (equity / hwm - 1.0) * 100
            logger.warning(
                "리밸런싱 전략 %d MDD 킬스위치 발동 — 자산가치 %.0f / 고점 %.0f "
                "(낙폭 %.2f%% ≤ −%.2f%%) → 전량 청산·현금 대피",
                self.strategy_id, equity, hwm, drawdown_pct, mdd_kill_pct * 100,
            )
            # 파국 방어 발동은 즉시 알림(critical). 발동은 이 전환 지점에서만 일어나므로
            # (killed 지속·쿨다운 중에는 재진입하지 않음) 자연스러운 dedup 이 된다.
            await publish_alert(
                self.redis,
                user_id=self._user_id,
                strategy_id=self.strategy_id,
                severity="critical",
                code="mdd_kill",
                message=(
                    f"MDD 킬스위치 발동 — 고점 {hwm:,.0f} 대비 낙폭 {drawdown_pct:.2f}%가 "
                    f"임계 −{mdd_kill_pct * 100:.1f}%를 넘어 전량 청산·현금 대피"
                ),
            )

        await self._set_mdd_state(hwm, killed, kill_date, rearm_pending)
        return killed

    # ───────────────────── 목표비중 산정(주문 I/O 없음) ─────────────────────
    async def _compute_plan(
        self, as_of, risk_off: bool
    ) -> tuple[list[str], dict[str, float]]:
        """리밸런싱 목표비중을 산정한다 — 주문·체결 없는 순수 계획.

        백테스트 _targets_at 와 동일 파이프라인: 후보풀 해석(고정/PIT) → 동적 상대강도
        축소 → (score 면) 종합점수 산정 → compute_target_weights.

        :param risk_off: 현금화 오버레이 여부. True 면 목표 빈 dict(전량 청산).
        :return: (pool, targets). pool 은 축소 전 후보풀 전체(보유 청산 판정용). risk_off
            여도 pool 을 함께 반환해 후보풀 밖 보유를 매도 대상으로 삼는다.
        """
        # 후보풀(pool): 고정 목록 또는 시점별(PIT) 지수 구성종목. 보유 청산 판정은 축소 전
        # pool 전체로 해야 후보풀에서 빠진 보유종목도 목표 0 으로 매도된다.
        pool = await self._resolve_universe(as_of)
        logger.info("전략 %d 후보풀 확보: %d종목", self.strategy_id, len(pool))
        if risk_off:
            return pool, {}

        selection = self._cfg.get("selection", {})
        method = selection.get("method")
        rule = selection.get("universe_rule") or {}
        weighting = self._cfg.get("weighting", "equal")
        scores: dict[str, float] | None = None
        history: dict[str, pd.Series] = {}

        # 가격기반 선정(momentum/custom/all)·동적 상대강도 축소·inverse_vol 변동성 산출에는
        # 종가 히스토리가 필요하다(method="score" 만으로는 불필요하지만 weighting=
        # "inverse_vol" 이면 method 무관하게 필요 — compute_target_weights 가 vols 미주입 시
        # price_history 로 변동성을 산출한다. 백테스트의 _compute_vol_ann(tail(253))과
        # 동일 정의를 맞추려면 최소 253봉을 확보해야 한다).
        # 리스크 레이어(P1-2) 변동성 타겟팅도 종가 히스토리를 요구한다(vol_lookback 봉+1).
        risk_layer = self._cfg.get("risk_layer") or {}
        want_vol_target = bool(risk_layer.get("target_vol"))
        # 변동성 적격 게이트(옵셔널, method="score")도 종가 히스토리를 요구한다
        # (base_lookback+1 봉). 미지정이면 기존대로 히스토리 시딩 없이 선정한다.
        vol_gate = selection.get("vol_gate") or {}
        want_vol_gate = bool(vol_gate)
        need_hist = (
            bool(rule.get("type"))
            or method in ("momentum", "custom", "all")
            or weighting == "inverse_vol"
            or want_vol_target
            or want_vol_gate
        )
        if need_hist:
            min_bars = int(rule.get("lookback", 0)) + 1 if rule.get("type") else 0
            if weighting == "inverse_vol":
                min_bars = max(min_bars, 253)
            if want_vol_target:
                min_bars = max(min_bars, int(risk_layer.get("vol_lookback", 20) or 20) + 1)
            if want_vol_gate:
                min_bars = max(min_bars, int(vol_gate.get("base_lookback", 252) or 252) + 1)
            history = await self._seed_history(pool, min_bars=min_bars)
            logger.info("전략 %d 히스토리 시딩 완료: %d종목", self.strategy_id, len(history))

        # 동적 유니버스: 상대강도 상위 pick 으로 후보풀 축소(백테스트 _dynamic_universe 동일).
        universe = pool
        if rule.get("type"):
            from app.services.backtest.portfolio import _dynamic_universe

            universe = _dynamic_universe(pd.DataFrame(history), pool, rule)
            logger.info("전략 %d 동적 유니버스 축소: %d종목", self.strategy_id, len(universe))

        # compute_target_weights 는 cfg["universe"] 로 후보를 재필터하므로 해석된 유니버스를 주입.
        cfg = {**self._cfg, "universe": universe}
        if method == "score":
            # 종합점수는 확정 영업일(직전 거래일 종가) 기준으로만 산정해 미래참조를 방지한다
            # (rebalance_time 은 통상 장중이라 당일 종가는 아직 미확정).
            factor_weights = selection.get("factor_weights")
            neutralize = selection.get("neutralize", "none")
            financial_period = self._cfg.get("financial_period", "annual")
            flow_window = int(selection.get("flow_window", 90))
            flow_denom = selection.get("flow_denom", "mcap")
            rm_reg = int(selection.get("resid_mom_reg_window", 36))
            rm_win = int(selection.get("resid_mom_window", 11))
            rm_skip = int(selection.get("resid_mom_skip", 1))
            pead_lookback_q = int(selection.get("pead_lookback_q", 8))
            scores = await asyncio.to_thread(
                compute_universe_scores,
                universe, as_of, factor_weights, neutralize, financial_period,
                flow_window, flow_denom, rm_reg, rm_win, rm_skip, pead_lookback_q,
            )
            logger.info("전략 %d 종합점수 산정 완료: %d종목", self.strategy_id, len(scores))
        targets = compute_target_weights(history, cfg, scores=scores)
        logger.info("전략 %d 목표비중 산정 완료: %d종목", self.strategy_id, len(targets))
        # 리스크 레이어(P1-2): 종목 집중 한도·변동성 타겟팅을 목표비중에 적용(백테스트
        # _targets_at 와 동일 로직 재사용). MDD 킬스위치는 계좌 고점(HWM) 상태 지속이
        # 필요해 _tick_once 상단에서 별도로 평가·청산한다(_evaluate_mdd_kill).
        if risk_layer and targets:
            from app.services.backtest.portfolio import _apply_risk_caps

            smap = await self._get_sector_map() if risk_layer.get("max_sector_pct") else None
            targets = _apply_risk_caps(targets, pd.DataFrame(history), risk_layer, smap)
        return pool, targets

    async def _get_sector_map(self) -> dict[str, str]:
        """섹터 집중 한도용 종목→업종 매핑을 러너 수명 동안 1회만 조회해 캐시한다.

        블로킹 KRX 조회이므로 스레드풀에서 실행한다. 미확보(빈 dict) 시 섹터 캡은
        _apply_risk_caps 에서 조용히 미적용된다. 실패는 캐시하지 않아(빈 dict 는 조회
        시도로 간주) 다음 리밸런싱에 재시도한다.

        조회 **실패**도 같은 저하(빈 매핑 → 섹터 캡 미적용)로 수렴시킨다. 예외를 그대로
        올리면 _compute_plan → _rebalance_once 를 타고 틱 전체가 죽어 **리밸런싱 자체가
        무산된다** — 선택적 리스크 한도 하나 때문에 주문을 못 내는 것은 과하고, 백테스트
        (portfolio.py)가 같은 상황에서 저하하는 것과도 어긋난다. 다만 '자료 없음'과는
        다른 사건이므로 ERROR 로 구분해 남긴다.
        """
        if self._sector_map:
            return self._sector_map
        from app.services.data.errors import DataSourceError
        from app.services.data.krx_index import sector_map as _sector_map_fn

        try:
            smap = await asyncio.to_thread(_sector_map_fn)
        except DataSourceError as e:
            logger.error(
                "전략 %d 섹터 한도용 업종 매핑 조회 실패 — 섹터 캡 미적용: %s",
                self.strategy_id, e,
            )
            return {}  # 실패는 캐시하지 않는다(다음 리밸런싱에 재시도)
        if smap:
            self._sector_map = smap
            logger.info("전략 %d 섹터 한도용 업종 매핑 로드: %d종목", self.strategy_id, len(smap))
        else:
            logger.warning("전략 %d 섹터 한도 설정됨이나 업종 매핑 미확보 — 섹터 캡 미적용.", self.strategy_id)
        return smap

    async def preview(self) -> dict:
        """주문을 내지 않고 '지금 리밸런싱하면 낼 주문'을 계산해 반환한다(노트북·점검용).

        선정·목표비중·현재 보유·현재가·산출 주문을 그대로 담아 돌려준다. 실제 매매는
        하지 않으므로 모의투자 전에 무엇을 살지/팔지 안전하게 확인할 수 있다.
        """
        if not self._cfg and not await self._load():
            raise RuntimeError(f"전략 {self.strategy_id} 적재 실패(없거나 자격증명 미등록)")

        as_of = _last_business_day()
        risk_off = await self._is_risk_off()
        pool, targets = await self._compute_plan(as_of, risk_off)

        async with AsyncSessionLocal() as db:
            positions = await self._holdings(db, pool)

        symbols = set(targets) | set(positions)
        prices = await self._quotes(symbols)
        drift_band = 0.0 if risk_off else float(self._cfg.get("drift_band_pct", 0.05))
        orders = compute_rebalance_orders(
            targets=targets,
            positions={s: float(q) for s, q in positions.items()},
            prices={s: float(p) for s, p in prices.items()},
            capital=float(self._cfg.get("capital", 10_000_000)),
            drift_band=drift_band,
        )
        return {
            "as_of": as_of.isoformat(),
            "risk_off": risk_off,
            "pool_size": len(pool),
            "targets": targets,
            "positions": {s: float(q) for s, q in positions.items()},
            "prices": {s: float(p) for s, p in prices.items()},
            "orders": orders,
        }

    # ───────────────────── 리밸런싱 1회 ─────────────────────
    async def _rebalance_once(
        self, now: datetime, risk_off: bool | None = None, bar_tag: str = "rebal",
        liq_kind: str = "regime",
    ) -> None:
        """리밸런싱 1회 실행.

        :param risk_off: 레짐 판정 결과를 호출부에서 미리 계산해 주입(중복 지수조회 회피).
            None 이면 이 함수가 _is_risk_off 로 직접 판정한다.
        :param bar_tag: 멱등성 키에 들어가는 봉 태그. 같은 거래일에 정기 리밸런싱("rebal")과
            레짐 액션("regime"), MDD 킬스위치("mdd")가 서로 다른 키를 갖도록 구분해
            중복주문을 방지·허용한다.
        :param liq_kind: 청산 사유 종류("regime"=레짐 현금화, "mdd"=MDD 킬스위치). 감사
            로그 매도 사유 문구 결정에만 쓴다.
        """
        as_of = _last_business_day()
        # 현금화 오버레이(레짐 필터): 위험회피 국면이면 목표를 빈 dict 로 두어 보유를
        # 전량 청산(현금화)하고 신규 매수를 하지 않는다. 백테스트 엔진과 동일 규약.
        if risk_off is None:
            risk_off = await self._is_risk_off()

        pool, targets = await self._compute_plan(as_of, risk_off)
        if risk_off:
            logger.info(
                "리밸런싱 전략 %d 레짐 위험회피 — 보유 청산·신규 매수 중단", self.strategy_id
            )
        elif not targets:
            logger.warning("리밸런싱 전략 %d 선정 종목 없음(데이터 부족) — 건너뜀", self.strategy_id)
            # 팩터/가격 조회 전면 장애 등으로 목표 종목이 하나도 산정되지 않아 이번
            # 리밸런싱을 통째로 건너뛰는 상황(조용한 스킵) — 사용자에게 알린다. 리밸런싱은
            # cadence/레짐 게이팅으로 저빈도라 발생 시마다 알려도 폭주하지 않는다.
            await publish_alert(
                self.redis,
                user_id=self._user_id,
                strategy_id=self.strategy_id,
                severity="warning",
                code="factor_outage",
                message=(
                    f"리밸런싱 발화 시점에 선정 종목이 하나도 산정되지 않아 이번 "
                    f"리밸런싱을 건너뜁니다(팩터/가격 데이터 조회 장애 가능). 다음 주기 재시도."
                ),
            )
            return

        async with AsyncSessionLocal() as db:
            positions = await self._holdings(db, pool)

        # 청산 국면인데 보유도 없으면 할 일이 없다.
        if not targets and not positions:
            logger.info("리밸런싱 전략 %d 레짐 위험회피 & 보유 없음 — 매매 없음", self.strategy_id)
            return

        # 매매 후보 = 목표 종목 ∪ 현재 보유 종목
        symbols = set(targets) | set(positions)
        logger.info("전략 %d 시세 조회 시작: %d종목", self.strategy_id, len(symbols))
        prices = await self._quotes(symbols)
        logger.info("전략 %d 시세 조회 완료: %d/%d종목 성공", self.strategy_id, len(prices), len(symbols))

        # 청산 국면에서는 드리프트 밴드를 무시(0)하고 보유를 전량 매도한다.
        drift_band = 0.0 if risk_off else float(self._cfg.get("drift_band_pct", 0.05))
        orders = compute_rebalance_orders(
            targets=targets,
            positions={s: float(q) for s, q in positions.items()},
            prices={s: float(p) for s, p in prices.items()},
            capital=float(self._cfg.get("capital", 10_000_000)),
            drift_band=drift_band,
        )
        if not orders:
            logger.info("리밸런싱 전략 %d 드리프트 밴드 내 — 매매 없음", self.strategy_id)
            return

        bar_ts = f"{now.date().isoformat()}:{bar_tag}"
        await self._execute_orders(
            orders, prices, positions, bar_ts, targets, risk_off, liq_kind
        )

    def _sell_reason(
        self, sym: str, targets: dict[str, float], risk_off: bool, sell_qty: int,
        liq_kind: str = "regime",
    ) -> str:
        """리밸런싱 매도 사유 문장(감사 로그용).

        - MDD 킬스위치: 고점 대비 낙폭 임계 초과로 파국 방어 전량 청산.
        - 레짐 위험회피: 현금화 전량 청산.
        - 선정 제외(목표비중 0): 후보에서 탈락해 전량 청산.
        - 비중 축소: 드리프트 밴드 초과분만 부분 매도.
        """
        if risk_off:
            if liq_kind == "mdd":
                return (
                    f"MDD 킬스위치 발동(고점 대비 낙폭 임계 초과): {sell_qty:,}주 전량 청산·현금 대피"
                )
            return f"레짐 위험회피(현금화): {sell_qty:,}주 전량 청산"
        weight = targets.get(sym, 0.0)
        if weight <= 0:
            return f"리밸런싱 선정 제외(목표비중 0%): {sell_qty:,}주 전량 청산"
        return (
            f"리밸런싱 비중 축소: 목표비중 {weight * 100:.1f}%로 조정 "
            f"(드리프트 밴드 초과분 {sell_qty:,}주 매도)"
        )

    async def _is_risk_off(self) -> bool:
        """현금화 오버레이(레짐 필터) 판정 — stateful 히스테리시스(비대칭 밴드).

        무상태 rs<ma 대신 직전 레짐 상태(Redis rebalance:regime:{id})를 읽어 밴드로 판정한다.
        백테스트 _regime_on_flags 와 동일 규약:
          - 직전 위험선호(on): 지수 < MA×(1 − exit_buffer) 일 때만 청산(off).
          - 직전 위험회피(off): 지수 ≥ MA×(1 + reentry_buffer) 일 때만 재진입(on).
          - 상태 없음(최초): 지수 ≥ MA 면 on(off=False), 아니면 off.
        exit_buffer=reentry_buffer=0.0 이면 기존 무상태 rs<ma 와 완전히 동일하다(하위호환).

        미래참조 방지를 위해 직전 확정 영업일까지의 종가로 이동평균을 계산한다.
        새 상태 저장은 호출부(_tick_once 의 _set_regime)가 담당한다.

        레짐 필터가 꺼져 있으면 False(투자 유지)를 반환한다. 기준지수 조회 결과에
        대해서는 두 경우를 구분해야 한다(2026-08-06 로컬 저장소 도입으로 계약이
        바뀌었다):

        - **조회 자체가 실패**(``_fetch_index_ohlcv`` 가 ``DataSourceError`` 를 던짐):
          이 함수 밖으로 그대로 전파한다. 이전 판은 여기서 예외를 흡수해 False(투자
          유지 = 신규 매수 가능)를 반환했지만, 그건 "기준지수를 못 읽었으니 위험선호로
          간주해 실제 주문을 낸다"는 뜻이었다 — 위험회피 국면인데 지수를 못 읽었다는
          이유로 매수가 나갈 수 있는, 데이터 부재를 정상으로 뭉개고 실제 자금을
          움직이는 §44-1/§47 의 가장 나쁜 판본이었다. 지금은 예외가 `_tick_once` →
          `BaseRunner.run` 의 `except Exception` 까지 올라가 이번 틱 전체가 실패로
          기록되고(`_record_tick_failure`, 누적 시 `runner_failures` 알림) 다음 주기에
          재시도한다. 이번 틱은 매수·매도 어느 쪽도 내지 않는다(무행동=보유 유지이므로
          신규 매수보다 항상 덜 공격적) — 이는 의도된 정책 변경이다.
        - **조회는 성공했지만 실제로 데이터가 없음**(빈 프레임): 여전히 False(투자
          유지)를 반환한다. 아래 `len(close) < ma_period`("데이터 부족") 분기도 같은
          이유(조회 실패가 아니라 이력이 실제로 짧은 진짜 데이터 상태)로 False 를
          반환하며, 이 두 분기는 건드리지 않았다.
        """
        rf = self._cfg.get("regime_filter") or {}
        if not rf.get("enabled"):
            return False

        ma_period = int(rf.get("ma_period", 200))
        exit_buffer = max(0.0, float(rf.get("exit_buffer_pct", 0.0) or 0.0))
        reentry_buffer = max(0.0, float(rf.get("reentry_buffer_pct", 0.0) or 0.0))
        ticker = _REGIME_INDEX_TICKER.get(rf.get("index", "KOSPI"), "1001")
        as_of = _last_business_day()
        start = _approx_start(as_of, ma_period + 10)  # 이동평균에 필요한 거래일 확보

        df = await asyncio.to_thread(
            _fetch_index_ohlcv, _ymd(start), _ymd(as_of), ticker
        )
        if df is None or df.empty or "close" not in df.columns:
            logger.warning(
                "리밸런싱 전략 %d 레짐 기준지수 조회 실패 — 오버레이 미적용(투자 유지)",
                self.strategy_id,
            )
            return False

        close = df["close"].dropna()
        if len(close) < ma_period:
            logger.warning(
                "리밸런싱 전략 %d 레짐 기준지수 데이터 부족(%d<%d) — 투자 유지",
                self.strategy_id, len(close), ma_period,
            )
            return False

        ma = float(close.tail(ma_period).mean())
        last = float(close.iloc[-1])

        # 직전 레짐 상태(risk-off 여부). 미기록(최초)이면 None → 무상태 초기화.
        prev_off = await self._get_regime()
        if prev_off is None:
            off = last < ma
        elif prev_off is False:               # 직전 위험선호(on)
            off = last < ma * (1.0 - exit_buffer)
        else:                                 # 직전 위험회피(off)
            off = not (last >= ma * (1.0 + reentry_buffer))

        logger.info(
            "리밸런싱 전략 %d 레짐 판정(히스테리시스): 지수=%.1f MA%d=%.1f "
            "exitθ=%.1f reentryθ=%.1f 직전=%s → %s",
            self.strategy_id, last, ma_period, ma,
            ma * (1.0 - exit_buffer), ma * (1.0 + reentry_buffer),
            "없음" if prev_off is None else ("위험회피" if prev_off else "위험선호"),
            "위험회피(현금화)" if off else "위험선호(투자)",
        )
        return off

    async def _resolve_universe(self, as_of) -> list[str]:
        """후보 유니버스(pool)를 해석한다 — 고정 목록 또는 시점별(PIT) 지수 구성종목.

        selection.universe_rule.source 가 지수명(KOSPI200/KOSPI100/KRX300)이면 as_of
        기준 '현재 구성종목'을 후보풀로 쓴다. 백테스트는 리밸런싱일별 롤링 멤버십이지만,
        실거래에서는 '지금 이 시점의 구성'이 곧 PIT 다(미래참조 없음). source='fixed'
        (기본)이면 config.universe 를 그대로 쓴다. 조회 실패/공백이면 config.universe 폴백.

        :param as_of: 멤버십 기준일(직전 확정 영업일). 반환값은 6자리 종목코드 목록.
        """
        selection = self._cfg.get("selection", {})
        rule = selection.get("universe_rule") or {}
        source = rule.get("source", "fixed")
        fixed = list(self._cfg.get("universe", []))
        if source == "fixed":
            return fixed

        from app.services.data import krx_index

        logger.info("전략 %d PIT 유니버스(%s) 조회 진입", self.strategy_id, source)
        try:
            members = await asyncio.to_thread(krx_index.index_members, as_of, source)
            logger.info(
                "전략 %d PIT 유니버스(%s) 조회 완료: %d종목", self.strategy_id, source, len(members),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "전략 %d PIT 유니버스(%s) 조회 실패 — config.universe 폴백: %s",
                self.strategy_id, source, e,
            )
            await self._alert_pit_fallback(source, f"조회 실패({e})")
            return fixed
        if not members:
            logger.warning(
                "전략 %d PIT 유니버스(%s) 비어있음 — config.universe 폴백",
                self.strategy_id, source,
            )
            await self._alert_pit_fallback(source, "구성종목 비어있음")
            return fixed

        # PIT 조회 성공 — 폴백 상태였다면 해제(다음 폴백 진입 시 재알림 가능).
        self._pit_fallback_active = False

        # 유동성 필터: 시가총액(억 원) 하한 미만 종목 제외(PIT — as_of 시점 시총 기준).
        min_cap = rule.get("min_market_cap")
        if min_cap:
            caps = await asyncio.to_thread(krx_index.market_caps, as_of)
            if caps:  # 조회 성공 시에만 필터(실패 시 원본 유지)
                min_cap_won = int(min_cap) * 10**8
                members = [c for c in members if caps.get(c, 0) >= min_cap_won]
        return members

    async def _alert_pit_fallback(self, source: str, cause: str) -> None:
        """PIT 유니버스 폴백(고정 유니버스 전환) 알림 — 폴백 진입 시 1회만 발행.

        생존편향 검증 원칙상 PIT 유니버스 이탈은 치명적이므로 warning 으로 알린다.
        _pit_fallback_active 로 dedup 해 한 폴백 구간에 매 틱 재발송하지 않는다.
        """
        if self._pit_fallback_active:
            return
        self._pit_fallback_active = True
        await publish_alert(
            self.redis,
            user_id=self._user_id,
            strategy_id=self.strategy_id,
            severity="warning",
            code="pit_fallback",
            message=(
                f"PIT 유니버스({source}) {cause} — config.universe 고정 폴백으로 전환. "
                f"생존편향 검증 원칙상 지수 구성 이탈은 즉시 점검이 필요합니다."
            ),
        )

    async def _alert_panic_unsupported(self) -> None:
        """패닉 오버레이 미지원으로 매매를 중단했음을 알린다 — 러너 수명 중 1회.

        60초 틱마다 재발행하면 알림이 폭주해 오히려 읽히지 않으므로 _pit_fallback_active
        와 동일한 '전환 시점' dedup 규약을 쓴다(로그도 같은 이유로 1회만 남긴다).
        """
        if self._panic_alerted:
            return
        self._panic_alerted = True
        logger.error(
            "리밸런싱 전략 %d panic_overlay 가 설정돼 있으나 실거래 엔진에 구현이 없어 "
            "매매를 중단한다(보유 유지). 백테스트와 다른 노출로 자금을 움직이지 않기 위한 "
            "의도된 거부다.",
            self.strategy_id,
        )
        await publish_alert(
            self.redis,
            user_id=self._user_id,
            strategy_id=self.strategy_id,
            severity="critical",
            code="panic_overlay_unsupported",
            message=(
                "panic_overlay 는 백테스트에만 구현돼 있어 실거래에서 재현할 수 없습니다. "
                "무시하고 매매하면 base_exposure·event_only 를 반영하지 않아 백테스트보다 "
                "공격적으로 운용되므로, 이 전략의 매매를 중단합니다(기존 보유는 유지, MDD "
                "킬스위치는 계속 동작). 실거래하려면 panic_overlay 를 해제하십시오."
            ),
        )

    async def _seed_history(
        self, universe: list[str], min_bars: int = 0
    ) -> dict[str, pd.Series]:
        """universe 각 종목의 종가 Series 를 시드한다(부족하면 외부 적재).

        :param min_bars: 추가로 보장할 최소 봉 수(동적 유니버스 상대강도 lookback 등).
        """
        selection = self._cfg.get("selection", {})
        lookback = int(selection.get("lookback", 120))
        need = lookback + 1
        # custom 규칙 선정은 룩백이 아니라 규칙 내 최장 지표 기간이 필요 봉 수를 결정한다.
        if selection.get("method") == "custom":
            from app.services.backtest.signals import _custom_min_periods

            rule_cfg = {"entry": selection.get("entry"), "exit": selection.get("exit")}
            need = max(need, _custom_min_periods(rule_cfg))
        need = max(need, int(min_bars))
        end = datetime.now()
        start = end - pd.Timedelta(days=need * 3)  # 거래일 고려 여유

        history: dict[str, pd.Series] = {}
        async with AsyncSessionLocal() as db:
            for i, sym in enumerate(universe, start=1):
                series = await get_close_series(db, sym, start, end)
                if len(series) < need:
                    try:
                        df = await asyncio.to_thread(
                            load_ohlcv, sym, start.date(), end.date()
                        )
                        await upsert_price_ticks(db, sym, df)
                        series = await get_close_series(db, sym, start, end)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("%s 리밸런싱 시드 적재 실패: %s", sym, e)
                history[sym] = series.astype(float)
                # 대량 유니버스(수십~수백종목) 시딩이 오래 걸릴 때 진행 상황을 남겨,
                # "멈춘 것처럼 보이지만 실제로는 순차 처리 중"인지 구분할 수 있게 한다.
                if i % 20 == 0 or i == len(universe):
                    logger.info(
                        "전략 %d 히스토리 시딩 진행: %d/%d", self.strategy_id, i, len(universe),
                    )
        return history

    async def _holdings(self, db, universe: list[str]) -> dict[str, Decimal]:
        """이 전략의 보유 포지션을 dict(symbol→qty) 로 반환(수량>0).

        strategy_id 로 직접 격리하므로 다른 전략의 포지션(같은 종목이라도)은 애초에
        조회되지 않는다. universe 필터는 남겨 두어(이 전략이 과거 유니버스에서 편입했다가
        빠진 종목 등) 관심 범위를 좁힌다. 선정에서 빠진 universe 종목은 목표 0 으로
        평가되어 자연히 청산 대상이 된다.
        """
        if not universe:
            return {}
        rows = await db.scalars(
            select(Position).where(
                Position.user_id == self._user_id,
                Position.strategy_id == self.strategy_id,
                Position.qty > 0,
                Position.symbol.in_(universe),
            )
        )
        return {p.symbol: p.qty for p in rows}

    async def _quotes(self, symbols: set[str]) -> dict[str, Decimal]:
        """대상 종목들의 현재가를 REST 로 조회한다(실패 종목은 제외)."""
        prices: dict[str, Decimal] = {}
        for sym in symbols:
            try:
                quote = await self._broker.get_quote(sym)
                prices[sym] = quote.price
            except Exception as e:  # noqa: BLE001
                logger.warning("%s 현재가 조회 실패 — 이번 리밸런싱에서 제외: %s", sym, e)
        return prices

    async def _execute_orders(
        self,
        orders: list[tuple[str, str, int]],
        prices: dict[str, Decimal],
        positions: dict[str, Decimal],
        bar_ts: str,
        targets: dict[str, float] | None = None,
        risk_off: bool = False,
        liq_kind: str = "regime",
    ) -> None:
        """매도 우선 정렬된 주문 목록을 순차 실행한다(매수는 리스크 검증 후).

        :param targets: 종목→목표비중. 감사 로그 사유(편입 순위·목표비중) 생성에 쓴다.
        :param risk_off: 레짐 위험회피(현금화) 국면 여부(청산 사유 문구 결정).
        :param liq_kind: 청산 사유 종류("regime"/"mdd") — 매도 사유 문구 결정.
        """
        targets = targets or {}
        # 목표비중 내림차순 편입 순위(1위=최대비중). 사유 문장의 "N/M위"에 쓴다.
        ranked = sorted(targets.items(), key=lambda kv: kv[1], reverse=True)
        rank_of = {sym: i for i, (sym, _) in enumerate(ranked, start=1)}
        total_picks = len(ranked)
        current_prices = {s: p for s, p in prices.items()}
        async with AsyncSessionLocal() as db:
            # 매수 신규 진입 차단: 일일 손실 한도 초과 시.
            daily = await risk.check_daily_loss_limit(
                db, self._user_id, self.strategy_id, current_prices
            )
            buys_allowed = daily.approved
            if not buys_allowed:
                logger.info("리밸런싱 매수 차단: %s", daily.reason)

        for sym, side, qty in orders:
            price = prices.get(sym)
            if price is None or qty <= 0:
                continue
            async with _position_lock(self.redis, self._user_id, sym) as acquired:
                if not acquired:
                    logger.info("포지션 락 경합 — %s 이번 리밸런싱 건너뜀", sym)
                    continue
                async with AsyncSessionLocal() as db:
                    if side == "sell":
                        pos = await db.scalar(
                            select(Position).where(
                                Position.user_id == self._user_id,
                                Position.strategy_id == self.strategy_id,
                                Position.symbol == sym,
                            )
                        )
                        held = pos.qty if pos else Decimal("0")
                        sell_qty = min(int(qty), int(held))
                        if sell_qty <= 0:
                            continue
                        # 사후 전략 개선용: 매도 시점의 포지션 손익률을 로그로 남긴다.
                        pnl_txt = ""
                        if pos and pos.avg_price and pos.avg_price > 0:
                            pnl = (price - pos.avg_price) / pos.avg_price
                            logger.info(
                                "리밸런싱 매도 %s x%d @%s — 평단 %s, 손익률 %.2f%%",
                                sym, sell_qty, price, pos.avg_price, float(pnl) * 100,
                            )
                            pnl_txt = f" · 평단 {float(pos.avg_price):,.0f} 대비 손익 {float(pnl) * 100:+.2f}%"
                        reason = (
                            self._sell_reason(sym, targets, risk_off, sell_qty, liq_kind)
                            + pnl_txt
                        )
                        await self._place(db, sym, "sell", sell_qty, price, bar_ts, reason)
                    else:  # buy
                        if not buys_allowed:
                            continue
                        # max_position 한도 존중: evaluate_buy 가 남은 한도로 수량을 캡한다.
                        decision = await risk.evaluate_buy(
                            db, self._user_id, self.strategy_id, sym, price,
                            Decimal(qty) * price,
                        )
                        if not decision.approved:
                            logger.info("리밸런싱 매수 보류 %s: %s", sym, decision.reason)
                            continue
                        logger.info(
                            "리밸런싱 매수 %s x%d @%s", sym, decision.qty, price
                        )
                        weight = targets.get(sym, 0.0)
                        rank = rank_of.get(sym)
                        rank_txt = f"편입 {rank}/{total_picks}위, " if rank else ""
                        reason = (
                            f"리밸런싱 {rank_txt}목표비중 {weight * 100:.1f}% 구성 "
                            f"· {decision.qty:,}주 @ {float(price):,.0f} 매수"
                        )
                        await self._place(db, sym, "buy", decision.qty, price, bar_ts, reason)
