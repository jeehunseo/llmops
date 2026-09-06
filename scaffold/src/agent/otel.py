"""Langfuse tracing over OTLP, matching the sibling projects' setup.

LangChainInstrumentor emits an OpenInference span per node / LLM call / tool
call, so a run shows up in Langfuse as the actual graph shape rather than one
opaque HTTP span.
"""

import base64

from agent.settings import Settings


def setup_tracing(app=None, settings: Settings | None = None) -> None:
    from agent.settings import get_settings

    settings = settings or get_settings()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return

    from openinference.instrumentation.langchain import LangChainInstrumentor
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()

    tracer_provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: settings.service_name})
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{settings.langfuse_host}/api/public/otel/v1/traces",
                headers={
                    "Authorization": f"Basic {auth}",
                    "x-langfuse-ingestion-version": "4",
                },
            )
        )
    )
    trace.set_tracer_provider(tracer_provider)
    LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
