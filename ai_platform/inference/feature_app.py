"""FastAPI application for reading online features by entity id.

The API is a thin HTTP layer over ``FeastOnlineAdapter``: it adds no Redis access and no
freshness rule of its own, so a lookup passes the same checks as a prediction. It is
read-only. Validation problems answer 422, missing or stale features 424, and backend
failures 503 or 504, using the same error body as the Fraud API.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import time
from typing import Mapping, Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from ai_platform.inference.assembler import HistoricalFeatureSet
from ai_platform.inference.config import configure_feast_redis_timeouts
from ai_platform.inference.errors import ConfigurationError, InferenceError
from ai_platform.inference.feast_online import FeastOnlineAdapter
from ai_platform.inference.http_common import error_response, normalize_correlation_id
from ai_platform.inference.schemas import (
    EntityTimestampRequest,
    EntityTimestampResponse,
    ErrorResponse,
    FeatureHealthResponse,
    FeatureLookupRequest,
    FeatureLookupResponse,
)


class FeatureReader(Protocol):
    """What the API needs from the online store adapter."""

    async def get_features(self, request: FeatureLookupRequest) -> HistoricalFeatureSet:
        """Return validated features, or raise an ``InferenceError``."""
        ...

    async def view_timestamps(self, request: EntityTimestampRequest) -> dict:
        """Return each view's stored time."""
        ...

    async def ready(self) -> bool:
        """Raise unless the online store can serve."""
        ...


class FeatureTelemetry(Protocol):
    """Record one finished lookup."""

    def record(self, *, outcome: str, seconds: float) -> None:
        """Count a lookup; ``outcome`` is ``success`` or an error code."""
        ...


class NoopFeatureTelemetry:
    """Discard lookup telemetry."""

    def record(self, *, outcome: str, seconds: float) -> None:
        """Ignore one lookup."""


class OpenTelemetryFeatureTelemetry:
    """Request counter and latency histogram for dashboards and KEDA."""

    def __init__(self) -> None:
        from opentelemetry import metrics

        meter = metrics.get_meter("ewallet.feature_api")
        self._requests = meter.create_counter("feature_api_requests_total")
        self._latency = meter.create_histogram("feature_api_request_latency_seconds", unit="s")

    def record(self, *, outcome: str, seconds: float) -> None:
        """Count a lookup by its bounded outcome."""
        self._requests.add(1, {"outcome": outcome})
        self._latency.record(seconds, {"outcome": outcome})


@dataclass(frozen=True)
class FeatureApiSettings:
    """Validated environment configuration."""

    feast_repo_path: Path
    timeout_seconds: float

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "FeatureApiSettings":
        """Read the same Feast variables the Fraud API uses."""
        values = os.environ if environment is None else environment
        raw_path = values.get("EWALLET_FEAST_REPO_PATH", "").strip()
        if not raw_path:
            raise ConfigurationError()
        repo = Path(raw_path).resolve()
        if not (repo / "feature_store.yaml").is_file():
            raise ConfigurationError()
        try:
            timeout = float(values.get("EWALLET_FEAST_TIMEOUT_SECONDS", "2.0"))
        except ValueError as exc:
            raise ConfigurationError() from exc
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise ConfigurationError()
        return cls(repo, timeout)


def build_reader(settings: FeatureApiSettings) -> FeastOnlineAdapter:
    """Open the Feast store with the bounded Redis timeouts."""
    from feast import FeatureStore

    store = FeatureStore(repo_path=str(settings.feast_repo_path))
    configure_feast_redis_timeouts(store, timeout_seconds=settings.timeout_seconds)
    return FeastOnlineAdapter(store, timeout_seconds=settings.timeout_seconds)


def create_feature_app(
    *, reader: FeatureReader, telemetry: FeatureTelemetry | None = None
) -> FastAPI:
    """Build the application around an injected reader."""
    hooks = telemetry or NoopFeatureTelemetry()
    app = FastAPI(title="E-Wallet Feature Retrieval", version="1.0.0")

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        """Attach a correlation id to the request and its response."""
        correlation_id = normalize_correlation_id(request.headers.get("x-correlation-id"))
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        """Answer 422 with sanitized details."""
        hooks.record(outcome="invalid_request", seconds=0.0)
        details = tuple(
            {"location": [str(part) for part in item["loc"]], "message": item["msg"], "type": item["type"]}
            for item in exc.errors()
        )
        return error_response(
            status_code=422, code="invalid_request", message="Request validation failed",
            correlation_id=request.state.correlation_id, details=details,
        )

    @app.exception_handler(InferenceError)
    async def inference_error(request: Request, exc: InferenceError):
        """Answer a known failure with its public code."""
        return error_response(
            status_code=exc.status_code, code=exc.code, message=exc.public_message,
            correlation_id=request.state.correlation_id, retryable=exc.retryable,
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        """Answer an unexpected failure without details."""
        hooks.record(outcome="internal_error", seconds=0.0)
        return error_response(
            status_code=500, code="internal_error", message="Internal feature error",
            correlation_id=request.state.correlation_id,
        )

    @app.post(
        "/v1/features/lookup",
        response_model=FeatureLookupResponse,
        responses={code: {"model": ErrorResponse} for code in (422, 424, 502, 503, 504)},
    )
    async def lookup(payload: FeatureLookupRequest, request: Request) -> FeatureLookupResponse:
        """Return the requested features, all inside the freshness policy."""
        started = time.perf_counter()
        try:
            found = await reader.get_features(payload)
        except InferenceError as error:
            hooks.record(outcome=error.code, seconds=time.perf_counter() - started)
            raise
        selected = {name: found.values[name] for name in payload.feature_names}
        stamps = [found.timestamps[name] for name in payload.feature_names if found.timestamps[name]]
        if not stamps:
            stamps = [stamp for stamp in found.timestamps.values() if stamp]
        hooks.record(outcome="success", seconds=time.perf_counter() - started)
        return FeatureLookupResponse(
            features=selected,
            oldest_feature_timestamp=min(stamps),
            correlation_id=request.state.correlation_id,
        )

    @app.post("/internal/v1/entity-timestamps", response_model=EntityTimestampResponse)
    async def entity_timestamps(payload: EntityTimestampRequest) -> EntityTimestampResponse:
        """Return stored view times for fixture preflight; the gateway never routes this."""
        return EntityTimestampResponse(views=await reader.view_timestamps(payload))

    @app.get("/health/live", response_model=FeatureHealthResponse)
    async def live() -> FeatureHealthResponse:
        """Report that the process is up."""
        return FeatureHealthResponse(status="ok")

    @app.get(
        "/health/ready",
        response_model=FeatureHealthResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def ready() -> FeatureHealthResponse:
        """Report that the online store can serve."""
        await reader.ready()
        return FeatureHealthResponse(status="ready")

    return app
