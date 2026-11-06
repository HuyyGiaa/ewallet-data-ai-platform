"""Synchronous helpers for driving the Fraud API from property tests.

Hypothesis re-enters a test once per example, so these helpers hide the event
loop and build the application around in-memory backends.
"""

from __future__ import annotations

import asyncio
from typing import Mapping

from fastapi import FastAPI

from ai_platform.inference.app import create_app
from ai_platform.inference.observability import NoopTelemetry
from tests.inference.support import build_fraud_service
from tests.inference.test_service_and_app import asgi_request


PREDICT_PATH = "/v1/fraud/predict"


def fraud_app(*, store: object | None = None, transport: object | None = None) -> FastAPI:
    """Build the real application with fake network edges."""
    return create_app(
        service=build_fraud_service(store=store, transport=transport),
        telemetry=NoopTelemetry(),
    )


def post_prediction(
    app: FastAPI, payload: Mapping[str, object], correlation_id: str = "corr-property"
) -> tuple[int, dict[str, str], dict[str, object]]:
    """POST one prediction and return status, headers and decoded body."""
    return asyncio.run(
        asgi_request(
            app,
            "POST",
            PREDICT_PATH,
            dict(payload),
            {"x-correlation-id": correlation_id},
        )
    )


def post_concurrently(
    app: FastAPI, requests: list[tuple[Mapping[str, object], str]]
) -> list[tuple[int, dict[str, str], dict[str, object]]]:
    """Issue all ``(payload, correlation_id)`` pairs on one event loop."""

    async def gather() -> list[tuple[int, dict[str, str], dict[str, object]]]:
        return list(
            await asyncio.gather(
                *(
                    asgi_request(
                        app,
                        "POST",
                        PREDICT_PATH,
                        dict(payload),
                        {"x-correlation-id": correlation_id},
                    )
                    for payload, correlation_id in requests
                )
            )
        )

    return asyncio.run(gather())
