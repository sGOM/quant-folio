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

from app.core.database import AsyncSessionLocal
from app.models import Position
from app.services.data.loader import (
    get_close_series,
    load_ohlcv,
    upsert_price_ticks,
)
from app.services.market import is_business_day, is_market_open, now_kst
from app.services.metrics import (
    _approx_start,
    _fetch_index_ohlcv,
    _last_business_day,
    _ymd,
    compute_universe_scores,
)
from engine import risk
from engine.base_runner import BaseRunner, _position_lock
from engine.rebalance import (
    compute_rebalance_orders,
    compute_target_weights,
    is_rebalance_due,
)

logger = logging.getLogger("engine.rebalance_runner")

_LAST_PREFIX = "rebalance:last:"
_REGIME_PREFIX = "rebalance:regime:"  # 직전 레짐 상태(risk-off 여부) 보관
_MDD_PREFIX = "rebalance:mdd:"  # MDD 킬스위치 상태(고점 HWM·발동 여부·발동일)
_LAST_TTL = 60 * 60 * 24 * 90  # 90일(휴장 등 대비 여유)

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
        """
        now = now_kst()
        rf = self._cfg.get("regime_filter") or {}
        regime_enabled = bool(rf.get("enabled"))
        logger.info(
            "전략 %d 틱 시작 (now=%s, regime_enabled=%s, market_open=%s)",
            self.strategy_id, now.isoformat(), regime_enabled, is_market_open(now),
        )

        # MDD 킬스위치(파국 백스톱) — 레짐/정기 리밸런싱보다 절대 우선(역할 분리: 레짐=추세
        # 오버레이, 킬스위치=고점 대비 낙폭 서킷브레이커). 장중에만 자산가치를 평가·청산한다.
        # 발동/쿨다운 중이면 레짐·cadence 로직을 건너뛰고 즉시 청산·현금 대피만 수행한다.
        risk = self._cfg.get("risk_layer") or {}
        mdd_kill_pct = risk.get("mdd_kill_pct")
        if mdd_kill_pct and is_market_open(now):
            killed = await self._evaluate_mdd_kill(
                float(mdd_kill_pct), int(risk.get("mdd_rearm_days", 20) or 20)
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

        risk_off = False
        reentry = False
        # 레짐 전환 판정은 장중에만(주문 실행 가능 시점). 매 tick 확인해 회복 즉시 반응.
        if regime_enabled and is_market_open(now):
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
        logger.info(
            "전략 %d 발화 판정: last=%s due=%s reentry=%s", self.strategy_id, last, due, reentry,
        )

        # 콜드 스타트 즉시 발화: initial_fill_immediate=true 이고 아직 한 번도 실행한 적이
        # 없으며(last 미기록 & 보유 없음) 장중이면, cadence 발화일/시각을 기다리지 않고 즉시
        # 1회 매수한다. 정규 스케줄을 소비(_set_last)해 이번 주기의 정기 리밸런싱을 대체하고
        # 중복 발화를 막는다. due 가 이미 True 면(발화일 도래) 굳이 부트스트랩할 필요 없다.
        bootstrap = (
            not due
            and bool(self._cfg.get("initial_fill_immediate"))
            and last is None
            and is_market_open(now)
            and not await self._has_holdings()
        )

        if not (due or reentry or bootstrap):
            return

        if due or bootstrap:
            kind = "정기 발화" if due else "콜드스타트 즉시 발화"
        else:
            kind = "레짐 재진입"
        logger.info("리밸런싱 전략 %d %s (%s)", self.strategy_id, kind, now.isoformat())
        # 부트스트랩은 정규 리밸런싱을 대체하므로 "rebal" 태그(재진입만 "regime").
        await self._rebalance_once(
            now, risk_off=False, bar_tag="rebal" if (due or bootstrap) else "regime"
        )
        if due or bootstrap:  # 재진입은 월간 cadence 스케줄을 소비하지 않는다
            await self._set_last(now)

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
        """후보풀(고정/PIT) 종목의 보유 포지션(수량>0)이 하나라도 있는지."""
        pool = await self._resolve_universe(_last_business_day())
        async with AsyncSessionLocal() as db:
            positions = await self._holdings(db, pool)
        return bool(positions)

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
        return f"{_MDD_PREFIX}{self.strategy_id}"

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
        self, hwm: float, killed: bool, kill_date: date | None
    ) -> None:
        state = {
            "hwm": float(hwm),
            "killed": bool(killed),
            "kill_date": kill_date.isoformat() if kill_date else None,
        }
        await self.redis.set(self._mdd_key(), json.dumps(state), ex=_LAST_TTL)

    async def _live_equity(self) -> float:
        """라이브 계좌 자산가치 근사 = 배정자본 + 보유 종목 미실현손익.

        라이브에는 별도의 현금 잔고 원장이 없다(리밸런싱은 config.capital 대비 목표비중으로
        수량을 정한다). 그래서 자산가치를 '배정자본(capital) + 보유 종목의 미실현손익
        (Σ 수량×(현재가−평단))' 으로 근사한다. 이는 다음과 동치다:

            equity ≈ (capital − Σ 수량×평단[투입원가])  +  Σ 수량×현재가[시가평가]
                   = capital + Σ 수량×(현재가 − 평단)

        즉 '미투자 현금분은 capital 잔여로, 투자분은 시가평가로' 본다. 완전 투자 상태에서
        시장 급락 시 미실현손익이 크게 음(−)이 되어 자산가치가 하락하고 HWM 대비 낙폭이
        커지므로 킬스위치가 겨냥하는 파국(시장 붕괴)을 정확히 포착한다.

        한계: 과거 라운드트립에서 확정된 실현손익은 반영되지 않는다(현재 보유 장부의 시가
        평가만 추적). 정밀 회계보다 파국 방어(안전장치)가 우선이라는 과제 원칙에 따른 절충이며,
        시세 조회 실패 종목은 손익 0(평단 평가)으로 보수 처리해 데이터 결손이 오발동을
        유발하지 않게 한다.
        """
        capital = float(self._cfg.get("capital", 10_000_000))
        pool = await self._resolve_universe(_last_business_day())
        async with AsyncSessionLocal() as db:
            rows = await db.scalars(
                select(Position).where(
                    Position.user_id == self._user_id,
                    Position.qty > 0,
                    Position.symbol.in_(pool),
                )
            )
            positions = list(rows)
        if not positions:
            return capital
        prices = await self._quotes({p.symbol for p in positions})
        unrealized = 0.0
        for p in positions:
            price = prices.get(p.symbol)
            if price is None:  # 시세 조회 실패 → 손익 0(평단 평가)으로 보수 처리
                continue
            unrealized += float(p.qty) * (float(price) - float(p.avg_price))
        return capital + unrealized

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
        재진입 자체는 이 함수가 아니라 기존 레짐/cadence 게이팅이 담당한다(역할 분리).
        """
        equity = await self._live_equity()
        today = now_kst().date()
        state = await self._get_mdd_state()
        if state is None:  # 최초/유실 → 현재 자산가치를 고점으로 초기화(안전측: 오발동 방지)
            hwm, killed, kill_date = equity, False, None
        else:
            hwm = float(state["hwm"])
            killed = bool(state.get("killed"))
            kd = state.get("kill_date")
            kill_date = date.fromisoformat(kd) if kd else None

        if equity > hwm:
            hwm = equity
        # 쿨다운 경과 → 재가동(고점 기준선 리셋)
        if killed and kill_date is not None and (
            self._business_days_since(kill_date, today) >= rearm_days
        ):
            killed = False
            kill_date = None
            hwm = equity
            logger.info(
                "리밸런싱 전략 %d MDD 킬스위치 쿨다운 경과 — 재가동(고점 리셋 %.0f)",
                self.strategy_id, hwm,
            )
        # 발동 판정(미발동 상태에서만)
        if not killed and hwm > 0 and equity / hwm - 1.0 <= -mdd_kill_pct:
            killed = True
            kill_date = today
            logger.warning(
                "리밸런싱 전략 %d MDD 킬스위치 발동 — 자산가치 %.0f / 고점 %.0f "
                "(낙폭 %.2f%% ≤ −%.2f%%) → 전량 청산·현금 대피",
                self.strategy_id, equity, hwm,
                (equity / hwm - 1.0) * 100, mdd_kill_pct * 100,
            )

        await self._set_mdd_state(hwm, killed, kill_date)
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
        risk = self._cfg.get("risk_layer") or {}
        want_vol_target = bool(risk.get("target_vol"))
        need_hist = (
            bool(rule.get("type"))
            or method in ("momentum", "custom", "all")
            or weighting == "inverse_vol"
            or want_vol_target
        )
        if need_hist:
            min_bars = int(rule.get("lookback", 0)) + 1 if rule.get("type") else 0
            if weighting == "inverse_vol":
                min_bars = max(min_bars, 253)
            if want_vol_target:
                min_bars = max(min_bars, int(risk.get("vol_lookback", 20) or 20) + 1)
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
            scores = await asyncio.to_thread(
                compute_universe_scores, universe, as_of, factor_weights, neutralize
            )
            logger.info("전략 %d 종합점수 산정 완료: %d종목", self.strategy_id, len(scores))
        targets = compute_target_weights(history, cfg, scores=scores)
        logger.info("전략 %d 목표비중 산정 완료: %d종목", self.strategy_id, len(targets))
        # 리스크 레이어(P1-2): 종목 집중 한도·변동성 타겟팅을 목표비중에 적용(백테스트
        # _targets_at 와 동일 로직 재사용). MDD 킬스위치는 계좌 고점(HWM) 상태 지속이
        # 필요해 _tick_once 상단에서 별도로 평가·청산한다(_evaluate_mdd_kill).
        if risk and targets:
            from app.services.backtest.portfolio import _apply_risk_caps

            targets = _apply_risk_caps(targets, pd.DataFrame(history), risk)
        return pool, targets

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
        레짐 필터가 꺼져 있거나 기준지수 조회에 실패하면 False(투자 유지)를 반환한다.
        새 상태 저장은 호출부(_maybe_rebalance 의 _set_regime)가 담당한다.
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
            return fixed
        if not members:
            logger.warning(
                "전략 %d PIT 유니버스(%s) 비어있음 — config.universe 폴백",
                self.strategy_id, source,
            )
            return fixed

        # 유동성 필터: 시가총액(억 원) 하한 미만 종목 제외(PIT — as_of 시점 시총 기준).
        min_cap = rule.get("min_market_cap")
        if min_cap:
            caps = await asyncio.to_thread(krx_index.market_caps, as_of)
            if caps:  # 조회 성공 시에만 필터(실패 시 원본 유지)
                min_cap_won = int(min_cap) * 10**8
                members = [c for c in members if caps.get(c, 0) >= min_cap_won]
        return members

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
        """universe 종목의 보유 포지션을 dict(symbol→qty) 로 반환(수량>0).

        universe 밖의 보유(다른 전략 포지션 등)는 건드리지 않도록 제외한다.
        선정에서 빠진 universe 종목은 목표 0 으로 평가되어 자연히 청산 대상이 된다.
        """
        if not universe:
            return {}
        rows = await db.scalars(
            select(Position).where(
                Position.user_id == self._user_id,
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
