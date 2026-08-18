import base64
import os


def setup_tracing(app) -> None:
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    host = os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000")
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    tracer_provider = TracerProvider(
        resource=Resource.create(
            {SERVICE_NAME: os.environ.get("LANGFUSE_PROJECT_NAME", "inference-api")}
        )
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{host}/api/public/otel/v1/traces",
                headers={
                    "Authorization": f"Basic {auth}",
                    "x-langfuse-ingestion-version": "4",
                },
            )
        )
    )
    trace.set_tracer_provider(tracer_provider)
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=tracer_provider, exclude_spans=["receive", "send"]
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
