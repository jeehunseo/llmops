import json
import os
import time
import uuid
from typing import AsyncIterator, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from pydantic import BaseModel

from otel import setup_tracing

tracer = trace.get_tracer(__name__)

TRITON_URL = os.environ.get("TRITON_URL", "http://triton:8000")
MODEL_NAME = os.environ.get("TRITON_MODEL_NAME", "ministral-3b")
MODEL_PATH = os.environ.get("MODEL_PATH")

app = FastAPI(title="inference-api")
setup_tracing(app)

_tokenizer = None


def get_tokenizer():
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer

        _tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    return _tokenizer


class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: int = 256
    temperature: float = 0.7


class GenerateResponse(BaseModel):
    text: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    """OpenAI-compatible model listing, used by clients to test connectivity."""
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": 0,
                "owned_by": "local",
            }
        ],
    }


@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    """Proxies to Triton's vLLM backend.

    `prompt` is sent to the model as-is. For instruct-style responses,
    format it with the model's chat template before calling this endpoint.
    """
    span = trace.get_current_span()
    span.update_name("generate_completion")
    span.set_attributes(
        {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.LLM.value,
            "description": "client -> FastAPI(/generate) -> Triton(/generate): sends raw prompt, returns completed text",
        }
    )
    payload = {
        "text_input": req.prompt,
        "parameters": {
            "stream": False,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        },
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                f"{TRITON_URL}/v2/models/{MODEL_NAME}/generate",
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Triton request failed: {exc}") from exc

    data = resp.json()
    return GenerateResponse(text=data.get("text_output", ""))


async def _stream_triton_deltas(prompt: str, max_tokens: int, temperature: float) -> AsyncIterator[str]:
    """Calls Triton's vLLM generate_stream endpoint and yields plain-text deltas."""
    payload = {
        "text_input": prompt,
        "parameters": {
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
    }
    previous_text = ""
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{TRITON_URL}/v2/models/{MODEL_NAME}/generate_stream",
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if not data_str:
                    continue
                chunk = json.loads(data_str)
                text = chunk.get("text_output", "")
                delta = text[len(previous_text) :] if text.startswith(previous_text) else text
                previous_text = text
                if delta:
                    yield delta


async def _llm_generate(prompt: str, max_tokens: int, temperature: float) -> AsyncIterator[str]:
    """Wraps the Triton call in an OpenInference LLM span so Langfuse renders
    the prompt/response in its Input/Output panel, and yields text deltas."""
    with tracer.start_as_current_span(
        "llm_generate",
        attributes={
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.LLM.value,
            SpanAttributes.INPUT_VALUE: prompt,
            SpanAttributes.LLM_MODEL_NAME: MODEL_NAME,
            SpanAttributes.LLM_INVOCATION_PARAMETERS: json.dumps(
                {"max_tokens": max_tokens, "temperature": temperature}
            ),
            "description": "FastAPI -> Triton(vLLM generate_stream): sends templated prompt, receives generated text deltas",
        },
    ) as span:
        full_text = ""
        try:
            async for delta in _stream_triton_deltas(prompt, max_tokens, temperature):
                full_text += delta
                yield delta
        finally:
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, full_text)


class ChatRequest(BaseModel):
    system_prompt: Optional[str] = None
    user_prompt: str
    max_tokens: int = 512
    temperature: float = 0.7


@app.post("/v1/chat")
async def chat(req: ChatRequest):
    """Formats system/user prompts with the model's chat template and
    streams the response back as Server-Sent Events.

    Each event is `data: {"delta": "..."}\\n\\n`; the stream ends with
    `data: [DONE]\\n\\n`.
    """
    span = trace.get_current_span()
    span.update_name("chat_stream")
    span.set_attributes(
        {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
            "description": "Django(api/v1/chat) -> FastAPI(/v1/chat): receives system_prompt/user_prompt, "
            "applies chat template, calls Triton, streams deltas back as SSE",
        }
    )
    messages = []
    if req.system_prompt:
        messages.append({"role": "system", "content": req.system_prompt})
    messages.append({"role": "user", "content": req.user_prompt})

    tokenizer = get_tokenizer()
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            async for delta in _llm_generate(prompt, req.max_tokens, req.temperature):
                yield f"data: {json.dumps({'delta': delta})}\n\n".encode()
        except httpx.HTTPError as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class OpenAIMessage(BaseModel):
    role: str
    content: str


class OpenAIChatCompletionsRequest(BaseModel):
    model: str = MODEL_NAME
    messages: List[OpenAIMessage]
    stream: bool = False
    max_tokens: int = 512
    temperature: float = 0.7


@app.post("/v1/chat/completions")
async def chat_completions(req: OpenAIChatCompletionsRequest):
    """OpenAI-compatible Chat Completions endpoint, for tools (e.g. Langfuse
    Playground) that speak the OpenAI SDK schema. Routes to the same Triton
    model regardless of the `model` field.
    """
    span = trace.get_current_span()
    span.update_name("openai_chat_completions")
    span.set_attributes(
        {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
            "description": "client(OpenAI SDK-compatible) -> FastAPI(/v1/chat/completions) -> Triton: "
            "converts messages via chat template, responds in OpenAI format",
        }
    )
    tokenizer = get_tokenizer()
    prompt = tokenizer.apply_chat_template(
        [{"role": m.role, "content": m.content} for m in req.messages],
        tokenize=False,
        add_generation_prompt=True,
    )
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if not req.stream:
        text = ""
        async for delta in _llm_generate(prompt, req.max_tokens, req.temperature):
            text += delta
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def event_stream() -> AsyncIterator[bytes]:
        def chunk(delta: dict, finish_reason: Optional[str] = None) -> bytes:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": req.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(payload)}\n\n".encode()

        yield chunk({"role": "assistant"})
        try:
            async for delta in _llm_generate(prompt, req.max_tokens, req.temperature):
                yield chunk({"content": delta})
        except httpx.HTTPError as exc:
            yield chunk({"content": f"[error: {exc}]"})
        yield chunk({}, finish_reason="stop")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
