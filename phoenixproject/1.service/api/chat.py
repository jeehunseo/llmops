import json
import os

import requests
from django.http import StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import context as otel_context
from opentelemetry import trace

INFERENCE_API_URL = os.environ.get("INFERENCE_API_URL", "http://inference-api:8080")


@csrf_exempt
@require_POST
def chat(request):
    span = trace.get_current_span()
    span.update_name("chat_relay")
    span.set_attributes(
        {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
            "description": "client -> Django(/api/v1/chat) -> FastAPI(inference-api/v1/chat): "
            "forwards system_prompt/user_prompt",
        }
    )
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return StreamingHttpResponse(
            iter([b'data: {"error": "invalid JSON body"}\n\n']),
            content_type="text/event-stream",
            status=400,
        )

    if not body.get("user_prompt"):
        return StreamingHttpResponse(
            iter([b'data: {"error": "user_prompt is required"}\n\n']),
            content_type="text/event-stream",
            status=400,
        )

    payload = {
        "system_prompt": body.get("system_prompt"),
        "user_prompt": body["user_prompt"],
        "max_tokens": body.get("max_tokens", 512),
        "temperature": body.get("temperature", 0.7),
    }

    # Captured now, while Django's request-handling span is still the active
    # context. The actual HTTP call happens later, lazily, as the streaming
    # response body is iterated -- by then the span context is gone, so it
    # must be re-attached inside the generator for RequestsInstrumentor to
    # pick up the right trace/parent when it injects its own traceparent.
    captured_context = otel_context.get_current()

    def relay():
        token = otel_context.attach(captured_context)
        try:
            with requests.post(
                f"{INFERENCE_API_URL}/v1/chat",
                json=payload,
                stream=True,
                timeout=(10, None),
            ) as upstream:
                for chunk in upstream.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
        finally:
            otel_context.detach(token)

    response = StreamingHttpResponse(relay(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
