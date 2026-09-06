import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.common.config import Settings
from app.common.log import setup_logging
from app.common.redis_client import create_redis
from app.common.streams import ensure_consumer_group


class JobRequest(BaseModel):
    name: str = Field(min_length=1)
    steps: int = Field(default=5, ge=1)
    step_delay_sec: float = Field(default=2.0, ge=0.0)
    data: dict = Field(default_factory=dict)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    setup_logging(settings.log_level)

    app.state.settings = settings
    app.state.redis = create_redis(settings)

    await app.state.redis.ping()
    await ensure_consumer_group(app.state.redis, settings)

    yield

    await app.state.redis.aclose()


app = FastAPI(
    title="FastAPI + Valkey Stream Jobs",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    await app.state.redis.ping()
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def create_job(body: JobRequest):
    r = app.state.redis
    settings: Settings = app.state.settings

    # 상한은 운영 상황에 따라 달라지므로 env로 조정한다.
    if body.steps > settings.max_steps:
        raise HTTPException(
            status_code=422,
            detail=f"steps는 {settings.max_steps} 이하여야 한다",
        )
    if body.step_delay_sec > settings.max_step_delay_sec:
        raise HTTPException(
            status_code=422,
            detail=f"step_delay_sec는 {settings.max_step_delay_sec} 이하여야 한다",
        )

    job_id = str(uuid.uuid4())
    job_key = f"job:{job_id}"

    payload = body.model_dump()

    await r.hset(
        job_key,
        mapping={
            "job_id": job_id,
            "status": "queued",
            "current_step": "0",
            "total_steps": str(body.steps),
            "retry_count": "0",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        },
    )

    # 트리밍은 worker의 janitor가 PEL 최소 id 기준(XTRIM MINID)으로 수행한다.
    # 여기서 MAXLEN으로 자르면 아직 처리 중인 message가 잘려 나갈 수 있다.
    stream_id = await r.xadd(
        settings.stream,
        {
            "job_id": job_id,
            "payload": json.dumps(payload, ensure_ascii=False),
        },
    )

    await r.hset(job_key, mapping={"stream_id": stream_id})

    return {
        "job_id": job_id,
        "stream_id": stream_id,
        "status": "queued",
    }


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    info = await app.state.redis.hgetall(f"job:{job_id}")
    if not info:
        raise HTTPException(status_code=404, detail="job not found")
    return info
