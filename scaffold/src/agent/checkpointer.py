"""Checkpointer selection -- the dev/prod seam.

Async context manager so the Postgres connection pool is opened and closed by
whoever owns the lifecycle (FastAPI lifespan, a test fixture, a notebook cell).
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.base import BaseCheckpointSaver

from agent.settings import Settings


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[BaseCheckpointSaver]:
    if not settings.use_postgres:
        # Dev/test: state lives in the process and dies with it.
        from langgraph.checkpoint.memory import MemorySaver

        yield MemorySaver()
        return

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(settings.postgres_url) as saver:
        # Idempotent DDL. In a real deployment run this as a migration job
        # before rolling pods, not on every container start.
        await saver.setup()
        yield saver
