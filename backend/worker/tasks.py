"""배치 작업 태스크 — 일봉 로컬 적재(C-1)·체결 정합 정기 점검(B-2)·DB 백업(E-2) 등.

Celery 태스크는 동기 함수라, 비동기 DB 세션을 쓰는 적재 로직은 asyncio.run 으로 감싼다.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import subprocess
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """비동기 태스크 본체를 실행하고 종료 직전 DB 커넥션 풀을 비운다.

    asyncio.run 은 태스크마다 새 이벤트루프를 만들고 닫는데, 전역 SQLAlchemy 엔진의
    풀에는 직전 루프에 묶인 커넥션이 남는다. 다음 태스크(새 루프)가 그 커넥션을
    재사용하면 "Future attached to a different loop" 로 죽는다 — 워커 프로세스에서
    두 번째 이후 실행부터 터지는 잠복 버그. 루프가 닫히기 전에 dispose 로 풀을
    비워 매 실행이 제 루프의 새 커넥션을 열게 한다.
    """

    async def _wrapped():
        try:
            return await coro
        finally:
            from app.core.database import engine

            await engine.dispose()

    return asyncio.run(_wrapped())

# 팩터 워밍업(모멘텀·52주고가·변동성)에 필요한 만큼 과거까지 커버한다.
_LOOKBACK_DAYS = 500

# 야간 일봉 적재 실패 알림 임계(docs/improvements.md §12) — 유니버스 대비 실패 종목
# 비율이 이를 넘으면 백테스트·팩터 계산이 조용히 stale 데이터로 굴러갈 위험이 크다고
# 보고 warning 알림을 발행한다. 소수 종목의 일시적 소스 장애는 흔해 과민 알림을 피하려
# 10%로 잡는다.
_INGEST_FAILURE_ALERT_RATIO = 0.10


async def _ingest_daily_ohlcv_async() -> dict:
    from redis.asyncio import Redis

    from app.core.config import settings
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

    if universe and len(failed) / len(universe) > _INGEST_FAILURE_ALERT_RATIO:
        from engine.alerts import publish_alert

        redis = Redis.from_url(settings.REDIS_URL)
        try:
            await publish_alert(
                redis, user_id=None, strategy_id=0, severity="warning",
                message=(
                    f"야간 일봉 적재 실패율 {len(failed)}/{len(universe)}"
                    f"({len(failed) / len(universe):.0%})가 임계치를 초과했습니다: "
                    f"{', '.join(failed[:10])}{' 외' if len(failed) > 10 else ''}"
                ),
                code="ohlcv_ingest_failure_rate",
                dedup_window_hours=20.0,  # §21: 원인 해소 전까지 매일 배치마다 반복 발행 억제
            )
        finally:
            await redis.aclose()

    return result


@celery_app.task(name="worker.ingest_daily_ohlcv")
def ingest_daily_ohlcv() -> dict:
    """KOSPI200 + 등록 전략 유니버스의 일봉을 price_ticks 에 증분 적재한다(C-1, 야간 배치).

    각 종목은 로컬에 이미 있는 만큼은 건너뛰고 부족분(신규 종목 전체 또는 최근 며칠)만
    외부 소스(FDR/pykrx)로 보충한다 — 매일 실행해도 가벼운 이유.
    """
    return _run_async(_ingest_daily_ohlcv_async())


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

    최근 90일 체결이 있는 (사용자, 전략) 쌍마다 fill-quality 리포트를 재계산해, M1(실행
    슬리피지)·M3(전체 이탈) 등급이 RED 면 warning 알림을 발행한다(critical 이 아니므로
    텔레그램 발송은 안 되고 앱 내 WS 알림만 — B-1 참고). RED 임계 자체는 이 함수가 아니라
    `compute_fill_quality_report`(app/api/routes/fill_quality.py)의 등급 산정 로직이 정한다.
    표본 부족(min_sample 미만)이면 등급이 INSUFFICIENT 라 자연히 알림이 발화하지 않는다.
    """
    return _run_async(_check_fill_quality_drift_async())


async def _snapshot_sector_map_async() -> dict:
    from app.core.config import settings
    from app.core.database import AsyncSessionLocal
    from app.services.data.krx_index import snapshot_sector_map

    async with AsyncSessionLocal() as db:
        try:
            n = await snapshot_sector_map(db)
            await db.commit()
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            # 분기 1회 배치라 조용히 실패하면 최소 3개월간 아무도 모른 채 PIT 업종
            # 데이터가 스테일 상태로 남는다(§20 판정 근거로 쓰인 데이터). 다른 배치
            # 태스크와 동일한 sentinel 관례로 알린다.
            from redis.asyncio import Redis

            from engine.alerts import publish_alert

            redis = Redis.from_url(settings.REDIS_URL)
            try:
                await publish_alert(
                    redis, user_id=None, strategy_id=0, severity="warning",
                    message=f"업종분류 분기 스냅샷 적재 실패: {e}",
                    code="sector_map_outage",
                    dedup_window_hours=24.0,
                )
            finally:
                await redis.aclose()
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
    return _run_async(_snapshot_sector_map_async())


# DB 백업(E-2) — 저장 위치·보존기간. worker 컨테이너에 마운트된 named volume(db_backups)에
# 쓴다(docker-compose.yml 참고). scripts/backup_db.sh(호스트 crontab용)와 별도 구현이지만
# 파일명 규칙(quant_YYYYMMDD_HHMMSS.sql.gz)·보존정책은 그대로 맞춘다.
_BACKUP_DIR = Path("/backups")
_BACKUP_RETENTION_DAYS = 14
# 마지막 성공 백업 시각(ISO) 키는 app.core.channels.BACKUP_LAST_SUCCESS_KEY 공유
# (worker.check_backup_freshness §9·엔진 헬스 API 가 같은 키를 조회).


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


def _upload_backup_to_s3(path: Path) -> str:
    """백업 파일을 S3 호환 스토리지에 업로드한다(§10, opt-in). 반환: 업로드된 키.

    endpoint_url 을 비워두면 AWS S3, 채우면 R2/B2/MinIO 등 S3 호환 스토리지를 겨냥한다.
    boto3 클라이언트는 동기 API 라 이 함수도 동기다 — 호출부(_backup_database_async)는
    이미 pg_dump 도 블로킹으로 직접 호출하는 단일 태스크 전용 이벤트루프라 문제 없다.
    """
    import boto3

    from app.core.config import settings

    client = boto3.client(
        "s3",
        endpoint_url=settings.S3_BACKUP_ENDPOINT_URL or None,
        region_name=settings.S3_BACKUP_REGION,
        aws_access_key_id=settings.S3_BACKUP_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_BACKUP_SECRET_ACCESS_KEY,
    )
    key = f"{settings.S3_BACKUP_PREFIX}{path.name}"
    client.upload_file(str(path), settings.S3_BACKUP_BUCKET, key)
    return key


async def _backup_database_async() -> dict:
    from redis.asyncio import Redis

    from app.core.channels import BACKUP_LAST_SUCCESS_KEY
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
        await redis.set(BACKUP_LAST_SUCCESS_KEY, now_kst().isoformat())

        # 오프사이트 복제(§10, opt-in) — 실패해도 로컬 백업 자체는 이미 성공했으므로
        # critical 이 아니라 warning 으로 알리고 태스크는 정상 종료시킨다.
        s3_key = None
        if settings.has_s3_backup:
            try:
                s3_key = _upload_backup_to_s3(out_path)
                logger.info(
                    "DB 백업 오프사이트 업로드 완료: s3://%s/%s", settings.S3_BACKUP_BUCKET, s3_key
                )
            except Exception as e:  # noqa: BLE001
                logger.error("DB 백업 오프사이트 업로드 실패(로컬 백업은 정상): %s", e)
                await publish_alert(
                    redis, user_id=None, strategy_id=0, severity="warning",
                    message=f"DB 백업 오프사이트 업로드 실패(로컬 백업은 정상): {e}",
                    code="db_backup_s3_upload_failed",
                )

    finally:
        await redis.aclose()

    result = {
        "ok": True, "path": str(out_path), "size_bytes": size, "removed": removed,
        "s3_key": s3_key,
    }
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

    오프사이트 복제(§10, opt-in): settings.has_s3_backup 이면 로컬 백업 성공 후 S3 호환
    스토리지에도 업로드한다(서버 디스크 손상 대비). 업로드 실패는 warning 알림만 발행하고
    태스크는 ok=True 로 정상 종료한다(로컬 백업 자체는 이미 성공했으므로).
    """
    return _run_async(_backup_database_async())


# 백업 신선도 감시(docs/improvements.md §9) — backup:last_success_at 를 아무도 읽지
# 않으면 worker/beat 자체가 죽어 백업이 아예 안 도는 침묵 실패를 아무도 못 알아챈다.
_BACKUP_STALE_AFTER_HOURS = 26  # 03:00 KST 배치 + 1일 여유(하루 지연까진 정상 재시도 허용)


async def _check_backup_freshness_async() -> dict:
    from redis.asyncio import Redis

    from app.core.channels import BACKUP_LAST_SUCCESS_KEY
    from app.core.config import settings
    from app.services.market import now_kst
    from engine.alerts import publish_alert

    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        raw = await redis.get(BACKUP_LAST_SUCCESS_KEY)
        if raw is None:
            age_hours = None
        else:
            last = datetime.fromisoformat(raw)
            age_hours = (now_kst() - last).total_seconds() / 3600

        stale = age_hours is None or age_hours > _BACKUP_STALE_AFTER_HOURS
        if stale:
            reason = (
                "백업 성공 기록이 없습니다(worker/beat 미기동 가능성)"
                if age_hours is None
                else f"마지막 백업 성공 이후 {age_hours:.1f}시간 경과"
            )
            logger.error("DB 백업 신선도 점검 실패: %s", reason)
            await publish_alert(
                redis, user_id=None, strategy_id=0, severity="critical",
                message=f"DB 백업이 최신이 아닙니다: {reason}", code="db_backup_stale",
                dedup_window_hours=20.0,  # §21: 이 태스크는 매일 09:00 1회 실행 — 해소 전까지
                # 하루 한 번씩만 재적재·재발송(20h로 다음 실행까지 겹치지 않으면서 재부팅
                # 등으로 스케줄이 살짝 밀려도 중복 발행되지 않게 24h보다 약간 짧게 잡음).
            )
        return {"ok": not stale, "last_success_at": raw, "age_hours": age_hours}
    finally:
        await redis.aclose()


@celery_app.task(name="worker.check_backup_freshness")
def check_backup_freshness() -> dict:
    """백업 신선도를 점검한다(§9) — backup:last_success_at 이 없거나 26시간 넘게 갱신되지
    않으면 critical 알림을 발행한다. backup_database 태스크 자체의 실패는 이미 그 안에서
    알림을 발행하므로, 이 태스크가 잡는 것은 그보다 상위의 침묵 실패(worker 프로세스 사망,
    beat 스케줄러 중단 등 backup_database 가 아예 실행되지 못하는 경우)다.
    """
    return _run_async(_check_backup_freshness_async())


# alerts 테이블 보존정책(§21) — §17로 모든 publish_alert 가 영속화되기 시작했는데 정리
# 경로가 없어 무한 증식했다. backup_database 의 _BACKUP_RETENTION_DAYS(14일) 정리
# 패턴을 그대로 따르되, 알림은 확인(읽음) 여부에 따라 보존기간을 이원화한다 — 읽지
# 않은 알림은 사용자가 아직 못 본 것일 수 있어 더 길게 보존한다.
_ALERT_RETENTION_READ_DAYS = 90
_ALERT_RETENTION_UNREAD_DAYS = 180


async def _cleanup_old_alerts_async() -> dict:
    from sqlalchemy import delete

    from app.core.config import settings
    from app.core.database import AsyncSessionLocal
    from app.models import Alert

    now = datetime.now(timezone.utc)
    read_cutoff = now - timedelta(days=_ALERT_RETENTION_READ_DAYS)
    unread_cutoff = now - timedelta(days=_ALERT_RETENTION_UNREAD_DAYS)

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                delete(Alert).where(
                    (Alert.is_read.is_(True) & (Alert.created_at < read_cutoff))
                    | (Alert.is_read.is_(False) & (Alert.created_at < unread_cutoff))
                )
            )
            await db.commit()
            deleted = result.rowcount
    except Exception as e:  # noqa: BLE001
        # 매일 재시도되고 실패해도 "alerts 테이블이 조금 더 커지는" 정도라 severity 는
        # warning — 다만 원인이 지속되면 알 수 있게 다른 배치 태스크와 같은 관례로 알린다.
        from redis.asyncio import Redis

        from engine.alerts import publish_alert

        redis = Redis.from_url(settings.REDIS_URL)
        try:
            await publish_alert(
                redis, user_id=None, strategy_id=0, severity="warning",
                message=f"alerts 보존정책 정리 실패: {e}",
                code="alert_cleanup_failed",
                dedup_window_hours=20.0,
            )
        finally:
            await redis.aclose()
        raise

    logger.info("alerts 보존정책 정리 완료: %d건 삭제", deleted)
    return {"deleted": deleted}


@celery_app.task(name="worker.cleanup_old_alerts")
def cleanup_old_alerts() -> dict:
    """오래된 alerts 를 보존정책(§21)에 따라 삭제한다.

    확인(읽음) 처리된 알림은 90일, 미확인 알림은 180일을 넘기면 삭제한다 — 사용자가
    아직 못 봤을 수 있는 미확인 알림을 더 오래 보존한다. §17 도입 이후 정리 경로가
    없어 테이블이 무한 증식하던 것을 해소한다.
    """
    return _run_async(_cleanup_old_alerts_async())


# ─────────────────────────── 경제 뉴스 수집 (ROADMAP M3) ───────────────────────────
async def _ingest_news_async() -> dict:
    from redis.asyncio import Redis

    from app.core.config import settings
    from app.core.database import AsyncSessionLocal
    from app.services.news import FEEDS, ingest_news, purge_old_news

    async with AsyncSessionLocal() as db:
        result = await ingest_news(db)
        result["purged"] = await purge_old_news(db)

    # 전체 피드가 실패하면 수집 파이프라인 자체가 죽은 것 — 완료 기준(ROADMAP M3)에
    # 따라 알림으로 감지한다. 개별 피드의 일시 장애는 흔해 알리지 않는다.
    if FEEDS and result["feeds_ok"] == 0:
        from engine.alerts import publish_alert

        redis = Redis.from_url(settings.REDIS_URL)
        try:
            await publish_alert(
                redis, user_id=None, strategy_id=0, severity="warning",
                message=(
                    "경제 뉴스 수집이 전체 실패했습니다(모든 피드 응답 없음). "
                    "네트워크 또는 피드 URL(app/services/news.py::FEEDS)을 확인하세요."
                ),
                code="news_ingest_failure",
                dedup_window_hours=6.0,  # 시간별 배치라 원인 해소 전 반복 발행 억제
            )
        finally:
            await redis.aclose()

    return result


@celery_app.task(name="worker.ingest_news")
def ingest_news() -> dict:
    """언론사 RSS 에서 경제 기사를 수집해 news_articles 에 적재한다(M3, 시간별 배치).

    url 유니크 기준 멱등이라 재실행에 안전하다. 적재와 함께 보존기간(60일)이 지난
    기사를 정리하고, 전체 피드 실패 시 전역 warning 알림을 발행한다.
    """
    return _run_async(_ingest_news_async())


# ─────────────────────────── 로컬 영구 저장소 야간 선적재 ───────────────────────────
def _snapshot_target_date() -> date:
    """선적재 대상 일자 — 전날(확정된 마지막 날).

    당일분은 장중 값이 계속 바뀌어 확정으로 굳힐 수 없으므로(스토어 설계 §6)
    배치는 전날까지만 다룬다.

    `date.today()` 대신 KST 로 명시 계산한다 — KRX 거래일은 KST 기준인데, 컨테이너
    TZ 는 UTC 이고 celery_app.timezone(="Asia/Seoul")은 beat 스케줄 표시에만
    적용될 뿐 태스크 본문의 `date.today()`에는 영향을 주지 않는다. 현재 beat 시각
    (18:50 KST = 09:50 UTC)에서는 우연히 날짜가 맞아떨어지지만, 스케줄을 KST
    새벽대로 옮기거나 컨테이너 TZ 가 바뀌면 에러 없이 대상일이 하루 어긋나는
    조용한 회귀가 난다.
    """
    from app.services.market import now_kst

    return now_kst().date() - timedelta(days=1)


def _snapshot_steps(ymd: str) -> list[tuple[str, Callable[[], object]]]:
    """선적재 단계 목록 — (이름, 호출) 쌍.

    별도 함수로 뽑은 이유는 테스트가 실제 pykrx 를 부르지 않고 갈아끼우기 위함이다.
    """
    from app.services.metrics.fetch import (
        _fetch_fundamentals,
        _fetch_index_ohlcv,
        _fetch_market_cap,
        _fetch_market_ohlcv_snapshot,
    )

    mkts = ["KOSPI", "KOSDAQ"]
    steps: list[tuple[str, Callable[[], object]]] = [
        ("펀더멘털", lambda: _fetch_fundamentals(ymd, mkts)),
        ("시가총액", lambda: _fetch_market_cap(ymd, mkts)),
    ]
    for mkt in mkts:
        steps.append((f"전종목OHLCV({mkt})", lambda m=mkt: _fetch_market_ohlcv_snapshot(ymd, m)))
    # 1001=KOSPI, 2001=KOSDAQ 대표지수 — 레짐·패닉 지표의 기준선.
    for code in ("1001", "2001"):
        steps.append((f"지수OHLCV({code})", lambda c=code: _fetch_index_ohlcv(ymd, ymd, c)))
    return steps


async def _publish_snapshot_alert(ymd: str, failed_names: list[str], total: int) -> None:
    """스냅샷 선적재 실패율 초과 알림을 발행한다.

    `engine.alerts.publish_alert` 는 Redis 뿐 아니라 전역 `AsyncSessionLocal`(풀링
    엔진)로 dedup 조회·`Alert` 영속화도 한다 — 이 함수를 단순 `asyncio.run` 으로
    감싸면 그 루프에서 연 커넥션이 풀에 반환된 채 루프가 닫혀, 같은 워커 프로세스의
    다음 태스크가 그 커넥션을 재사용하다 "Future attached to a different loop" 로
    죽는다. 그래서 호출부는 반드시 `_run_async`(종료 전 전역 엔진 dispose)를 써야
    한다.
    """
    from redis.asyncio import Redis

    from app.core.config import settings
    from engine.alerts import publish_alert

    redis = Redis.from_url(settings.REDIS_URL)
    try:
        await publish_alert(
            redis, user_id=None, strategy_id=0, severity="warning",
            message=(
                f"야간 스냅샷 선적재 실패율 {len(failed_names)}/{total}"
                f"({len(failed_names) / total:.0%})가 임계치를 초과했습니다: "
                f"{', '.join(failed_names)}"
            ),
            code="snapshot_ingest_failure_rate",
            dedup_window_hours=20.0,  # §21: 원인 해소 전까지 매일 배치마다 반복 발행 억제
        )
    finally:
        await redis.aclose()


@celery_app.task(name="worker.ingest_daily_snapshots")
def ingest_daily_snapshots() -> dict:
    """전날 확정분을 로컬 저장소에 선적재한다.

    온디맨드 write-through 만으로도 저장소는 채워지지만, 그러면 그 날짜를 처음 밟는
    백테스트가 대기 비용을 전부 문다. 배치가 미리 채워두면 장중 조회가 사라진다.

    한 종류가 실패해도 나머지를 계속한다 — 부분 선적재라도 다음 백테스트의 외부
    조회를 그만큼 줄인다. 실패는 집계하고, 실패율이 임계(`_INGEST_FAILURE_ALERT_RATIO`,
    ingest_daily_ohlcv 와 동일 상수 재사용)를 넘으면 warning 알림을 발행한다 —
    §44-1(KRX 로그인 차단)이 재발하면 6단계 전부 실패해도 태스크 자체는 SUCCESS 로
    끝나 아무 알림 없이 넘어갈 수 있기 때문이다.

    단계가 6개뿐이라 형제(ingest_daily_ohlcv, 종목 단위)와 달리 **1건만 실패해도**
    (1/6≈17%) 이 10% 임계를 넘는다 — 여기서는 의도된 동작이다. 종목별 적재에서
    몇 종목 실패는 흔한 잡음이지만, 여기 한 단계는 "하루치 데이터 종류 하나 전체"
    (예: 전체 시장 시가총액)이므로 그 하나의 실패도 무시할 잡음이 아니다.
    """
    from app.services.data.errors import DataSourceError

    target = _snapshot_target_date()
    ymd = target.strftime("%Y%m%d")
    ok = failed = 0
    failed_names: list[str] = []

    for name, call in _snapshot_steps(ymd):
        try:
            call()
            ok += 1
        except DataSourceError as e:
            failed += 1
            failed_names.append(name)
            logger.warning("선적재 실패 [%s] %s: %s", ymd, name, e)
        except Exception:  # noqa: BLE001 - 한 단계 실패가 배치 전체를 멈추면 안 된다
            failed += 1
            failed_names.append(name)
            logger.warning("선적재 실패 [%s] %s", ymd, name, exc_info=True)

    logger.info("일별 스냅샷 선적재 완료 [%s] ok=%d failed=%d", ymd, ok, failed)

    total = ok + failed
    if total and failed / total > _INGEST_FAILURE_ALERT_RATIO:
        try:
            _run_async(_publish_snapshot_alert(ymd, failed_names, total))
        except Exception:  # noqa: BLE001 - 알림 발행 실패가 이미 끝난 선적재 결과를 무효화하면 안 된다
            logger.warning("스냅샷 선적재 실패 알림 발행 중 오류", exc_info=True)

    return {"date": ymd, "ok": ok, "failed": failed}
