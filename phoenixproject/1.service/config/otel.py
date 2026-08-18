import os


def setup_tracing() -> None:
    if not os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"):
        return

    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from phoenix.otel import register

    tracer_provider = register(
        project_name=os.environ.get("PHOENIX_PROJECT_NAME", "service"),
        batch=True,
    )
    DjangoInstrumentor().instrument(tracer_provider=tracer_provider)
    RequestsInstrumentor().instrument(tracer_provider=tracer_provider)
