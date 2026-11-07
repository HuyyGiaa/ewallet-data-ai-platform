"""Production entrypoint: OpenTelemetry providers, /metrics, then uvicorn.

Metrics use the Prometheus reader on the app's own port; spans carry the
correlation id and the W3C context is propagated to KServe by the client.
Every finished request logs its trace id so traces can be correlated with
KServe access logs without a collector.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from opentelemetry import metrics, trace
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import make_asgi_app

from ai_platform.features.feast.registry_mirror import mirror_from_environment
from ai_platform.inference.app import create_app
from ai_platform.inference.vault import load_from_environment


def configure_telemetry() -> None:
    resource = Resource.create(
        {
            "service.name": "fraud-api",
            "service.version": os.environ.get("EWALLET_GIT_SHA", "unknown"),
            "ewallet.model.name": os.environ.get("EWALLET_MODEL_NAME", ""),
            "ewallet.model.version": os.environ.get("EWALLET_MODEL_VERSION", ""),
        }
    )
    metrics.set_meter_provider(
        MeterProvider(resource=resource, metric_readers=[PrometheusMetricReader()])
    )
    trace.set_tracer_provider(TracerProvider(resource=resource))


def build():
    # Before Feast reads EWALLET_REDIS_CONNECTION_STRING; a no-op unless enabled.
    for secret in load_from_environment(os.environ):
        logging.getLogger("ewallet.fraud_inference").info(
            '{"event":"vault_secret_loaded","env":"%s","path":"%s","version":%d}',
            secret.env_name,
            secret.path,
            secret.version,
        )
    # Read-only object-level mirror; Feast's GCS store would need buckets.get.
    mirror = mirror_from_environment()
    if mirror is not None:
        logging.getLogger("ewallet.fraud_inference").info(
            '{"event":"feast_registry_mirrored","uri":"%s","generation":%d}',
            mirror.uri,
            mirror.generation,
        )
    configure_telemetry()
    app = create_app()
    app.mount("/metrics", make_asgi_app())

    tracer = trace.get_tracer("ewallet.fraud_api")

    @app.middleware("http")
    async def log_trace_id(request, call_next):
        if request.url.path != "/v1/fraud/predict":
            return await call_next(request)
        with tracer.start_as_current_span("POST /v1/fraud/predict") as span:
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status_code)
            context = span.get_span_context()
        logging.getLogger("ewallet.fraud_inference").info(
            '{"event":"request_finished","path":"%s","status":%d,"trace_id":"%s","correlation_id":"%s"}',
            request.url.path,
            response.status_code,
            format(context.trace_id, "032x"),
            response.headers.get("x-correlation-id", ""),
        )
        return response

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run(build(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
