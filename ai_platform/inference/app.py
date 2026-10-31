"""FastAPI application factory for Block B online inference."""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ai_platform.inference.assembler import CanonicalFeatureAssembler
from ai_platform.inference.config import (
    InferenceSettings,
    configure_feast_redis_timeouts,
)
from ai_platform.inference.errors import InferenceError
from ai_platform.inference.feast_online import FeastOnlineAdapter
from ai_platform.inference.kserve import KServeV2Client
from ai_platform.inference.observability import (
    TelemetryHooks,
    default_telemetry,
)
from ai_platform.inference.schemas import (
    DependencyReadinessResponse,
    ErrorBody,
    ErrorResponse,
    FraudPredictionRequest,
    FraudPredictionResponse,
    HealthResponse,
)
from ai_platform.inference.service import InferenceService


_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def normalize_correlation_id(value: str | None) -> str:
    if value is not None and _CORRELATION_ID.fullmatch(value):
        return value
    return str(uuid4())


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    correlation_id: str,
    retryable: bool = False,
    details: tuple[dict[str, object], ...] = (),
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            correlation_id=correlation_id,
            retryable=retryable,
            details=details,
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def _build_service(
    settings: InferenceSettings, telemetry: TelemetryHooks
) -> InferenceService:
    from feast import FeatureStore

    store = FeatureStore(repo_path=str(settings.feast_repo_path))
    configure_feast_redis_timeouts(
        store, timeout_seconds=settings.feast_timeout_seconds
    )
    return InferenceService(
        feature_provider=FeastOnlineAdapter(
            store, timeout_seconds=settings.feast_timeout_seconds
        ),
        assembler=CanonicalFeatureAssembler(),
        predictor=KServeV2Client(
            base_url=settings.kserve_base_url,
            model_name=settings.model_name,
            model_version=settings.model_version,
            timeout_seconds=settings.kserve_timeout_seconds,
        ),
        telemetry=telemetry,
    )


def _get_service(request: Request) -> InferenceService:
    return request.app.state.inference_service


def create_app(
    *,
    settings: InferenceSettings | None = None,
    service: InferenceService | None = None,
    telemetry: TelemetryHooks | None = None,
) -> FastAPI:
    hooks = telemetry or default_telemetry()
    inference_service = service or _build_service(
        settings or InferenceSettings.from_env(), hooks
    )
    app = FastAPI(title="E-Wallet Fraud Inference", version="1.0.0")
    app.state.inference_service = inference_service
    app.state.telemetry = hooks

    @app.middleware("http")
    async def correlation_and_trace_context(request: Request, call_next):
        correlation_id = normalize_correlation_id(
            request.headers.get("x-correlation-id")
        )
        request.state.correlation_id = correlation_id
        detach = None
        try:
            from opentelemetry import context, propagate

            token = context.attach(propagate.extract(dict(request.headers)))
            detach = lambda: context.detach(token)
        except ImportError:
            pass
        try:
            response = await call_next(request)
        finally:
            if detach is not None:
                detach()
        response.headers["x-correlation-id"] = correlation_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        hooks.record_error("invalid_request")
        details = tuple(
            {
                "location": [str(part) for part in item["loc"]],
                "message": item["msg"],
                "type": item["type"],
            }
            for item in exc.errors()
        )
        return _error_response(
            status_code=422,
            code="invalid_request",
            message="Request validation failed",
            correlation_id=request.state.correlation_id,
            details=details,
        )

    @app.exception_handler(InferenceError)
    async def inference_error(request: Request, exc: InferenceError):
        return _error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.public_message,
            correlation_id=request.state.correlation_id,
            retryable=exc.retryable,
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        hooks.record_error("internal_error")
        return _error_response(
            status_code=500,
            code="internal_error",
            message="Internal inference error",
            correlation_id=request.state.correlation_id,
        )

    @app.post(
        "/v1/fraud/predict",
        response_model=FraudPredictionResponse,
        responses={
            422: {"model": ErrorResponse},
            424: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
    )
    async def predict(
        payload: FraudPredictionRequest,
        request: Request,
        dependency: InferenceService = Depends(_get_service),
    ) -> FraudPredictionResponse:
        return await dependency.predict(
            payload, correlation_id=request.state.correlation_id
        )

    @app.get("/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/health/ready",
        response_model=DependencyReadinessResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def ready(
        dependency: InferenceService = Depends(_get_service),
    ) -> DependencyReadinessResponse:
        await dependency.ready()
        return DependencyReadinessResponse()

    return app
