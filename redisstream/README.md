# FastAPI + Valkey Stream + Sequential Worker

구조:

```text
Client
  |
  v
FastAPI
  |
  | XADD jobs:stream
  v
Valkey Stream
  |
  +-----------------------+
  |                       |
XREADGROUP             XAUTOCLAIM
신규 job               중단된 job
  |                       |
  +-----------+-----------+
              |
              v
       asyncio.Queue(maxsize=1)
              |
              v
         single executor
              |
       +------+------+
       |             |
     success       crash/error
       |             |
      XACK         no XACK
                     |
                     v
                 Pending(PEL)
                     |
              heartbeat 중단
                     |
              CLAIM_IDLE_MS 경과
                     |
                 XAUTOCLAIM
```

## 핵심 동작

- FastAPI는 job을 `jobs:stream`에 등록한다.
- Worker의 consumer는 `XREADGROUP`으로 신규 job을 가져온다.
- Worker의 reclaimer는 `XAUTOCLAIM`으로 오래 idle인 Pending job을 가져온다.
- 둘은 모두 동일한 내부 `asyncio.Queue`로 보낸다.
- executor는 하나뿐이라 worker process 하나당 job 하나만 실행한다.
- `capacity = Semaphore(1)`을 Redis read 전부터 잡으므로 worker가 여러 message를 미리 PEL에 잡아두지 않는다.
- 정상 완료한 경우에만 `XACK`한다.
- 실행 중에는 heartbeat가 같은 consumer로 `XCLAIM JUSTID`를 수행해 PEL idle 시간을 갱신한다.
- worker가 죽으면 heartbeat도 중지되고 `CLAIM_IDLE_MS` 후 다른 worker가 reclaim한다.
- step 완료마다 Redis Hash에 checkpoint를 저장하여 중단 후 다음 step부터 재개한다.

### 재시도 회계는 PEL의 delivery count로 한다

재시도 횟수의 근거는 job Hash가 아니라 `XPENDING`의 delivery count다.

`XAUTOCLAIM`은 이 값을 증가시키고 heartbeat의 `XCLAIM JUSTID`는 증가시키지 않으므로,
값이 곧 "배달된 횟수"가 된다. Hash와 달리 payload가 깨진 message에도 항상 존재하기 때문에,
`job_id`가 없는 message가 재시도 한도에 영원히 닿지 못하는 상황이 생기지 않는다.
Hash의 `retry_count`는 조회용 미러일 뿐 판정에 쓰지 않는다.

### 잘못된 message는 첫 배달에 DLQ로 보낸다

`job_id` 누락, `payload` 파싱 실패처럼 재시도해도 결과가 같은 오류는
실행 "전" 검증 단계(`app/common/messages.py`)에서 걸러 즉시 `jobs:dead`로 보낸다.
`CLAIM_IDLE_MS`를 반복해서 소모하지 않는다.

DLQ 이동 경로는 `job_id`에 의존하지 않는다. `job_id`가 없는 message일수록
DLQ로 보내야 하는데, 이 경로가 `job_id`를 요구하면 그 message는 회수만 반복하게 된다.

### 종료 시 실행 중 job을 완주시킨다 (graceful drain)

SIGTERM을 받으면 4단계로 종료한다.

1. consumer / reclaimer / janitor만 중단해 신규 유입을 막는다.
2. 실행 중 job에 `SHUTDOWN_GRACE_SEC`만큼 완주 시간을 준다. 완주하면 `XACK`되어 회수가 불필요해진다.
3. 유예를 넘기면 executor를 취소한다. `XACK`하지 않으므로 PEL에 남아 회수 대상이 된다.
4. 자기 consumer 등록을 정리한다.

`docker-compose.yml`의 `stop_grace_period`는 `SHUTDOWN_GRACE_SEC`보다 커야 한다.
작으면 drain 도중 SIGKILL된다.

### consumer 등록을 정리한다

worker id에 pid와 uuid가 들어가므로 재시작할 때마다 consumer 등록이 하나씩 쌓인다.

- 정상 종료 시 자기 등록을 삭제한다. **단, PEL이 남아 있으면 삭제하지 않는다.**
  `XGROUP DELCONSUMER`는 그 consumer의 PEL 항목까지 지우므로,
  미완료 message가 있는 상태에서 지우면 그 message는 어떤 worker도 회수할 수 없게 된다.
- 죽은 worker가 남긴 등록은 janitor가 주기적으로 스윕한다.
  pending이 0이고 `CONSUMER_IDLE_MS` 이상 idle인 것만 대상으로 한다.

### stream은 PEL 최소 id 기준으로만 자른다

`XACK`은 stream entry를 지우지 않으므로 stream은 그대로 커진다.
janitor가 `XTRIM MINID`로 자르되, 기준점을 PEL의 최소 id로 잡아
**미완료 message는 절대 자르지 않는다.** `MAXLEN` 근사 트리밍은 처리 중인 message도
잘라낼 수 있어 checkpoint 재개와 상충하므로 쓰지 않는다.

종료 상태(`completed` / `failed`)의 job Hash에는 `JOB_TTL_SEC` TTL을 건다.

> 트리밍을 처음 켜는 시점에 stream이 이미 매우 크면 그 1회는 제거 건수에 비례해 오래 걸린다.
> 필요하면 `TRIM_ENABLED=false`로 두고 별도로 정리한다.

### checkpoint 재개는 best-effort다

`current_step`은 job Hash에 있고, Hash는 eviction이나 TTL로 사라질 수 있다.
사라졌으면 조용히 step 0부터 다시 시작한다. 보장이 아니라 최선 노력으로 취급해야 한다.

## 환경변수

운영 상황에 따라 바뀌어야 하는 값은 모두 env로 노출한다.
`app/common/config.py`의 `Settings`가 유일한 진입점이며, 다른 코드는 `os.environ`을 직접 읽지 않는다.
`.env.example`을 `.env`로 복사해 조정한다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VALKEY_URL` | `redis://localhost:6379/0` | 접속 URL |
| `STREAM_KEY` | `jobs:stream` | job stream 키 |
| `DEAD_STREAM_KEY` | `jobs:dead` | DLQ stream 키 |
| `GROUP_NAME` | `jobs:workers` | consumer group 이름 |
| `CLAIM_IDLE_MS` | `30000` | 이만큼 idle인 PEL 항목을 회수한다. job 실행 시간이 아니라 **worker 사망 감지 시간**으로 잡는다 |
| `HEARTBEAT_INTERVAL_SEC` | `10` | PEL idle 갱신 주기 |
| `RECLAIM_INTERVAL_SEC` | `3` | 회수 대상이 없을 때의 폴링 간격 |
| `READ_BLOCK_MS` | `3000` | `XREADGROUP` 블로킹 시간 |
| `MAX_RETRIES` | `5` | 회수 허용 횟수. 초과 시 DLQ |
| `SHUTDOWN_GRACE_SEC` | `60` | 종료 시 실행 중 job에 줄 완주 시간. **정상 job의 최대 실행 시간에 맞춘다** |
| `CONSUMER_IDLE_MS` | `300000` | 이만큼 idle이고 pending이 0인 consumer 등록을 제거 |
| `JANITOR_INTERVAL_SEC` | `60` | 스윕/트리밍 주기 |
| `JANITOR_LOCK_TTL_SEC` | `55` | janitor 락 TTL |
| `TRIM_ENABLED` | `true` | stream 트리밍 사용 여부 |
| `DEAD_MAXLEN` | `10000` | DLQ 보관 상한 |
| `JOB_TTL_SEC` | `604800` | 종료 상태 job Hash TTL (7일) |
| `MAX_STEPS` | `100` | API가 받는 `steps` 상한 |
| `MAX_STEP_DELAY_SEC` | `3600` | API가 받는 `step_delay_sec` 상한 |
| `LOG_LEVEL` | `INFO` | 로그 레벨 |
| `WORKER_REPLICAS` | `3` | worker 컨테이너 수. 동시 처리 가능한 job 수와 같다 |
| `STOP_GRACE_PERIOD` | `70s` | compose가 SIGKILL까지 기다리는 시간 |

잘못된 조합은 기동 시점에 `ConfigError`로 막는다.

- `HEARTBEAT_INTERVAL_SEC * 2000 <= CLAIM_IDLE_MS`
  — heartbeat가 회수 창 안에 2회 이상 돌지 못하면 정상 실행 중인 job이 회수될 수 있다.
- `CONSUMER_IDLE_MS >= CLAIM_IDLE_MS * 2`
  — 살아 있는 worker의 등록이 스윕에 지워지면 안 된다.
- `JANITOR_LOCK_TTL_SEC < JANITOR_INTERVAL_SEC`
  — 락이 주기보다 오래 살면 janitor가 영영 돌지 못한다.

## 실행

```bash
docker compose up --build
```

API: `http://localhost:8000` / Swagger: `http://localhost:8000/docs`

## Job 등록

```bash
curl -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "demo",
    "steps": 10,
    "step_delay_sec": 3,
    "data": {
      "value": "hello"
    }
  }'
```

## 상태 조회

```bash
curl http://localhost:8000/jobs/<JOB_ID>
```

## worker 장애/reclaim 테스트

긴 job을 하나 넣고 worker를 강제 종료한다.

```bash
curl -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"name": "crash-test", "steps": 20, "step_delay_sec": 3}'
```

```bash
docker compose kill worker
docker compose up -d worker
docker compose logs -f worker
```

`CLAIM_IDLE_MS`가 지난 뒤 Pending job을 reclaim하고 마지막 `current_step` 이후부터 재개한다.
로그의 `resume_from`과 `delivered`로 확인할 수 있다.

## graceful drain 테스트

강제 종료(`kill`)가 아니라 정상 종료(`stop`)를 쓴다.

```bash
curl -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"name": "drain-test", "steps": 12, "step_delay_sec": 1}'

sleep 4
time docker compose stop worker
```

`stop`이 즉시 끝나지 않고 남은 step만큼 기다린 뒤 job이 `completed`가 되어야 한다.

## 잘못된 message 테스트

```bash
docker compose exec valkey valkey-cli XADD jobs:stream '*' payload '{"steps":1}'
docker compose exec valkey valkey-cli XLEN jobs:dead
```

`job_id`가 없으므로 첫 배달에서 곧바로 DLQ로 이동해야 한다.

## Worker 수

worker는 기본 **3개**로 뜬다(`WORKER_REPLICAS`).

```bash
docker compose up --build          # worker 3개
WORKER_REPLICAS=5 docker compose up --build
```

각 worker process는 자기 내부에서는 한 번에 하나만 실행한다.
따라서 **동시 처리량은 곧 replica 수**다. worker가 3개면 시스템 전체에서
최대 3개의 job이 동시에 실행된다.

worker를 늘려도 각 worker가 PEL에 선점하는 message는 1건을 넘지 않는다.
`capacity = Semaphore(1)`을 Redis read 전에 잡기 때문이다.

## 상태 확인

```bash
docker compose exec valkey valkey-cli XPENDING jobs:stream jobs:workers
docker compose exec valkey valkey-cli XPENDING jobs:stream jobs:workers - + 10
docker compose exec valkey valkey-cli XINFO CONSUMERS jobs:stream jobs:workers
docker compose exec valkey valkey-cli XRANGE jobs:stream - +
docker compose exec valkey valkey-cli XRANGE jobs:dead - +
```

`XPENDING ... - + 10`의 마지막 열이 delivery count이며, 재시도 판정의 근거다.

## 테스트

Redis/Valkey stream 의미론(PEL, idle, `XAUTOCLAIM` 경합, delivery count)이 검증 대상이라
mock을 쓰지 않고 실제 Valkey를 대상으로 한다.

```bash
docker run --rm -d --name test-valkey -p 6379:6379 valkey/valkey:9.1.2-alpine
pip install -r requirements-dev.txt
pytest
```

접속 대상은 `TEST_VALKEY_URL`로 바꿀 수 있다(기본 `redis://localhost:6379/15`).

## 중요한 운영상 주의점

Redis/Valkey Stream + Consumer Group은 기본적으로 at-least-once 방식으로 생각해야 한다.

예를 들어 실제 외부 DB 변경은 성공했는데 worker가 checkpoint 또는 XACK 전에 죽으면,
다른 worker가 해당 step을 다시 실행할 수 있다.

따라서 실제 job step은 가능한 한 `job_id + step` 등을 이용해 idempotent하게 구현해야 한다.

예:

```sql
CREATE UNIQUE INDEX uq_job_step
ON processed_job_step(job_id, step);
```
