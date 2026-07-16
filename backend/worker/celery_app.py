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
}


@celery_app.task(name="worker.ping")
def ping() -> str:
    """워커 헬스체크 태스크. 정상이면 "pong" 을 반환한다."""
    return "pong"
