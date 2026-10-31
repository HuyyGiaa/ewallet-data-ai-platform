"""Pydantic request, response, error, and health schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)


CanonicalId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class TransactionType(str, Enum):
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    TRANSFER = "transfer"
    PAYMENT = "payment"


class TransactionChannel(str, Enum):
    APP = "app"
    WEB = "web"
    ATM = "atm"
    UNKNOWN = "UNKNOWN"


class FraudPredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: CanonicalId
    event_timestamp: AwareDatetime
    user_id: CanonicalId
    account_id: CanonicalId
    device_id: CanonicalId
    merchant_id: CanonicalId | None = None
    counterparty_account_id: CanonicalId | None = None
    type: TransactionType
    amount: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    currency: Literal["VND"]
    channel: TransactionChannel

    @model_validator(mode="after")
    def validate_conditional_entities(self) -> "FraudPredictionRequest":
        if self.type == TransactionType.PAYMENT and self.merchant_id is None:
            raise ValueError("merchant_id is required for payment")
        if (
            self.type == TransactionType.TRANSFER
            and self.counterparty_account_id is None
        ):
            raise ValueError("counterparty_account_id is required for transfer")
        return self

    @computed_field(return_type=bool)
    @property
    def merchant_present(self) -> bool:
        return self.merchant_id is not None

    def request_time_features(self) -> dict[str, object]:
        return {
            "amount": float(self.amount),
            "type": self.type.value,
            "channel": self.channel.value,
            "merchant_present": self.merchant_present,
        }


class FraudPredictionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: CanonicalId
    fraud_probability: Annotated[
        float, Field(ge=0.0, le=1.0, allow_inf_nan=False)
    ]
    fraud_prediction: Literal[0, 1]
    model_name: CanonicalId
    model_version: CanonicalId
    feature_timestamp: AwareDatetime
    correlation_id: CanonicalId


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: CanonicalId
    message: str
    correlation_id: CanonicalId
    retryable: bool = False
    details: tuple[dict[str, object], ...] = ()


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorBody


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "ready", "not_ready"]
    service: Literal["fraud-inference"] = "fraud-inference"


class DependencyReadinessResponse(BaseModel):
    """Connectivity readiness, not a feature materialization/parity claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"] = "ready"
    service: Literal["fraud-inference"] = "fraud-inference"
    scope: Literal["dependency_connectivity"] = "dependency_connectivity"
    feature_materialization_verified: Literal[False] = False
    offline_online_parity_verified: Literal[False] = False


class ModelPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fraud_probability: Annotated[
        float, Field(ge=0.0, le=1.0, allow_inf_nan=False)
    ]
    fraud_prediction: Literal[0, 1]
    model_name: CanonicalId
    model_version: CanonicalId
