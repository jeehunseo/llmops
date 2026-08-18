# phoenix project

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

## 3. phoenix

트레이싱 수집/조회 서버 (`phoenix/`)

```bash
cd phoenix
cp .env.example .env
docker compose up -d
```

`1.service`, `2.inference`는 `PHOENIX_COLLECTOR_ENDPOINT=http://phoenix:4317`로 이 컨테이너에 트레이스를 전송합니다 (공용 네트워크 `llmops-net` 경유).

## 채팅 스트리밍 엔드포인트

`1.service`(Django) → `2.inference`(FastAPI, 채팅 템플릿 적용) → Triton(vLLM, 추론) 순으로 전달되고, 응답은 SSE로 스트리밍됩니다.

```bash
curl -N -X POST http://localhost:3000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"system_prompt": "You are a helpful assistant.", "user_prompt": "hi"}'
```

응답 형식: `data: {"delta": "..."}\n\n` 반복, 마지막에 `data: [DONE]\n\n`.

