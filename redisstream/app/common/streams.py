import redis.asyncio as redis
from redis.exceptions import ResponseError

from app.common.config import Settings


async def ensure_consumer_group(r: redis.Redis, settings: Settings) -> None:
    try:
        await r.xgroup_create(
            name=settings.stream,
            groupname=settings.group,
            id="0-0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
