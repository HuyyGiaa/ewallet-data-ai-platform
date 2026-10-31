"""feature_lookup: authorized historical online features for one transaction.

Reuses the Block B ``OnlineFeatureProvider`` (Feast Online, freshness and
contract checks). Only the 31 historical features are served; the 6 derived
features exist only inside fraud scoring, and nothing here claims the 41-input
vector is materialized in Feast.
"""

from __future__ import annotations

from datetime import datetime
import math
from numbers import Integral, Real
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_platform.inference.errors import FeatureContractError
from ai_platform.inference.feast_online import OnlineFeatureProvider
from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.llm_agent.contracts import RequestContext
from ai_platform.llm_agent.tools.runtime import ToolOutcome, ToolUnavailableError
from ai_platform.ml.training.contract import (
    DERIVED_FEATURE_COLUMNS,
    HISTORICAL_FEATURE_COLUMNS,
    REQUEST_TIME_FEATURE_COLUMNS,
)

AUTHORIZED_FEATURES = frozenset(HISTORICAL_FEATURE_COLUMNS)


class FeatureLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction: FraudPredictionRequest
    feature_names: Annotated[
        tuple[str, ...] | None, Field(min_length=1, max_length=len(HISTORICAL_FEATURE_COLUMNS))
    ] = None

    @field_validator("feature_names")
    @classmethod
    def _authorized_unique(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("feature_names must be unique")
        unknown = sorted(set(value) - AUTHORIZED_FEATURES)
        if unknown:
            raise ValueError(f"feature_names are not authorized: {unknown}")
        return value


def _json_value(value: object) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise FeatureContractError()
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real) and math.isfinite(float(value)):
        return float(value)
    raise FeatureContractError()


class FeatureLookupTool:
    name = "feature_lookup"
    description = (
        "Return the authorized historical online features (Feast Online, freshness-checked) "
        "for one canonical transaction. Fails closed when features are missing or stale."
    )
    input_model = FeatureLookupInput

    def __init__(self, provider: OnlineFeatureProvider) -> None:
        self._provider = provider

    async def run(self, arguments: FeatureLookupInput, context: RequestContext) -> ToolOutcome:
        historical = await self._provider.get_features(arguments.transaction)
        names = arguments.feature_names or HISTORICAL_FEATURE_COLUMNS
        features: dict[str, dict[str, object]] = {}
        for name in names:
            if name not in historical.values or name not in historical.timestamps:
                raise FeatureContractError()
            timestamp = historical.timestamps[name]
            if timestamp is not None and not isinstance(timestamp, datetime):
                raise FeatureContractError()
            features[name] = {
                "value": _json_value(historical.values[name]),
                "event_timestamp": timestamp.isoformat() if timestamp is not None else None,
            }
        return ToolOutcome(result={
            "transaction_id": arguments.transaction.transaction_id,
            "historical_features": features,
            "request_time_features": arguments.transaction.request_time_features(),
            "feature_contract": {
                "historical_returned": len(features),
                "historical": len(HISTORICAL_FEATURE_COLUMNS),
                "request_time": len(REQUEST_TIME_FEATURE_COLUMNS),
                "derived_in_scoring_only": len(DERIVED_FEATURE_COLUMNS),
            },
            "materialization_verified": False,
        })

    async def ready(self) -> None:
        if await self._provider.ready() is not True:
            raise ToolUnavailableError("feature_store_not_ready", "Online feature store is not ready")
