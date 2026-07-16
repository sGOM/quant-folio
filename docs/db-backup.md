# DB 백업 (E-2)

체결 감사 로그(`orders`/`executions`)·전략 config 는 법적·복구 관점 모두에서 **유일본이면
안 된다**. `scripts/backup_db.sh` 로 `pg_dump` 압축 덤프를 만들고 보존 주기를 관리한다.

## 사용법

```bash
# 기본: 저장소 루트의 ./backups 에, 14일 보존
./scripts/backup_db.sh

# 저장 위치·보존기간 변경(예: NAS 마운트, 30일 보존)
BACKUP_DIR=/mnt/nas/quant-backups RETENTION_DAYS=30 ./scripts/backup_db.sh
```

`docker compose`(db 서비스)가 이미 떠 있어야 한다. 스크립트는:
1. `docker compose exec db pg_dump` 로 전체 스키마+데이터를 덤프해 gzip 압축.
2. `BACKUP_DIR`(기본 `./backups`, `.gitignore` 로 커밋 제외)에 타임스탬프 파일명으로 저장.
3. `RETENTION_DAYS`(기본 14일) 초과분을 자동 삭제.

## 야간 크론 등록 (권장)

```cron
# 매일 새벽 3시(서버 로컬 시각), 저장소 루트에서
0 3 * * * cd /path/to/quant && ./scripts/backup_db.sh >> /var/log/quant-backup.log 2>&1
```

## 복구

```bash
gunzip -c backups/quant_20260716_030000.sql.gz | docker compose exec -T db psql -U quant -d quant
```

## 알려진 경고(무해)

`pg_dump` 실행 중 TimescaleDB 내부 카탈로그 테이블(`continuous_agg`)에 대한 순환 FK 경고가
나올 수 있다 — Timescale 확장 자체의 메타데이터 구조 때문이며, 이 프로젝트가 실제로 쓰는
테이블(`price_ticks` 등 하이퍼테이블 데이터 포함)의 백업·복구에는 영향이 없다.

## 오프사이트 보관 권장

`BACKUP_DIR`을 로컬 디스크가 아닌 NAS·클라우드 스토리지 마운트로 지정하거나, 크론 뒤에
`rclone`/`rsync` 등으로 원격 복사를 추가하는 것을 권장한다 — 서버 디스크 자체가 손상되면
로컬 백업만으로는 복구할 수 없다.
