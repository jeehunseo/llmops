"""
worker 통합 테스트.

각 테스트는 수동 검증에서 확인한 시나리오에 대응한다.
정상 동작 4건은 회귀 방지용, 나머지는 수정된 결함의 재발 방지용이다.
"""

import asyncio
import json

import pytest

from app.worker.main import Worker
from tests.conftest import (
    dlq_entries,
    enqueue_job,
    job_status,
    make_settings,
    pending_count,
    redis_for,
    running_worker,
    wait_for,
)


# ----------------------------------------------------------------------
# 회귀 방지: 원래 정상 동작하던 것들
# ----------------------------------------------------------------------


async def test_happy_path_completes_and_acks(r, settings):
    job_id, _ = await enqueue_job(r, settings, steps=2)

    async def done():
        return await job_status(r, job_id) == "completed"

    async with running_worker(r, settings):
        assert await wait_for(done), "job이 완료되지 않았다"

    assert await pending_count(r, settings) == 0
    # 종료 상태의 hash에는 TTL이 걸려야 한다.
    assert await r.ttl(f"job:{job_id}") > 0


async def test_resumes_from_checkpoint(r, settings, caplog):
    caplog.set_level("INFO")
    job_id, _ = await enqueue_job(r, settings, steps=5, delay=0.01)

    # 3번째 step까지 끝난 상태를 만든다.
    await r.hset(f"job:{job_id}", "current_step", "3")

    async def done():
        return await job_status(r, job_id) == "completed"

    async with running_worker(r, settings):
        assert await wait_for(done)

    text = caplog.text
    assert "step=4/5" in text and "step=5/5" in text
    # 이미 끝난 step은 다시 실행하지 않는다.
    assert "step=1/5" not in text
    assert "resume_from=3" in text


async def test_worker_never_holds_more_than_one_pending(r, settings):
    """read 이전에 capacity를 확보하므로 PEL 선점이 1건을 넘지 않는다."""
    for _ in range(3):
        await enqueue_job(r, settings, steps=3, delay=0.15)

    worker_id = "test-capacity-worker"

    async with running_worker(r, settings, worker_id=worker_id):
        for _ in range(40):
            entries = await r.xpending_range(
                name=settings.stream,
                groupname=settings.group,
                min="-",
                max="+",
                count=20,
            )
            mine = [e for e in entries if e["consumer"] == worker_id]
            assert len(mine) <= 1, f"PEL 선점이 1건을 넘었다: {mine}"
            await asyncio.sleep(0.05)


async def test_heartbeat_does_not_inflate_delivery_count(r, settings):
    """
    retry 회계가 delivery count에 의존하므로,
    heartbeat(XCLAIM JUSTID)가 이 값을 올리면 안 된다.
    """
    _, message_id = await enqueue_job(r, settings, steps=1)
    worker = Worker(r, settings, worker_id="hb-worker")

    await r.xreadgroup(
        groupname=settings.group,
        consumername="hb-worker",
        streams={settings.stream: ">"},
        count=1,
    )

    before = await worker.delivery_count(message_id)

    for _ in range(3):
        await r.xclaim(
            name=settings.stream,
            groupname=settings.group,
            consumername="hb-worker",
            min_idle_time=0,
            message_ids=[message_id],
            justid=True,
        )

    assert await worker.delivery_count(message_id) == before


# ----------------------------------------------------------------------
# 결함 1·2: 잘못된 message가 즉시 DLQ로 가야 한다
# ----------------------------------------------------------------------


async def test_message_without_job_id_goes_to_dlq_on_first_delivery(r, settings):
    """
    job_id가 없으면 job hash를 만들 수 없다.
    retry 카운터가 hash에 있으면 이 message는 영원히 한도에 닿지 못한다.
    """
    await r.xadd(
        settings.stream,
        {"payload": json.dumps({"steps": 1, "step_delay_sec": 0})},
    )

    async def in_dlq():
        return len(await dlq_entries(r, settings)) == 1

    async with running_worker(r, settings):
        assert await wait_for(in_dlq), "DLQ로 이동하지 않았다"

    entries = await dlq_entries(r, settings)
    fields = entries[0][1]

    # 재시도 소진이 아니라 첫 배달의 검증 실패로 이동해야 한다.
    assert "invalid message" in fields["reason"]
    assert "job_id" in fields["reason"]
    assert fields["job_id"] == "(unknown)"
    # 원본을 보존한다.
    assert "payload" in json.loads(fields["raw"])

    assert await pending_count(r, settings) == 0


async def test_malformed_payload_goes_to_dlq_on_first_delivery(r, settings):
    job_id = "test-malformed"
    await r.hset(f"job:{job_id}", mapping={"job_id": job_id, "status": "queued"})
    await r.xadd(settings.stream, {"job_id": job_id, "payload": "NOT-JSON"})

    async def in_dlq():
        return len(await dlq_entries(r, settings)) == 1

    async with running_worker(r, settings):
        assert await wait_for(in_dlq)

    fields = (await dlq_entries(r, settings))[0][1]
    assert "invalid message" in fields["reason"]
    assert fields["job_id"] == job_id

    assert await job_status(r, job_id) == "failed"
    assert await pending_count(r, settings) == 0


async def test_retry_exceeded_uses_delivery_count(r, settings):
    """회수 횟수가 한도를 넘으면 DLQ로 간다. 판정 근거는 PEL delivery count다."""
    job_id, message_id = await enqueue_job(r, settings, steps=1)

    # 다른 consumer가 배달받은 뒤 회수가 반복된 상태를 만든다.
    await r.xreadgroup(
        groupname=settings.group,
        consumername="ghost",
        streams={settings.stream: ">"},
        count=1,
    )
    for _ in range(settings.max_retries + 1):
        await r.xclaim(
            name=settings.stream,
            groupname=settings.group,
            consumername="ghost",
            min_idle_time=0,
            message_ids=[message_id],
        )

    worker = Worker(r, settings, worker_id="probe")
    assert await worker.delivery_count(message_id) > settings.max_retries + 1

    async def in_dlq():
        return len(await dlq_entries(r, settings)) == 1

    async with running_worker(r, settings):
        assert await wait_for(in_dlq)

    fields = (await dlq_entries(r, settings))[0][1]
    assert "retry exceeded" in fields["reason"]
    assert await job_status(r, job_id) == "failed"


# ----------------------------------------------------------------------
# 결함 3: graceful drain
# ----------------------------------------------------------------------


async def test_shutdown_drains_running_job(r, settings):
    """SIGTERM 상당의 stop 신호를 받아도 실행 중 job은 완주하고 ACK된다."""
    job_id, _ = await enqueue_job(r, settings, steps=6, delay=0.3)

    worker = Worker(r, settings)
    task = asyncio.create_task(worker.run())

    async def started():
        return await job_status(r, job_id) == "running"

    assert await wait_for(started, timeout=5.0)

    worker.stop_event.set()
    await asyncio.wait_for(task, timeout=settings.shutdown_grace_sec + 10)

    assert await job_status(r, job_id) == "completed"
    assert await pending_count(r, settings) == 0


async def test_drain_timeout_leaves_message_pending():
    """유예 시간을 넘기면 ACK하지 않고 PEL에 남겨 회수 경로로 넘긴다."""
    settings = make_settings(shutdown_grace_sec=0.2)

    async with redis_for(settings) as r:
        job_id, _ = await enqueue_job(r, settings, steps=20, delay=0.5)

        worker = Worker(r, settings)
        task = asyncio.create_task(worker.run())

        async def started():
            return await job_status(r, job_id) == "running"

        assert await wait_for(started, timeout=5.0)

        worker.stop_event.set()
        await asyncio.wait_for(task, timeout=10)

        assert await job_status(r, job_id) == "interrupted"
        assert await pending_count(r, settings) == 1


# ----------------------------------------------------------------------
# 결함 4: consumer 등록 정리
# ----------------------------------------------------------------------


async def test_cleanup_keeps_consumer_that_still_has_pending(r, settings):
    """
    DELCONSUMER는 그 consumer의 PEL 항목까지 지운다.
    미완료 message가 있으면 등록을 남겨야 회수가 가능하다.
    """
    _, message_id = await enqueue_job(r, settings, steps=1)
    worker = Worker(r, settings, worker_id="wid-pending")

    await r.xreadgroup(
        groupname=settings.group,
        consumername="wid-pending",
        streams={settings.stream: ">"},
        count=1,
    )

    await worker.cleanup_own_consumer()
    names = [c["name"] for c in await r.xinfo_consumers(settings.stream, settings.group)]
    assert "wid-pending" in names
    assert await pending_count(r, settings) == 1

    # PEL이 비면 그때 삭제한다.
    await r.xack(settings.stream, settings.group, message_id)
    await worker.cleanup_own_consumer()
    names = [c["name"] for c in await r.xinfo_consumers(settings.stream, settings.group)]
    assert "wid-pending" not in names


async def test_sweep_removes_only_idle_and_empty_consumers(r, settings):
    await r.xgroup_createconsumer(settings.stream, settings.group, "ghost")
    worker = Worker(r, settings, worker_id="alive")

    # 아직 임계 idle에 못 미치면 지우지 않는다.
    assert await worker.sweep_dead_consumers() == 0

    await asyncio.sleep(settings.consumer_idle_ms / 1000 + 0.3)
    assert await worker.sweep_dead_consumers() == 1

    names = [c["name"] for c in await r.xinfo_consumers(settings.stream, settings.group)]
    assert "ghost" not in names


async def test_worker_cleans_up_its_own_consumer_on_shutdown(r, settings):
    """재시작마다 consumer 등록이 쌓이지 않아야 한다."""
    job_id, _ = await enqueue_job(r, settings, steps=1)

    async def done():
        return await job_status(r, job_id) == "completed"

    async with running_worker(r, settings, worker_id="short-lived"):
        assert await wait_for(done)

    names = [c["name"] for c in await r.xinfo_consumers(settings.stream, settings.group)]
    assert "short-lived" not in names


# ----------------------------------------------------------------------
# 결함 5·6: 트리밍과 회수 cursor
# ----------------------------------------------------------------------


async def test_trim_never_removes_pending_entries(r, settings):
    """
    MAXLEN 근사 트리밍과 달리 MINID 방식은 미완료 message를 자르지 않는다.
    pending 항목이 잘리면 그 job은 재개도 회수도 불가능해진다.
    """
    message_ids = []
    for _ in range(3):
        _, mid = await enqueue_job(r, settings, steps=1)
        message_ids.append(mid)

    await r.xreadgroup(
        groupname=settings.group,
        consumername="c1",
        streams={settings.stream: ">"},
        count=3,
    )
    # 앞의 두 건만 완료 처리하고 마지막은 pending으로 남긴다.
    await r.xack(settings.stream, settings.group, message_ids[0], message_ids[1])

    worker = Worker(r, settings, worker_id="trimmer")
    removed = await worker.trim_stream()

    remaining = [entry_id for entry_id, _ in await r.xrange(settings.stream, "-", "+")]
    assert message_ids[2] in remaining, "미완료 message가 트리밍되었다"
    # 완료된 앞쪽 두 건은 실제로 제거되어야 한다.
    assert removed == 2
    assert message_ids[0] not in remaining
    assert message_ids[1] not in remaining


async def test_reclaim_advances_autoclaim_cursor(r, settings):
    """XAUTOCLAIM cursor를 이어 써야 PEL이 커져도 앞에서부터 재스캔하지 않는다."""
    _, message_id = await enqueue_job(r, settings, steps=1, delay=0.01)

    await r.xreadgroup(
        groupname=settings.group,
        consumername="ghost",
        streams={settings.stream: ">"},
        count=1,
    )
    await asyncio.sleep(settings.claim_idle_ms / 1000 + 0.2)

    worker = Worker(r, settings, worker_id="cursor-worker")
    assert worker.claim_cursor == "0-0"

    result = await r.xautoclaim(
        name=settings.stream,
        groupname=settings.group,
        consumername=worker.worker_id,
        min_idle_time=settings.claim_idle_ms,
        start_id=worker.claim_cursor,
        count=1,
    )

    # redis-py 6.4.0은 Redis 7+ 응답을 3요소로 돌려준다.
    assert len(result) >= 2
    worker.claim_cursor = result[0] or "0-0"
    assert [mid for mid, _ in result[1]] == [message_id]


@pytest.mark.parametrize("pending_first", [True, False])
async def test_trim_handles_empty_and_nonempty_pel(r, settings, pending_first):
    """PEL이 비어 있으면 last-delivered-id 기준으로 자른다."""
    _, message_id = await enqueue_job(r, settings, steps=1)
    await r.xreadgroup(
        groupname=settings.group,
        consumername="c1",
        streams={settings.stream: ">"},
        count=1,
    )
    if not pending_first:
        await r.xack(settings.stream, settings.group, message_id)

    worker = Worker(r, settings, worker_id="trimmer")
    # 어느 경우에도 예외 없이 동작해야 한다.
    await worker.trim_stream()

    remaining = [entry_id for entry_id, _ in await r.xrange(settings.stream, "-", "+")]
    if pending_first:
        assert message_id in remaining
