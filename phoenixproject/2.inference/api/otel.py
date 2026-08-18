import os


def setup_tracing(app) -> None:
    if not os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"):
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from phoenix.otel import register

    tracer_provider = register(
        project_name=os.environ.get("PHOENIX_PROJECT_NAME", "inference-api"),
        batch=True,
    )
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=tracer_provider, exclude_spans=["receive", "send"]
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
