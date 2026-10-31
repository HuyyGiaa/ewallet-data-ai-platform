"""Dependency-injected orchestration for one fraud prediction request."""

from __future__ import annotations

from typing import Protocol

from ai_platform.inference.assembler import CanonicalFeatureAssembler, ModelInputVector
from ai_platform.inference.errors import InferenceError
from ai_platform.inference.feast_online import OnlineFeatureProvider
from ai_platform.inference.observability import (
    EventLogger,
    JsonEventLogger,
    NoopTelemetry,
    TelemetryHooks,
)
from ai_platform.inference.schemas import (
    FraudPredictionRequest,
    FraudPredictionResponse,
    ModelPrediction,
)


class ModelPredictor(Protocol):
    async def predict(
        self, vector: ModelInputVector, *, correlation_id: str
    ) -> ModelPrediction: ...

    async def ready(self) -> bool: ...


class InferenceService:
    def __init__(
        self,
        *,
        feature_provider: OnlineFeatureProvider,
        assembler: CanonicalFeatureAssembler,
        predictor: ModelPredictor,
        telemetry: TelemetryHooks | None = None,
        event_logger: EventLogger | None = None,
    ) -> None:
        self._features = feature_provider
        self._assembler = assembler
        self._predictor = predictor
        self._telemetry = telemetry or NoopTelemetry()
        self._logger = event_logger or JsonEventLogger()

    async def predict(
        self,
        request: FraudPredictionRequest,
        *,
        correlation_id: str,
    ) -> FraudPredictionResponse:
        with self._telemetry.request(correlation_id=correlation_id):
            try:
                with self._telemetry.stage(
                    "feature_retrieval", correlation_id=correlation_id
                ):
                    historical = await self._features.get_features(request)
                with self._telemetry.stage(
                    "feature_assembly", correlation_id=correlation_id
                ):
                    vector = self._assembler.assemble(request, historical)
                with self._telemetry.stage(
                    "model_inference", correlation_id=correlation_id
                ):
                    prediction = await self._predictor.predict(
                        vector, correlation_id=correlation_id
                    )
            except InferenceError as exc:
                self._telemetry.record_error(exc.code)
                self._logger.event(
                    "prediction_failed",
                    correlation_id=correlation_id,
                    error_code=exc.code,
                    retryable=exc.retryable,
                )
                raise
            except Exception:
                self._telemetry.record_error("internal_error")
                self._logger.event(
                    "prediction_failed",
                    correlation_id=correlation_id,
                    error_code="internal_error",
                    retryable=False,
                )
                raise

            self._logger.event(
                "prediction_completed",
                correlation_id=correlation_id,
                model_name=prediction.model_name,
                model_version=prediction.model_version,
            )
            return FraudPredictionResponse(
                transaction_id=request.transaction_id,
                fraud_probability=prediction.fraud_probability,
                fraud_prediction=prediction.fraud_prediction,
                model_name=prediction.model_name,
                model_version=prediction.model_version,
                feature_timestamp=vector.feature_timestamp,
                correlation_id=correlation_id,
            )

    async def ready(self) -> bool:
        await self._features.ready()
        await self._predictor.ready()
        return True
