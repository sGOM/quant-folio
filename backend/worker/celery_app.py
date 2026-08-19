"""Celery 워커 — 배치/비동기 작업 (데이터 적재, 백테스트 실행 등).

일봉 로컬 적재(C-1, worker.ingest_daily_ohlcv)는 KOSPI200+등록 전략 유니버스를
야간에 price_ticks 로 증분 적재한다(app.services.data.ingest 참고). include= 로 지정해
Celery 가 워커 기동 시 지연 임포트하게 한다(이 모듈이 정의하는 celery_app 을 tasks.py 가
역참조하는 순환 임포트를 피하기 위함 — 즉시 최상위 임포트하면 순환이 생긴다).
"""
from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "quantfolio",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Seoul",
    enable_utc=True,
)

celery_app.conf.beat_schedule = {
    # 장 마감(15:30 KST) 이후, 당일 시세가 확정되는 저녁 시간대에 실행.
    "ingest-daily-ohlcv": {
        "task": "worker.ingest_daily_ohlcv",
        "schedule": crontab(hour=18, minute=30),
    },
    # 로컬 영구 저장소 선적재 — 일봉 적재(18:30) 직후. 온디맨드 write-through 만으로도
    # 저장소는 채워지지만, 그러면 그 날짜를 처음 밟는 백테스트가 대기 비용을 다 문다.
    "ingest-daily-snapshots": {
        "task": "worker.ingest_daily_snapshots",
        "schedule": crontab(hour=18, minute=50),
    },
    # KIS 종목마스터(거래정지·관리종목·액면가·업종분류) 일별 스냅샷 — 일봉 적재
    # (18:30)와 로컬 저장소 선적재(18:50) 사이. 인증·유량제한 없는 시장 전체 zip
    # 다운로드라 다른 배치와 자원 경합이 없다.
    "snapshot-kis-stock-master-nightly": {
        "task": "worker.snapshot_kis_stock_master",
        "schedule": crontab(hour=18, minute=40),
    },
    # 체결 정합 정기 점검(B-2) — 주간(월요일 아침). 슬리피지 실측이 서서히 벌어지는
    # 종류의 이슈라 일 단위로 돌 필요는 없다.
    "check-fill-quality-drift": {
        "task": "worker.check_fill_quality_drift",
        "schedule": crontab(day_of_week="mon", hour=9, minute=0),
    },
    # 업종분류 PIT 스냅샷 적재(C-2 해소) — 분기 1회(1/4/7/10월 1일). 업종 재편 빈도가
    # 낮아 이보다 촘촘한 주기는 불필요. 장 마감 이후 시간대에 실행.
    "snapshot-sector-map-quarterly": {
        "task": "worker.snapshot_sector_map",
        "schedule": crontab(month_of_year="1,4,7,10", day_of_month="1", hour=19, minute=0),
    },
    # DB 백업(E-2) — 매일 새벽 3시(KST). 장중·야간 배치(18:30 적재)와 겹치지 않고, 이용자
    # 활동이 없는 시간대라 pg_dump 부하가 다른 작업과 경합하지 않는다.
    "backup-database-nightly": {
        "task": "worker.backup_database",
        "schedule": crontab(hour=3, minute=0),
    },
    # 백업 신선도 감시(§9) — 야간 백업(03:00) 이후 이용자 활동이 시작되는 아침에 점검.
    # worker/beat 자체가 죽어 backup_database 가 아예 실행되지 못하는 침묵 실패를 잡는다.
    "check-backup-freshness": {
        "task": "worker.check_backup_freshness",
        "schedule": crontab(hour=9, minute=0),
    },
    # 경제 뉴스 수집(ROADMAP M3) — 시간별. RSS 는 최근 기사만 내려주므로 이보다 성기면
    # 기사가 유실되고, url 멱등이라 촘촘해도 무해하다. 다른 배치와 겹치지 않게 분(分)만
    # 비켜 둔다.
    "ingest-news-hourly": {
        "task": "worker.ingest_news",
        "schedule": crontab(minute=10),
    },
    # alerts 테이블 보존정책 정리(§21) — §17 도입 이후 정리 경로가 없어 무한 증식하던
    # 것을 해소. 야간 백업(03:00)과 겹치지 않는 시간대에 실행.
    "cleanup-old-alerts": {
        "task": "worker.cleanup_old_alerts",
        "schedule": crontab(hour=4, minute=0),
    },
}


@celery_app.task(name="worker.ping")
def ping() -> str:
    """워커 헬스체크 태스크. 정상이면 "pong" 을 반환한다."""
    return "pong"
