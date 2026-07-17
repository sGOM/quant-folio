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
}


@celery_app.task(name="worker.ping")
def ping() -> str:
    """워커 헬스체크 태스크. 정상이면 "pong" 을 반환한다."""
    return "pong"
