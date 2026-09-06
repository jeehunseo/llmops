import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import redis.asyncio as redis

from app.common.config import Settings
from app.common.log import setup_logging
from app.common.messages import InvalidMessage, JobMessage
from app.common.redis_client import create_redis
from app.common.streams import ensure_consumer_group

log = logging.getLogger("worker")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass
class QueueItem:
    message_id: str
    data: dict[str, str]
    source: str  # "new" 또는 "reclaimed"


class Worker:
    def __init__(
        self,
        r: redis.Redis,
        settings: Settings,
        worker_id: str | None = None,
    ):
        self.r = r
        self.settings = settings
        self.worker_id = worker_id or make_worker_id()

        # 실제 실행 대기 Queue.
        # worker 하나는 한 번에 정확히 1개만 소유/처리하도록 크기를 1로 둔다.
        self.queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=1)

        # 중요:
        # Queue 크기만 1로 두면 Redis에서 message를 먼저 읽어 PEL에 올린 뒤
        # queue.put()에서 기다릴 수 있다.
        # 그래서 Redis에서 message를 가져오기 "전"에 capacity를 확보한다.
        self.capacity = asyncio.Semaphore(1)

        self.stop_event = asyncio.Event()

        # XAUTOCLAIM 순회 위치. 매번 0-0부터 재스캔하지 않는다.
        self.claim_cursor = "0-0"

        # 실행 중 job의 완료 신호. 종료 시 drain 대기에 쓴다.
        self.current_job_done: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # 수집
    # ------------------------------------------------------------------

    async def consume_new_jobs(self) -> None:
        s = self.settings

        while not self.stop_event.is_set():
            await self.capacity.acquire()
            reserved = True

            try:
                result = await self.r.xreadgroup(
                    groupname=s.group,
                    consumername=self.worker_id,
                    streams={s.stream: ">"},
                    count=1,
                    block=s.read_block_ms,
                )

                if not result:
                    self.capacity.release()
                    reserved = False
                    continue

                _, messages = result[0]
                message_id, data = messages[0]

                await self.queue.put(
                    QueueItem(message_id=message_id, data=data, source="new")
                )
                reserved = False  # executor가 처리 후 capacity를 release

            except asyncio.CancelledError:
                if reserved:
                    self.capacity.release()
                raise
            except Exception as exc:
                log.error("consumer error: %s", exc)
                if reserved:
                    self.capacity.release()
                await asyncio.sleep(1)

    async def reclaim_pending_jobs(self) -> None:
        s = self.settings

        # 시작 직후 consumer와 과도하게 경쟁하지 않도록 짧게 기다린다.
        await asyncio.sleep(0.2)

        while not self.stop_event.is_set():
            reserved = False

            try:
                # 한 worker가 하나만 소유하도록 먼저 capacity 확보.
                await self.capacity.acquire()
                reserved = True

                result = await self.r.xautoclaim(
                    name=s.stream,
                    groupname=s.group,
                    consumername=self.worker_id,
                    min_idle_time=s.claim_idle_ms,
                    start_id=self.claim_cursor,
                    count=1,
                )

                # redis-py 반환: [next_cursor, messages] 또는
                #                [next_cursor, messages, deleted_ids] (Redis 7+)
                # cursor를 이어 쓰지 않으면 PEL이 커질수록 매번 앞에서부터 재스캔한다.
                self.claim_cursor = result[0] or "0-0"
                messages = result[1]

                if len(result) > 2 and result[2]:
                    # stream에서는 사라졌는데 PEL에 남아 있던 항목.
                    # MINID 트리밍을 쓰면 발생하지 않아야 한다.
                    # 발생했다면 job 유실이므로 반드시 기록을 남긴다.
                    log.warning("PEL 항목이 stream에서 소실됨: %s", result[2])

                if not messages:
                    self.capacity.release()
                    reserved = False
                    await asyncio.sleep(s.reclaim_interval_sec)
                    continue

                message_id, data = messages[0]
                log.info("reclaimed message=%s", message_id)

                await self.queue.put(
                    QueueItem(message_id=message_id, data=data, source="reclaimed")
                )
                reserved = False  # executor가 release

            except asyncio.CancelledError:
                if reserved:
                    self.capacity.release()
                raise
            except Exception as exc:
                log.error("reclaim error: %s", exc)
                if reserved:
                    self.capacity.release()
                await asyncio.sleep(s.reclaim_interval_sec)

    # ------------------------------------------------------------------
    # 실행
    # ------------------------------------------------------------------

    async def heartbeat(self, message_id: str) -> None:
        """
        실행 중인 message의 PEL idle 시간을 주기적으로 갱신한다.

        작업이 claim_idle_ms보다 오래 걸려도 정상 실행 중인 message가
        다른 worker에게 XAUTOCLAIM되는 것을 방지한다.

        XCLAIM JUSTID는 delivery count를 올리지 않으므로
        retry 회계(delivery_count)를 오염시키지 않는다.

        worker가 죽으면 heartbeat도 멈추므로 claim_idle_ms 후 다른 worker가 회수한다.
        """
        s = self.settings

        while True:
            await asyncio.sleep(s.heartbeat_interval_sec)

            try:
                await self.r.xclaim(
                    name=s.stream,
                    groupname=s.group,
                    consumername=self.worker_id,
                    min_idle_time=0,
                    message_ids=[message_id],
                    justid=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("heartbeat error message=%s: %s", message_id, exc)

    async def delivery_count(self, message_id: str) -> int:
        """
        PEL의 delivery count를 retry의 source of truth로 쓴다.

        XAUTOCLAIM이 이 값을 증가시키고 heartbeat의 XCLAIM JUSTID는
        증가시키지 않으므로 값이 곧 "배달된 횟수"가 된다.

        job hash와 달리 payload가 깨진 message에도 항상 존재하므로,
        잘못된 message가 재시도 한도에 영원히 닿지 못하는 문제가 생기지 않는다.
        """
        s = self.settings

        entries = await self.r.xpending_range(
            name=s.stream,
            groupname=s.group,
            min=message_id,
            max=message_id,
            count=1,
        )
        if not entries:
            return 0
        return int(entries[0]["times_delivered"])

    async def execute_job(self, item: QueueItem) -> None:
        s = self.settings
        message_id = item.message_id

        # 1) job hash를 건드리기 전에 배달 횟수부터 확인한다.
        #    첫 배달이 1이므로 허용 회수 횟수는 max_retries + 1.
        delivered = await self.delivery_count(message_id)
        if delivered > s.max_retries + 1:
            await self.move_to_dead_letter(
                item, reason=f"retry exceeded: delivered={delivered}"
            )
            return

        # 2) 형식 오류는 재시도해도 결과가 같으므로 즉시 DLQ로 보낸다.
        #    회수를 반복하며 claim_idle_ms를 낭비하지 않는다.
        try:
            msg = JobMessage.parse(item.data)
        except InvalidMessage as exc:
            await self.move_to_dead_letter(item, reason=f"invalid message: {exc}")
            return

        job_key = f"job:{msg.job_id}"

        # checkpoint 재개는 best-effort다.
        # hash가 eviction이나 TTL로 사라졌으면 0부터 다시 시작한다.
        current_step = int((await self.r.hget(job_key, "current_step")) or "0")

        await self.r.hset(
            job_key,
            mapping={
                "status": "running",
                "worker_id": self.worker_id,
                "source": item.source,
                # 조회용 미러. 재시도 판정에는 쓰지 않는다.
                "retry_count": str(max(delivered - 1, 0)),
                "started_at": utc_now(),
                "updated_at": utc_now(),
            },
        )

        log.info(
            "START job=%s message=%s source=%s resume_from=%s delivered=%s",
            msg.job_id,
            message_id,
            item.source,
            current_step,
            delivered,
        )

        # 데모에서는 step 단위 checkpoint를 둔다.
        # 실제 업무에서는 각 step이 job_id + step 기준으로 멱등해야 한다.
        for step in range(current_step + 1, msg.steps + 1):
            log.info("job=%s step=%s/%s", msg.job_id, step, msg.steps)

            # 실제 작업으로 교체:
            # await call_external_api(...)
            # await process_file(...)
            # await update_database(...)
            await asyncio.sleep(msg.step_delay_sec)

            # step 완료 후 checkpoint
            await self.r.hset(
                job_key,
                mapping={"current_step": str(step), "updated_at": utc_now()},
            )

        # 상태 저장 후 ACK.
        await self.r.hset(
            job_key,
            mapping={
                "status": "completed",
                "completed_at": utc_now(),
                "updated_at": utc_now(),
            },
        )

        # 종료 상태의 job hash는 무한 보관하지 않는다.
        await self.r.expire(job_key, s.job_ttl_sec)

        await self.r.xack(s.stream, s.group, message_id)

        log.info("DONE job=%s message=%s", msg.job_id, message_id)

    async def move_to_dead_letter(self, item: QueueItem, reason: str) -> None:
        """
        message를 DLQ로 옮기고 원본을 ACK한다.

        이 경로는 job_id에 의존하면 안 된다.
        job_id가 없는 message일수록 DLQ로 보내야 하는데,
        여기서 job_id를 요구하면 그 message는 영원히 회수만 반복하게 된다.
        """
        s = self.settings
        job_id = item.data.get("job_id") or ""

        await self.r.xadd(
            s.dead_stream,
            {
                "original_stream": s.stream,
                "original_message_id": item.message_id,
                "job_id": job_id or "(unknown)",
                # payload 키 자체가 없는 message도 있으므로 원본 전체를 보존한다.
                "raw": json.dumps(item.data, ensure_ascii=False),
                "reason": reason,
                "failed_at": utc_now(),
            },
            maxlen=s.dead_maxlen,
            approximate=True,
        )

        if job_id:
            job_key = f"job:{job_id}"
            await self.r.hset(
                job_key,
                mapping={
                    "status": "failed",
                    "error": reason,
                    "updated_at": utc_now(),
                },
            )
            await self.r.expire(job_key, s.job_ttl_sec)

        # DLQ 기록이 끝난 뒤 원래 message를 ACK.
        await self.r.xack(s.stream, s.group, item.message_id)

        log.warning(
            "DEAD job=%s message=%s reason=%s",
            job_id or "(unknown)",
            item.message_id,
            reason,
        )

    async def _mark_interrupted(self, item: QueueItem, exc: Exception | None) -> None:
        job_id = item.data.get("job_id")
        if not job_id:
            return

        mapping = {"status": "interrupted", "updated_at": utc_now()}
        if exc is not None:
            mapping["error"] = str(exc)

        with contextlib.suppress(Exception):
            await self.r.hset(f"job:{job_id}", mapping=mapping)

    async def _next_item(self) -> QueueItem | None:
        """
        queue.get()이 stop_event에도 깨어나게 한다.

        기존에는 stop_event가 set돼도 executor가 queue.get()에서 계속 대기해
        종료 시 task를 통째로 cancel하는 수밖에 없었다.
        """
        getter = asyncio.ensure_future(self.queue.get())
        stopper = asyncio.ensure_future(self.stop_event.wait())

        done, _ = await asyncio.wait(
            {getter, stopper}, return_when=asyncio.FIRST_COMPLETED
        )

        if getter in done:
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
            return getter.result()

        getter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await getter

        # stop 직전에 producer가 넣어둔 item이 있으면 그것까지는 처리한다.
        try:
            return self.queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def executor(self) -> None:
        while True:
            item = await self._next_item()
            if item is None:
                return

            done = asyncio.Event()
            self.current_job_done = done
            heartbeat_task = asyncio.create_task(self.heartbeat(item.message_id))

            try:
                await self.execute_job(item)

            except asyncio.CancelledError:
                # ACK하지 않는다.
                # process가 다시 올라오거나 다른 worker가 claim_idle_ms 이후 회수한다.
                await self._mark_interrupted(item, None)
                log.warning("INTERRUPTED message=%s", item.message_id)
                raise

            except Exception as exc:
                # ACK하지 않는다. PEL에 남겨 회수 대상으로 만든다.
                # delivery count가 한도를 넘으면 다음 회수 때 DLQ로 간다.
                await self._mark_interrupted(item, exc)
                log.error("JOB ERROR message=%s: %s", item.message_id, exc)

            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

                self.queue.task_done()
                self.capacity.release()
                done.set()

    # ------------------------------------------------------------------
    # janitor
    # ------------------------------------------------------------------

    async def cleanup_own_consumer(self) -> None:
        """
        자기 consumer 등록을 지운다.

        PEL이 남아 있으면 지우지 않는다.
        DELCONSUMER는 해당 consumer의 PEL 항목까지 삭제하므로,
        미완료 message가 있는 상태에서 지우면 그 message는
        어떤 worker도 회수할 수 없게 된다.
        """
        s = self.settings

        try:
            pending = await self.r.xpending_range(
                name=s.stream,
                groupname=s.group,
                min="-",
                max="+",
                count=1,
                consumername=self.worker_id,
            )
            if pending:
                log.info("consumer 등록 유지: PEL 잔류 %s건", len(pending))
                return

            await self.r.xgroup_delconsumer(s.stream, s.group, self.worker_id)
            log.info("consumer 등록 삭제: %s", self.worker_id)
        except Exception as exc:
            log.warning("consumer cleanup 실패: %s", exc)

    async def sweep_dead_consumers(self) -> int:
        """
        죽은 worker가 남긴 consumer 등록을 제거한다.

        worker id에 pid와 uuid가 들어가므로 재시작할 때마다 등록이 하나씩
        쌓인다. pending이 0이고 충분히 오래 idle인 것만 지운다.
        """
        s = self.settings
        removed = 0

        for c in await self.r.xinfo_consumers(s.stream, s.group):
            name = c["name"]
            if name == self.worker_id:
                continue
            if int(c["pending"]) == 0 and int(c["idle"]) > s.consumer_idle_ms:
                await self.r.xgroup_delconsumer(s.stream, s.group, name)
                removed += 1
                log.info("죽은 consumer 삭제: %s (idle=%sms)", name, c["idle"])

        return removed

    async def trim_stream(self) -> int:
        """
        PEL 최소 id 이전만 잘라낸다.

        MAXLEN 근사 트리밍과 달리 미완료 message를 절대 자르지 않는다.
        pending 항목이 잘리면 그 job은 재개도 회수도 불가능해지므로
        checkpoint 재개를 유지하는 이상 MINID 방식이어야 한다.
        """
        s = self.settings

        summary = await self.r.xpending(s.stream, s.group)

        if summary and summary.get("pending"):
            safe_min = summary["min"]
        else:
            groups = await self.r.xinfo_groups(s.stream)
            safe_min = next(
                (g["last-delivered-id"] for g in groups if g["name"] == s.group),
                None,
            )

        if not safe_min or safe_min == "0-0":
            return 0

        # approximate=True(`~`)는 radix 노드가 찰 때까지 아무것도 자르지 않아
        # 소규모 stream에서는 무동작이 된다. janitor는 주기와 락으로 이미
        # 속도가 제한되므로 정확 트리밍을 쓴다.
        # 단, 트리밍을 처음 켜는 시점에 stream이 이미 매우 크면
        # 그 1회는 제거 건수에 비례해 오래 걸린다.
        removed = await self.r.xtrim(s.stream, minid=safe_min, approximate=False)
        if removed:
            log.info("stream trim: %s건 제거 (minid=%s)", removed, safe_min)
        return removed

    async def janitor(self) -> None:
        """
        consumer 스윕과 stream 트리밍을 주기적으로 수행한다.
        여러 worker가 동시에 돌지 않도록 짧은 TTL 락을 잡는다.
        """
        s = self.settings

        while not self.stop_event.is_set():
            await asyncio.sleep(s.janitor_interval_sec)
            if self.stop_event.is_set():
                return

            try:
                acquired = await self.r.set(
                    s.janitor_lock_key,
                    self.worker_id,
                    nx=True,
                    ex=s.janitor_lock_ttl_sec,
                )
                if not acquired:
                    continue

                await self.sweep_dead_consumers()
                if s.trim_enabled:
                    await self.trim_stream()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("janitor error: %s", exc)

    # ------------------------------------------------------------------
    # 수명주기
    # ------------------------------------------------------------------

    async def run(self) -> None:
        s = self.settings
        await ensure_consumer_group(self.r, s)

        consumer = asyncio.create_task(self.consume_new_jobs(), name="consumer")
        reclaimer = asyncio.create_task(self.reclaim_pending_jobs(), name="reclaimer")
        janitor = asyncio.create_task(self.janitor(), name="janitor")
        executor = asyncio.create_task(self.executor(), name="executor")

        log.info("worker started id=%s", self.worker_id)

        try:
            await self.stop_event.wait()
        finally:
            log.info("worker stopping")

            # 1단계: 신규 유입만 차단한다. 실행 중 job은 건드리지 않는다.
            for task in (consumer, reclaimer, janitor):
                task.cancel()
            await asyncio.gather(
                consumer, reclaimer, janitor, return_exceptions=True
            )

            # 2단계: 실행 중 job에 유예를 준다.
            #        여기서 완주하면 XACK되므로 회수 자체가 불필요해진다.
            done = self.current_job_done
            if done is not None and not done.is_set():
                log.info("draining current job (grace=%ss)", s.shutdown_grace_sec)
                try:
                    await asyncio.wait_for(done.wait(), timeout=s.shutdown_grace_sec)
                    log.info("drain 완료")
                except asyncio.TimeoutError:
                    log.warning("drain 시간 초과. PEL에 남겨 회수로 처리한다")

            # 3단계: executor 종료.
            executor.cancel()
            await asyncio.gather(executor, return_exceptions=True)

            # 4단계: PEL이 비었을 때만 consumer 등록을 정리한다.
            await self.cleanup_own_consumer()

            log.info("worker stopped id=%s", self.worker_id)


async def async_main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level)

    r = create_redis(settings)
    worker = Worker(r, settings)

    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop_event.set)

    try:
        await r.ping()
        await worker.run()
    finally:
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(async_main())
