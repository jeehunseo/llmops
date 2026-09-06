import redis.asyncio as redis

from app.common.config import Settings


def create_redis(settings: Settings) -> redis.Redis:
    # Valkey는 Redis 프로토콜을 그대로 쓰므로 redis-py를 클라이언트로 사용한다.
    return redis.from_url(
        settings.valkey_url,
        decode_responses=True,
        health_check_interval=30,
    )
