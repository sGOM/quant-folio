"""worker/tasks.py 의 DB 백업 순수 로직 검증(§신규발굴 9차 1위).

`_backup_database_async`(pg_dump·S3·celery beat 배선)를 이제까지 참조하는
테스트가 전무했다(grep 확인). 그중 외부 I/O(subprocess/boto3) 없이 검증
가능한 순수 함수 두 개 — `_parse_database_url`(DATABASE_URL → pg_dump 접속
정보 파싱)·`_prune_old_backups`(보존기간 계산·삭제 대상 판정) — 를 검증한다.
여기 버그는 "야간 백업이 조용히 잘못된 host/port 로 실패"하거나 "보존기간
계산이 틀려 백업이 너무 일찍 삭제/과다 보존"되는 방식으로 나타나 데이터
손실 리스크와 직결된다. `_run_pg_dump_gzip`/`_upload_backup_to_s3`는
subprocess/boto3 실제 프로세스·네트워크 의존이라 이번 범위에서 제외한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from worker.tasks import _parse_database_url, _prune_old_backups


# ─────────────────────────── _parse_database_url ───────────────────────────
def test_parse_database_url_extracts_asyncpg_connection_info():
    conn = _parse_database_url("postgresql+asyncpg://quant:secret@db:5432/quant")
    assert conn == {
        "user": "quant", "password": "secret", "host": "db",
        "port": "5432", "dbname": "quant",
    }


def test_parse_database_url_defaults_when_fields_missing():
    """user/password/host/port/dbname 이 URL 에 없으면 안전한 기본값으로 폴백한다."""
    conn = _parse_database_url("postgresql+asyncpg://")
    assert conn == {
        "user": "quant", "password": "", "host": "db",
        "port": "5432", "dbname": "quant",
    }


def test_parse_database_url_handles_non_default_port_and_dbname():
    conn = _parse_database_url("postgresql+asyncpg://u:p@myhost:15432/mydb")
    assert conn["host"] == "myhost"
    assert conn["port"] == "15432"
    assert conn["dbname"] == "mydb"


# ─────────────────────────── _prune_old_backups ───────────────────────────
def _touch_backup(dir_, name: str, *, age_days: float) -> None:
    p = dir_ / name
    p.write_bytes(b"x")
    mtime = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
    import os

    os.utime(p, (mtime, mtime))


def test_prune_removes_only_backups_older_than_retention(tmp_path):
    _touch_backup(tmp_path, "quant_20260101_000000.sql.gz", age_days=20)  # 오래됨 → 삭제
    _touch_backup(tmp_path, "quant_20260115_000000.sql.gz", age_days=1)  # 최근 → 보존

    removed = _prune_old_backups(tmp_path, retention_days=14)

    assert removed == ["quant_20260101_000000.sql.gz"]
    remaining = sorted(p.name for p in tmp_path.glob("quant_*.sql.gz"))
    assert remaining == ["quant_20260115_000000.sql.gz"]


def test_prune_ignores_files_not_matching_backup_pattern(tmp_path):
    _touch_backup(tmp_path, "quant_20260101_000000.sql.gz", age_days=20)
    other = tmp_path / "unrelated.txt"
    other.write_bytes(b"x")
    import os

    old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(other, (old, old))

    removed = _prune_old_backups(tmp_path, retention_days=14)

    assert removed == ["quant_20260101_000000.sql.gz"]
    assert other.exists()  # 백업 파일명 패턴이 아니면 건드리지 않는다.


def test_prune_keeps_everything_when_all_within_retention(tmp_path):
    _touch_backup(tmp_path, "quant_20260120_000000.sql.gz", age_days=2)
    _touch_backup(tmp_path, "quant_20260119_000000.sql.gz", age_days=3)

    removed = _prune_old_backups(tmp_path, retention_days=14)

    assert removed == []
    assert len(list(tmp_path.glob("quant_*.sql.gz"))) == 2


def test_prune_empty_directory_returns_empty_list(tmp_path):
    assert _prune_old_backups(tmp_path, retention_days=14) == []
