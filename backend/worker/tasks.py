"""배치 작업 태스크 — 일봉 로컬 적재(C-1)·체결 정합 정기 점검(B-2) 등.

Celery 태스크는 동기 함수라, 비동기 DB 세션을 쓰는 적재 로직은 asyncio.run 으로 감싼다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

from worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# 팩터 워밍업(모멘텀·52주고가·변동성)에 필요한 만큼 과거까지 커버한다.
_LOOKBACK_DAYS = 500


async def _ingest_daily_ohlcv_async() -> dict:
    from app.core.database import AsyncSessionLocal
    from app.services.data.ingest import build_universe
    from app.services.data.loader import ensure_ohlcv_coverage

    end = date.today()
    start = end - timedelta(days=_LOOKBACK_DAYS)
    start_dt = datetime.combine(start, time(0, 0), tzinfo=timezone.utc)
    end_dt = datetime.combine(end, time(23, 59, 59), tzinfo=timezone.utc)

    async with AsyncSessionLocal() as db:
        universe = await build_universe(db)
        failed: list[str] = []
        for sym in universe:
            try:
                await ensure_ohlcv_coverage(db, sym, start_dt, end_dt)
            except Exception as e:  # noqa: BLE001
                failed.append(sym)
                logger.warning("일봉 적재 실패(%s): %s", sym, e)

    result = {"universe": len(universe), "ok": len(universe) - len(failed), "failed": failed}
    logger.info("일봉 로컬 적재 완료: %s", result)
    return result


@celery_app.task(name="worker.ingest_daily_ohlcv")
def ingest_daily_ohlcv() -> dict:
    """KOSPI200 + 등록 전략 유니버스의 일봉을 price_ticks 에 증분 적재한다(C-1, 야간 배치).

    각 종목은 로컬에 이미 있는 만큼은 건너뛰고 부족분(신규 종목 전체 또는 최근 며칠)만
    외부 소스(FDR/pykrx)로 보충한다 — 매일 실행해도 가벼운 이유.
    """
    return asyncio.run(_ingest_daily_ohlcv_async())


# 체결 정합(fill quality) 정기 점검(B-2) — 최근 며칠간 창.
_FILL_QUALITY_WINDOW_DAYS = 90


async def _check_fill_quality_drift_async() -> dict:
    from redis.asyncio import Redis
    from sqlalchemy import select

    from app.api.routes.fill_quality import _FILLED_STATUSES, compute_fill_quality_report
    from app.core.config import settings
    from app.core.database import AsyncSessionLocal
    from app.models import Order
    from app.services.backtest.slippage_calibration import propose_slippage_calibration

    d_to = date.today()
    d_from = d_to - timedelta(days=_FILL_QUALITY_WINDOW_DAYS)
    checked: list[tuple[int, int]] = []
    alerted: list[int] = []
    proposed: list[int] = []

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(Order.user_id, Order.strategy_id)
            .where(
                Order.strategy_id.is_not(None),
                Order.status.in_(_FILLED_STATUSES),
                Order.created_at >= datetime.combine(d_from, time(0, 0), tzinfo=timezone.utc),
            )
            .distinct()
        )
        pairs = [(int(u), int(s)) for u, s in rows.all()]

        redis = Redis.from_url(settings.REDIS_URL)
        try:
            from engine.alerts import publish_alert

            for user_id, strategy_id in pairs:
                checked.append((user_id, strategy_id))
                try:
                    report = await compute_fill_quality_report(
                        db, user_id, d_from=d_from, d_to=d_to, strategy_id=strategy_id,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("체결 정합 점검 실패(전략 %s): %s", strategy_id, e)
                    continue

                grades = report.get("grades", {})
                # 표본이 충분해지면 슬리피지 캘리브레이션 제안을 산출한다(자동 반영 X — 사람 승인
                # 대기용). 표본 부족·유의변화 없음이면 None. 알림 문구에 함께 노출한다.
                proposal = propose_slippage_calibration(report)
                prop_txt = (
                    f" 제안 slippage_bps={proposal.proposed_bps}"
                    f"(실측 중앙값 {proposal.observed_median_bps:.1f}bp, 표본 {proposal.sample_size})"
                    if proposal is not None
                    else ""
                )

                # RED 등급 자체가 이미 가정 대비 큰 이탈(M1: 평균>15bp 또는 표준편차>3×가정,
                # M3: 연환산 드래그차>1.5%p/yr — plan 이 말하는 "2배 임계"와 같은 급의 이탈)을
                # 뜻하므로, 별도 배수 계산 없이 RED 를 그대로 외부 알림(B-1 텔레그램) 기준으로 쓴다.
                if grades.get("m1_exec") == "RED" or grades.get("m3_total") == "RED":
                    m1_mean = report.get("m1_exec", {}).get("all", {}).get("mean")
                    assumption = report.get("assumptions", {}).get("backtest_slip_bps")
                    drag = report.get("annualized_drag", {}).get("drag_diff_pct_per_yr")
                    msg = (
                        f"전략 {strategy_id} 체결 정합 이탈 — M1(실행 슬리피지)={grades.get('m1_exec')} "
                        f"M3(총 정합)={grades.get('m3_total')} 실측 {round(m1_mean, 1) if m1_mean is not None else '-'}bp "
                        f"vs 가정 {assumption}bp(최근 {_FILL_QUALITY_WINDOW_DAYS}일"
                        f"{f', 연환산 드래그차 {drag:.2f}%p/yr' if drag is not None else ''})."
                        f"{prop_txt}"
                    )
                    # critical: RED 는 방치하면 실거래-백테스트 성과 추정이 계통적으로 어긋난다는
                    # 뜻이라 앱 미접속 중에도 알아야 한다 — B-1 텔레그램 채널로도 발송된다.
                    await publish_alert(
                        redis, user_id=user_id, strategy_id=strategy_id, severity="critical",
                        message=msg, code="fill_quality_drift",
                    )
                    alerted.append(strategy_id)
                    if proposal is not None:
                        proposed.append(strategy_id)
                elif proposal is not None:
                    # RED 는 아니지만 표본이 충분해 유의미한 캘리브레이션 제안이 나온 경우:
                    # 정합이 위험 수준은 아니므로 warning(앱 내 WS)으로만 승인 대기를 알린다.
                    await publish_alert(
                        redis, user_id=user_id, strategy_id=strategy_id, severity="warning",
                        message=(
                            f"전략 {strategy_id} 슬리피지 캘리브레이션 제안 — "
                            f"현재 {proposal.current_bps:.1f}bp →{prop_txt}. 승인 시 config 에 반영됩니다."
                        ),
                        code="slippage_calibration_proposed",
                    )
                    proposed.append(strategy_id)
        finally:
            await redis.aclose()

    result = {"checked": len(checked), "alerted": alerted, "proposed": proposed}
    logger.info("체결 정합 정기 점검 완료: %s", result)
    return result


@celery_app.task(name="worker.check_fill_quality_drift")
def check_fill_quality_drift() -> dict:
    """실거래-백테스트 체결 정합(P2-3)을 주간 점검해 슬리피지 가정 이탈 시 경보한다(B-2).

    최근 90일 체결이 있는 (사용자, 전략) 쌍마다 fill-quality 리포트를 재계산해, M1/M3
    등급이 RED 이거나 실측 실행 슬리피지가 백테스트 가정의 2배를 넘으면 warning 알림을
    발행한다(critical 이 아니므로 텔레그램 발송은 안 되고 앱 내 WS 알림만 — B-1 참고).
    표본 부족(min_sample 미만)이면 등급이 INSUFFICIENT 라 자연히 알림이 발화하지 않는다.
    """
    return asyncio.run(_check_fill_quality_drift_async())
