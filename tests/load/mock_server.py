"""Local Fraud API for load tests, backed by in-memory Feast and KServe fakes.

The real FastAPI app, service, adapters, assembler and wire contract run here;
only the network edges are fake. Results therefore measure this application's
own overhead on this machine and are not production latency figures.

The server binds to loopback only and starts no cloud client.
"""

from __future__ import annotations

import argparse
import logging

from fastapi import FastAPI
import uvicorn

from ai_platform.inference.app import create_app
from ai_platform.inference.observability import NoopTelemetry
from tests.inference.support import (
    DeterministicModelTransport,
    EntityFeatureStore,
    build_fraud_service,
)
from tests.load.scenarios import DEPENDENCY_FAULTS, FEATURE_BACKEND, MODEL_BACKEND


LOOPBACK = "127.0.0.1"


def build_mock_app(*, model_latency_seconds: float = 0.0) -> FastAPI:
    """Compose the production application around deterministic fakes."""
    store = EntityFeatureStore(
        entity_faults={
            fault.user_id: fault.behaviour
            for fault in DEPENDENCY_FAULTS
            if fault.backend == FEATURE_BACKEND
        }
    )
    transport = DeterministicModelTransport(
        correlation_faults={
            fault.correlation_id: fault.behaviour
            for fault in DEPENDENCY_FAULTS
            if fault.backend == MODEL_BACKEND
        },
        latency_seconds=model_latency_seconds,
    )
    telemetry = NoopTelemetry()
    service = build_fraud_service(
        store=store, transport=transport, telemetry=telemetry
    )
    return create_app(service=service, telemetry=telemetry)


def main() -> None:
    """Serve the mock-backed application on the loopback interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--model-latency-ms", type=float, default=0.0)
    options = parser.parse_args()
    if options.model_latency_ms < 0:
        parser.error("--model-latency-ms must not be negative")

    logging.getLogger("ewallet.fraud_inference").setLevel(logging.WARNING)
    uvicorn.run(
        build_mock_app(model_latency_seconds=options.model_latency_ms / 1000),
        host=LOOPBACK,
        port=options.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
