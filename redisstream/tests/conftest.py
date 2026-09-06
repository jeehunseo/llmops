"""
통합 테스트용 공통 fixture.

이 시스템의 위험은 전부 Redis/Valkey stream 의미론(PEL, idle,
XAUTOCLAIM 경합, delivery count)에 있어 mock으로는 검증 가치가 없다.
따라서 실제 Valkey를 대상으로 한다.

    docker run --rm -p 6379:6379 valkey/valkey:9.1.2-alpine
    pytest

TEST_VALKEY_URL로 접속 대상을 바꿀 수 있다.
"""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
import redis.asyncio as redis

from app.common.config import Settings
from app.common.streams import ensure_consumer_group
from app.worker.main import Worker

TEST_VALKEY_URL = os.getenv("TEST_VALKEY_URL", "redis://localhost:6379/15")


def make_settings(**overrides) -> Settings:
    """테스트용 짧은 타이밍. 키는 매번 격리한다."""
    suffix = uuid.uuid4().hex[:8]
    base = {
        "valkey_url": TEST_VALKEY_URL,
        "stream": f"t:{suffix}:stream",
        "dead_stream": f"t:{suffix}:dead",
        "group": f"t:{suffix}:group",
        "claim_idle_ms": 1_000,
        "heartbeat_interval_sec": 0.4,
        "reclaim_interval_sec": 0.2,
        "read_block_ms": 200,
        "max_retries": 2,
        "shutdown_grace_sec": 10.0,
        "consumer_idle_ms": 2_000,
        "janitor_interval_sec": 2.0,
        "janitor_lock_ttl_sec": 1,
        "dead_maxlen": 100,
        "job_ttl_sec": 60,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@asynccontextmanager
async def redis_for(settings: Settings):
    client = redis.from_url(settings.valkey_url, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        pytest.skip(f"Valkey가 필요하다: {settings.valkey_url}")

    await ensure_consumer_group(client, settings)
    try:
        yield client
    finally:
        job_keys = await client.keys("job:test-*")
        if job_keys:
            await client.delete(*job_keys)
        await client.delete(
            settings.stream, settings.dead_stream, settings.janitor_lock_key
        )
        await client.aclose()


@pytest_asyncio.fixture
async def r(settings: Settings):
    async with redis_for(settings) as client:
        yield client


async def enqueue_job(
    r: redis.Redis,
    settings: Settings,
    *,
    steps: int = 2,
    delay: float = 0.02,
    job_id: str | None = None,
) -> tuple[str, str]:
    """정상 job 하나를 stream에 등록하고 (job_id, message_id)를 돌려준다."""
    job_id = job_id or f"test-{uuid.uuid4().hex[:8]}"

    await r.hset(
        f"job:{job_id}",
        mapping={
            "job_id": job_id,
            "status": "queued",
            "current_step": "0",
            "total_steps": str(steps),
            "retry_count": "0",
        },
    )

    message_id = await r.xadd(
        settings.stream,
        {
            "job_id": job_id,
            "payload": json.dumps(
                {
                    "name": "t",
                    "steps": steps,
                    "step_delay_sec": delay,
                    "data": {},
                }
            ),
        },
    )
    return job_id, message_id


@asynccontextmanager
async def running_worker(
    r: redis.Redis,
    settings: Settings,
    worker_id: str | None = None,
):
    worker = Worker(r, settings, worker_id=worker_id)
    task = asyncio.create_task(worker.run())
    try:
        yield worker
    finally:
        worker.stop_event.set()
        await asyncio.wait_for(task, timeout=settings.shutdown_grace_sec + 10)


async def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """predicate(무인자 코루틴 함수)가 True를 돌려줄 때까지 기다린다."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def job_status(r: redis.Redis, job_id: str) -> str | None:
    return await r.hget(f"job:{job_id}", "status")


async def pending_count(r: redis.Redis, settings: Settings) -> int:
    summary = await r.xpending(settings.stream, settings.group)
    return int(summary["pending"]) if summary else 0


async def dlq_entries(r: redis.Redis, settings: Settings) -> list:
    return await r.xrange(settings.dead_stream, "-", "+")
