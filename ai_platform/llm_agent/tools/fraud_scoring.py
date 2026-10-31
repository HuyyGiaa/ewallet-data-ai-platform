"""fraud_scoring: KServe prediction through the Block B ``InferenceService``.

The tool never produces a score itself. It requires the prediction to come
from the configured model name and version, so a different model behind the
endpoint is reported as a contract error rather than silently accepted.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ai_platform.inference.contracts import MODEL_SCHEMA_SHA256
from ai_platform.inference.schemas import FraudPredictionRequest, FraudPredictionResponse
from ai_platform.llm_agent.contracts import RequestContext
from ai_platform.llm_agent.tools.runtime import ToolContractError, ToolOutcome, ToolUnavailableError

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


class FraudPredictor(Protocol):
    async def predict(
        self, request: FraudPredictionRequest, *, correlation_id: str
    ) -> FraudPredictionResponse: ...

    async def ready(self) -> bool: ...


class FraudScoringInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction: FraudPredictionRequest


class FraudScoringTool:
    name = "fraud_scoring"
    description = (
        "Score one canonical transaction with the registered KServe fraud model. "
        "Returns the model's probability and provenance; never a synthetic score."
    )
    input_model = FraudScoringInput

    def __init__(self, predictor: FraudPredictor, *, model_name: str, model_version: str) -> None:
        if not (_MODEL_ID.fullmatch(model_name) and _MODEL_ID.fullmatch(model_version)):
            raise ValueError("model_name and model_version must be explicit identifiers")
        self._predictor = predictor
        self._model = (model_name, model_version)

    async def run(self, arguments: FraudScoringInput, context: RequestContext) -> ToolOutcome:
        prediction = await self._predictor.predict(arguments.transaction, correlation_id=context.request_id)
        if prediction.transaction_id != arguments.transaction.transaction_id:
            raise ToolContractError("prediction_identity_mismatch", "Prediction is for another transaction")
        if (prediction.model_name, prediction.model_version) != self._model:
            raise ToolContractError("unexpected_model", "Prediction came from an unexpected model version")
        return ToolOutcome(
            result={
                "transaction_id": prediction.transaction_id,
                "fraud_probability": prediction.fraud_probability,
                "fraud_prediction": prediction.fraud_prediction,
                "model_name": prediction.model_name,
                "model_version": prediction.model_version,
                "feature_timestamp": prediction.feature_timestamp.isoformat(),
                "model_input_schema_sha256": MODEL_SCHEMA_SHA256,
            },
            version_overrides={"model_revision": f"{prediction.model_name}/{prediction.model_version}"},
        )

    async def ready(self) -> None:
        if await self._predictor.ready() is not True:
            raise ToolUnavailableError("model_service_not_ready", "Fraud model service is not ready")
