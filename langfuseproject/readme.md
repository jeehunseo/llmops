# langfuse project

세 개의 독립된 서비스로 구성됩니다. 서로 다른 `docker-compose.yml`로 각각 관리되지만, 공용 네트워크로 서로 통신합니다.

## 0. 공용 네트워크 (최초 1회)

```bash
docker network create llmops-net
```

## 1. service

Django REST API 서버 (`1.service/`)

```bash
cd 1.service
uv sync
docker compose up -d --build
```

## 2. inference

FastAPI(앞단) + Triton Inference Server(추론, vLLM backend, Ministral-3-3B-Instruct) (`2.inference/`)

```bash
cd 2.inference
cp .env.example .env   # HUGGING_FACE_HUB_TOKEN 채워넣기 (Ministral 라이선스 동의 필요)
uv sync
docker compose up -d --build
```

## 3. langfuse

트레이싱 수집/조회 서버 (`langfuse/`). Postgres(트랜잭션 DB) + ClickHouse(트레이스 분석 DB) + Redis(캐시/큐) + MinIO(S3 호환 오브젝트 스토리지, 이벤트/미디어/익스포트 저장) + langfuse-web/worker 로 구성됩니다.

```bash
cd langfuse
cp .env.example .env
# SALT / ENCRYPTION_KEY / NEXTAUTH_SECRET / *_PASSWORD / *_AUTH 값 채워넣기
# (ENCRYPTION_KEY는 openssl rand -hex 32 로 생성)
docker compose up -d
```

`LANGFUSE_INIT_*` 값을 채워두면 최초 기동 시 조직/프로젝트/유저/API 키가 자동 생성되어 로그인 화면의 회원가입 절차를 건너뛸 수 있습니다. 이때 생성되는 `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `LANGFUSE_INIT_PROJECT_SECRET_KEY`를 `1.service`, `2.inference`의 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`에 그대로 넣어주면 됩니다. 비워두면 UI(http://localhost:3001)에서 직접 가입 후 Settings -> API Keys 에서 발급받아 채워넣습니다.

`1.service`, `2.inference`는 표준 OpenTelemetry SDK로 `LANGFUSE_HOST`(`http://langfuse-web:3000`)의 OTLP 엔드포인트(`/api/public/otel/v1/traces`)에 Basic Auth(`LANGFUSE_PUBLIC_KEY:LANGFUSE_SECRET_KEY`)로 트레이스를 전송합니다 (공용 네트워크 `llmops-net` 경유).

Langfuse UI는 `1.service`가 이미 호스트 포트 3000을 쓰고 있어 **3001**번으로 노출됩니다 (`http://localhost:3001`).

## 4. 데이터 보존 기간 설정 (retention)

Langfuse 자체 보존 기능(프로젝트별 설정, ClickHouse 행과 블롭을 함께 삭제)은 **Enterprise 전용**입니다. MIT(OSS) 빌드에서는 데이터가 무한정 쌓이므로, 동일한 효과를 직접 걸어줘야 합니다. Phoenix에서 `PHOENIX_DEFAULT_RETENTION_POLICY_DAYS` 한 줄로 되던 부분이 여기서는 두 군데로 나뉩니다.

```bash
cd langfuse
./setup_retention.sh 180      # 기본값 180일, 인자로 일수 변경 가능
```

스크립트가 하는 일은 두 가지이고, **둘 다 필요합니다**:

1. **ClickHouse TTL** -- 트레이스 데이터가 들어있는 5개 테이블에 TTL을 겁니다.
   - `events_full`, `events_core` (v4에서 실제로 사용하는 테이블), `observations` (`start_time` 기준)
   - `traces`, `scores` (v3 레거시 테이블, 마이그레이션이 계속 생성하므로 함께 처리) (`timestamp` 기준)
2. **MinIO 수명주기 규칙** -- `langfuse` 버킷의 이벤트 블롭을 만료시킵니다. TTL만 걸면 ClickHouse만 비워지고 MinIO는 계속 커집니다.

같은 값으로 다시 실행해도 무방하며(멱등), 다른 일수로 실행하면 정책이 덮어써집니다.

### 동작 방식

TTL은 **한 번 걸어두면 이후 자동 적용**됩니다. 크론이나 배치 작업이 따로 필요 없습니다. 이미 쌓여 있던 데이터에도 소급 적용됩니다.

다만 삭제 시점은 정확히 N일째가 아니라 ClickHouse의 **백그라운드 병합(merge)이 돌 때** 정리되므로, 디스크 사용량은 며칠 시차를 두고 줄어듭니다. 특정 월을 즉시 비워야 하면(디스크가 이미 꽉 찬 경우 등) 파티션을 직접 떨굽니다 -- 전 테이블이 월 단위 파티셔닝입니다:

```bash
docker compose exec clickhouse clickhouse-client \
  --user clickhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "ALTER TABLE default.events_full DROP PARTITION '202601'"
```

`DELETE FROM`은 피하는 편이 좋습니다. ReplacingMergeTree에서는 삭제 표시만 남고 실제 디스크가 바로 회수되지 않아, 자체 호스팅 환경에서 디스크가 차오르는 원인이 됩니다.

### 참고

- 계약상 "N일 후 자동 삭제"가 요구사항인 납품 건이라면, 삭제 누락 시 책임 문제가 생길 수 있으므로 EE 라이선스를 검토하는 편이 안전합니다. 위 방식은 디스크 관리 목적에 적합합니다.
- ClickHouse의 시스템 로그 테이블(`trace_log`, `metric_log` 등)은 Langfuse가 쓰지 않는데도 기본 TTL이 없어 장기 운영 시 커집니다. 필요하면 별도로 비활성화하거나 짧은 TTL을 겁니다.

## 채팅 스트리밍 엔드포인트

`1.service`(Django) → `2.inference`(FastAPI, 채팅 템플릿 적용) → Triton(vLLM, 추론) 순으로 전달되고, 응답은 SSE로 스트리밍됩니다.

```bash
curl -N -X POST http://localhost:3000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"system_prompt": "You are a helpful assistant.", "user_prompt": "hi"}'
```

응답 형식: `data: {"delta": "..."}\n\n` 반복, 마지막에 `data: [DONE]\n\n`.

## phoenixproject와의 차이

- 트레이싱 백엔드: Arize Phoenix(Postgres + phoenix 단일 컨테이너) → Langfuse(Postgres + ClickHouse + Redis + MinIO + web/worker 컨테이너)로 인프라가 더 무겁습니다.
- OTEL 연동 방식: `arize-phoenix-otel`의 `register()` 헬퍼 대신, 표준 `opentelemetry-sdk` + `OTLPSpanExporter`를 직접 구성합니다(`config/otel.py`, `api/otel.py`). 계측 코드(OpenInference span attributes)는 그대로 재사용했습니다 -- Langfuse가 `input.value`/`output.value` 등 OpenInference 컨벤션을 네이티브로 인식합니다.
- 인증: Phoenix의 `PHOENIX_API_KEY`(Bearer) 대신 Langfuse의 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`(Basic Auth) 조합을 씁니다.
- MinIO 이미지는 아카이브된 `minio/minio` 대신 Chainguard가 유지보수하는 `cgr.dev/chainguard/minio`를 사용합니다(Langfuse 공식 compose와 동일).
- 보존 기간: Phoenix는 `PHOENIX_DEFAULT_RETENTION_POLICY_DAYS` 환경변수로 무료 제공되던 기능이, Langfuse에서는 Enterprise 전용입니다. OSS에서는 `langfuse/setup_retention.sh`로 ClickHouse TTL + MinIO 수명주기를 직접 걸어야 합니다(위 "4. 데이터 보존 기간 설정" 참고).
