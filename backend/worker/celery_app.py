"""Celery 워커 — 배치/비동기 작업 (데이터 적재, 백테스트 실행 등).

PRD §7-2 에서 본격 사용. 1단계에서는 뼈대와 헬스체크 태스크만 둔다.
"""
from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "quantfolio",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Seoul",
    enable_utc=True,
)


@celery_app.task(name="worker.ping")
def ping() -> str:
    """워커 헬스체크 태스크. 정상이면 "pong" 을 반환한다."""
    return "pong"
