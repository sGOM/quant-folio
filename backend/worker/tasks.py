"""배치 작업 태스크 — 일봉 로컬 적재(C-1)·체결 정합 정기 점검(B-2)·DB 백업(E-2) 등.

Celery 태스크는 동기 함수라, 비동기 DB 세션을 쓰는 적재 로직은 asyncio.run 으로 감싼다.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import subprocess
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

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


async def _snapshot_sector_map_async() -> dict:
    from app.core.database import AsyncSessionLocal
    from app.services.data.krx_index import snapshot_sector_map

    async with AsyncSessionLocal() as db:
        try:
            n = await snapshot_sector_map(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    result = {"snapshot_symbols": n}
    logger.info("업종분류 분기 스냅샷 적재 완료: %s", result)
    return result


@celery_app.task(name="worker.snapshot_sector_map")
def snapshot_sector_map() -> dict:
    """업종분류 PIT 스냅샷을 분기 1회 적재한다(C-2 해소, docs/improvements.md 참고).

    KRX MDC(app.services.data.krx_index.sector_map)는 '현재' 업종분류만 제공하므로,
    지금부터 주기 적재해 향후 백테스트 구간부터는 point-in-time 매핑을 쓸 수 있게 한다
    (krx_index.sector_map(as_of=...) 가 이 테이블을 먼저 조회). 스냅샷 도입 이전 과거
    구간의 소급 적용은 데이터 소스 부재로 여전히 불가능(문서화된 잔존 한계).
    같은 분기에 이미 스냅샷이 있으면 멱등하게 스킵한다(snapshot_sector_map 내부 정책).
    """
    return asyncio.run(_snapshot_sector_map_async())


# DB 백업(E-2) — 저장 위치·보존기간. worker 컨테이너에 마운트된 named volume(db_backups)에
# 쓴다(docker-compose.yml 참고). scripts/backup_db.sh(호스트 crontab용)와 별도 구현이지만
# 파일명 규칙(quant_YYYYMMDD_HHMMSS.sql.gz)·보존정책은 그대로 맞춘다.
_BACKUP_DIR = Path("/backups")
_BACKUP_RETENTION_DAYS = 14
# 마지막 성공 백업 시각(ISO). TTL 없이 최신값만 유지 — /health 등에서 노출할 수 있게 남겨둔다.
_BACKUP_LAST_SUCCESS_REDIS_KEY = "backup:last_success_at"


def _parse_database_url(url: str) -> dict:
    """asyncpg 형식 DATABASE_URL(postgresql+asyncpg://user:pw@host:port/db)에서 pg_dump 접속 정보를 뽑는다.

    urlparse 는 scheme 접두사(+asyncpg)와 무관하게 user/password/hostname/port/path 를
    그대로 파싱하므로 스킴 자체를 손질할 필요가 없다.
    """
    parsed = urlparse(url)
    return {
        "user": parsed.username or "quant",
        "password": parsed.password or "",
        "host": parsed.hostname or "db",
        "port": str(parsed.port or 5432),
        "dbname": parsed.path.lstrip("/") or "quant",
    }


def _run_pg_dump_gzip(conn: dict, out_path: Path) -> int:
    """pg_dump 를 실행해 stdout 을 gzip 압축하며 out_path 에 스트리밍 저장하고, 바이트 크기를 반환한다.

    `pg_dump | gzip` 을 shell=True 없이 재현한다 — 비밀번호는 커맨드라인 인자가 아니라
    PGPASSWORD 환경변수로만 전달해 프로세스 목록(ps)·로그에 노출되지 않게 한다.
    """
    env = {**os.environ, "PGPASSWORD": conn["password"]}
    cmd = [
        "pg_dump",
        "-h", conn["host"],
        "-p", conn["port"],
        "-U", conn["user"],
        "-d", conn["dbname"],
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert proc.stdout is not None
    with open(out_path, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb") as gz:
        while True:
            chunk = proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            gz.write(chunk)
    returncode = proc.wait()
    if returncode != 0:
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise RuntimeError(f"pg_dump 종료코드 {returncode}: {stderr.strip()}")
    return out_path.stat().st_size


def _prune_old_backups(backup_dir: Path, retention_days: int) -> list[str]:
    """보존기간(retention_days)을 넘은 백업 파일을 삭제하고 삭제된 파일명 목록을 반환한다."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed: list[str] = []
    for p in backup_dir.glob("quant_*.sql.gz"):
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            p.unlink(missing_ok=True)
            removed.append(p.name)
    return removed


async def _backup_database_async() -> dict:
    from redis.asyncio import Redis

    from app.core.config import settings
    from app.services.market import now_kst
    from engine.alerts import publish_alert

    conn = _parse_database_url(settings.DATABASE_URL)
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = now_kst().strftime("%Y%m%d_%H%M%S")
    out_path = _BACKUP_DIR / f"quant_{ts}.sql.gz"
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    redis = Redis.from_url(settings.REDIS_URL)
    try:
        try:
            size = _run_pg_dump_gzip(conn, tmp_path)
            tmp_path.rename(out_path)
        except Exception as e:  # noqa: BLE001 — 백업 실패는 무인 운영 중 알아야 하는 critical.
            tmp_path.unlink(missing_ok=True)
            logger.error("DB 백업 실패: %s", e)
            # strategy_id 는 이 알림에 의미가 없지만 publish_alert 시그니처가 요구하는 필수값이라
            # 0(전략 무관 sentinel)을 쓴다. user_id=None 이면 WS 전송은 스킵되고 텔레그램만 나간다.
            await publish_alert(
                redis, user_id=None, strategy_id=0, severity="critical",
                message=f"야간 DB 백업 실패: {e}", code="db_backup_failed",
            )
            return {"ok": False, "error": str(e)}

        removed = _prune_old_backups(_BACKUP_DIR, _BACKUP_RETENTION_DAYS)
        await redis.set(_BACKUP_LAST_SUCCESS_REDIS_KEY, now_kst().isoformat())

    finally:
        await redis.aclose()

    result = {"ok": True, "path": str(out_path), "size_bytes": size, "removed": removed}
    logger.info("DB 백업 완료: %s", result)
    return result


@celery_app.task(name="worker.backup_database")
def backup_database() -> dict:
    """DB 전체를 pg_dump+gzip 으로 백업한다(E-2, 야간 배치).

    호스트 crontab(scripts/backup_db.sh) 대신 worker 컨테이너 안에서 pg_dump 로 db 서비스에
    직접 접속해 덤프한다(등록을 잊어도 조용히 안 도는 일이 없게). 성공하면 Redis
    (backup:last_success_at)에 마지막 성공 시각을 남기고, 실패하면 critical 알림을 발행한다
    (텔레그램, docs/db-backup.md 참고). 개별 pg_dump 실패는 예외 전파 대신 결과 dict로
    표현한다 — Celery 재시도보다는 다음날 스케줄에서 재시도되는 편이 안전(중간 상태 tmp 파일
    정리까지 마친 뒤 반환).
    """
    return asyncio.run(_backup_database_async())
