# DB 백업 (E-2)

체결 감사 로그(`orders`/`executions`)·전략 config 는 법적·복구 관점 모두에서 **유일본이면
안 된다**. `pg_dump` 압축 덤프를 만들고 보존 주기를 관리한다.

## 야간 자동 백업 (Celery beat, 기본)

`worker` 컨테이너가 매일 새벽 3시(KST)에 `worker.backup_database` 태스크를 자동 실행한다
(`backend/worker/celery_app.py` 의 `beat_schedule["backup-database-nightly"]`). 호스트
crontab 등록이 더 이상 필요 없다 — 등록을 잊어서 백업이 조용히 안 도는 문제를 없앤다.

동작:
1. worker 컨테이너 안에서 `pg_dump -h db -U <user> -d <dbname>` 로 db 서비스에 직접 접속해
   전체 스키마+데이터를 덤프하고, `pg_dump | gzip` 을 shell 없이 재현해 스트리밍 압축한다
   (`backend/worker/tasks.py::_run_pg_dump_gzip`). 비밀번호는 `PGPASSWORD` 환경변수로만
   전달되고 커맨드라인 인자·로그에는 노출되지 않는다.
2. named volume `db_backups`(컨테이너 내부 `/backups`)에 `quant_YYYYMMDD_HHMMSS.sql.gz`
   파일명으로 저장한다.
3. 14일(`_BACKUP_RETENTION_DAYS`) 초과 백업을 자동 삭제한다.
4. 성공하면 Redis 키 `backup:last_success_at` 에 마지막 성공 시각(ISO, KST)을 기록한다.
   최신값만 유지(TTL 없음) — 백업 상태를 확인하려면:
   ```bash
   docker compose exec redis redis-cli GET backup:last_success_at
   ```
5. 실패하면 `engine.alerts.publish_alert(severity="critical")` 로 텔레그램 알림을 발행한다
   (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 미설정이면 로그만 남고 외부 발송은 스킵 —
   B-1 참고). 실패한 태스크 자체는 예외를 던지지 않고 `{"ok": False, "error": ...}` 를
   반환한다 — Celery 재시도보다는 다음날 03:00 스케줄에서 재시도되는 편이 안전하다.

Celery 태스크를 수동으로 즉시 실행해보려면:
```bash
docker compose exec worker celery -A worker.celery_app.celery_app call worker.backup_database
```

## 수동 백업 (스크립트, 보조용)

호스트에서 즉시 1회 백업하고 싶을 때(예: 마이그레이션 직전) `scripts/backup_db.sh` 를 쓸 수
있다. 이 스크립트는 `docker compose exec db pg_dump` 로 호스트에서 db 컨테이너에 exec 하는
방식이라 야간 자동 백업(worker 컨테이너 내부 저장, `db_backups` 볼륨)과는 별도 저장 위치
(`./backups`, 호스트 로컬)를 쓴다.

```bash
# 기본: 저장소 루트의 ./backups 에, 14일 보존
./scripts/backup_db.sh

# 저장 위치·보존기간 변경(예: NAS 마운트, 30일 보존)
BACKUP_DIR=/mnt/nas/quant-backups RETENTION_DAYS=30 ./scripts/backup_db.sh
```

## 복구

야간 자동 백업(named volume `db_backups`)에서 복구:
```bash
docker compose exec worker sh -c "gunzip -c /backups/quant_20260716_030000.sql.gz" \
  | docker compose exec -T db psql -U quant -d quant
```

수동 백업(`scripts/backup_db.sh`, `./backups`)에서 복구:
```bash
gunzip -c backups/quant_20260716_030000.sql.gz | docker compose exec -T db psql -U quant -d quant
```

## 알려진 경고(무해)

`pg_dump` 실행 중 TimescaleDB 내부 카탈로그 테이블(`continuous_agg`)에 대한 순환 FK 경고가
나올 수 있다 — Timescale 확장 자체의 메타데이터 구조 때문이며, 이 프로젝트가 실제로 쓰는
테이블(`price_ticks` 등 하이퍼테이블 데이터 포함)의 백업·복구에는 영향이 없다.

## 오프사이트 복제 (S3 호환 스토리지, 기본 비활성)

named volume `db_backups`(야간 자동 백업) · `BACKUP_DIR`(수동 스크립트) 모두 서버 디스크
위에 있다 — 서버 디스크 자체가 손상되면 로컬 백업만으로는 복구할 수 없다. `worker.backup_database`
태스크(`backend/worker/tasks.py::_upload_backup_to_s3`)가 pg_dump 성공 직후 S3 호환
스토리지(AWS S3·Cloudflare R2·Backblaze B2·MinIO 등)에 자동 업로드하는 경로를 opt-in으로
제공한다.

설정(`secrets/s3_backup_access_key_id.txt`·`secrets/s3_backup_secret_access_key.txt` 둘 다
비어 있지 않아야 활성화):

```bash
# 액세스 키 발급 후 시크릿 파일에 기록
echo "<ACCESS_KEY_ID>" > secrets/s3_backup_access_key_id.txt
echo "<SECRET_ACCESS_KEY>" > secrets/s3_backup_secret_access_key.txt
```

버킷·엔드포인트는 `.env`(평문, 비밀 아님)로 설정한다:

```bash
S3_BACKUP_BUCKET=my-quantfolio-backups
# AWS S3 는 비워둔다. R2/B2/MinIO 등은 해당 서비스의 S3 호환 엔드포인트를 넣는다.
S3_BACKUP_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
S3_BACKUP_REGION=auto
S3_BACKUP_PREFIX=quantfolio-db-backups/
```

`docker compose up -d --build worker` 로 재기동하면 그날 새벽 백업부터 로컬 저장 직후
같은 파일을 위 버킷에 업로드한다. 업로드 실패는 로컬 백업 성공을 무효화하지 않는다 —
warning 알림(`db_backup_s3_upload_failed`)만 발행하고 태스크는 정상 종료한다(다음날
재시도). 버킷·자격증명 중 하나라도 비어 있으면 업로드 자체를 건너뛴다(기존 로컬 전용
동작에 영향 없음).
