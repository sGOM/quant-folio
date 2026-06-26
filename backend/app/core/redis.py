"""Redis 연결 — pub/sub · 큐 · 분산 락 공용 클라이언트."""
from redis.asyncio import Redis, from_url

from app.core.config import settings

# 프로세스 전역에서 공유하는 Redis 클라이언트.
# decode_responses=True 로 응답을 str 로 받아 호출부에서 디코드 부담을 없앤다.
redis_client: Redis = from_url(
    settings.REDIS_URL,
    encoding="utf-8",
    decode_responses=True,
)


async def get_redis() -> Redis:
    """FastAPI 의존성 주입용 Redis 클라이언트 제공자. :return: 공용 redis_client"""
    return redis_client
