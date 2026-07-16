#!/usr/bin/env bash
# DB 백업 (E-2) — TimescaleDB(quant) 전체를 pg_dump 로 압축 덤프하고 보존 주기를 넘은
# 백업을 정리한다. 체결 감사 로그·전략 config 는 유일본이면 안 되므로(법적·복구 관점) 이
# 스크립트를 야간 크론으로 돌려 최소한의 이중화를 확보한다.
#
# 사용법:
#   ./scripts/backup_db.sh                 # 기본: ./backups 에 저장, 14일 보존
#   BACKUP_DIR=/mnt/nas/quant-backups RETENTION_DAYS=30 ./scripts/backup_db.sh
#
# 크론 등록 예(매일 새벽 3시, 저장소 루트에서):
#   0 3 * * * cd /path/to/quant && ./scripts/backup_db.sh >> /var/log/quant-backup.log 2>&1
#
# 복구 예:
#   gunzip -c backups/quant_20260716_030000.sql.gz | docker compose exec -T db psql -U quant -d quant
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
POSTGRES_USER="${POSTGRES_USER:-quant}"
POSTGRES_DB="${POSTGRES_DB:-quant}"

mkdir -p "$BACKUP_DIR"

ts="$(date +%Y%m%d_%H%M%S)"
out="$BACKUP_DIR/quant_${ts}.sql.gz"
tmp="${out}.tmp"

echo "[backup_db] 덤프 시작 → $out"
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip > "$tmp"
mv "$tmp" "$out"
echo "[backup_db] 완료: $(du -h "$out" | cut -f1)"

echo "[backup_db] 보존 기간(${RETENTION_DAYS}일) 초과 백업 정리"
find "$BACKUP_DIR" -name 'quant_*.sql.gz' -mtime "+${RETENTION_DAYS}" -print -delete
