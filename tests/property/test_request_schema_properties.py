"""Request and response schema boundaries as universal properties."""

from __future__ import annotations

from hypothesis import assume, given, strategies as st
from pydantic import ValidationError
import pytest

from ai_platform.inference.schemas import (
    FraudPredictionRequest,
    FraudPredictionResponse,
    ModelPrediction,
)
from tests.inference.support import FEATURE_TIME, MODEL_NAME, MODEL_VERSION
from tests.property.strategies import (
    ID_FIELDS,
    invalid_payload_classes,
    valid_payloads,
)


@given(valid_payloads())
def test_every_valid_payload_is_accepted(payload):
    request = FraudPredictionRequest.model_validate(payload)
    assert request.merchant_present is (payload["merchant_id"] is not None)
    assert request.transaction_id == payload["transaction_id"]
    assert request.user_id == payload["user_id"]


@given(valid_payloads())
def test_request_time_features_are_the_four_contract_values(payload):
    request = FraudPredictionRequest.model_validate(payload)
    assert request.request_time_features() == {
        "amount": payload["amount"],
        "type": payload["type"],
        "channel": payload["channel"],
        "merchant_present": payload["merchant_id"] is not None,
    }


@pytest.mark.parametrize("partition", sorted(invalid_payload_classes()))
@given(data=st.data())
def test_every_invalid_partition_is_rejected(partition, data):
    payload = data.draw(invalid_payload_classes()[partition])
    with pytest.raises(ValidationError):
        FraudPredictionRequest.model_validate(payload)


@given(valid_payloads())
def test_json_round_trip_preserves_the_request(payload):
    request = FraudPredictionRequest.model_validate(payload)
    dumped = request.model_dump(mode="json", exclude={"merchant_present"})
    assert FraudPredictionRequest.model_validate(dumped) == request


@given(valid_payloads(), st.sampled_from(ID_FIELDS), st.text(" \t", min_size=1, max_size=4))
def test_surrounding_whitespace_never_changes_an_identifier(payload, field, padding):
    padded = {**payload, field: f"{padding}{payload[field]}{padding}"}
    assert FraudPredictionRequest.model_validate(
        padded
    ) == FraudPredictionRequest.model_validate(payload)


@given(valid_payloads())
def test_requests_are_immutable(payload):
    request = FraudPredictionRequest.model_validate(payload)
    with pytest.raises(ValidationError):
        request.amount = 1.0


def _response(probability: float, prediction: int = 0) -> FraudPredictionResponse:
    return FraudPredictionResponse(
        transaction_id="tx",
        fraud_probability=probability,
        fraud_prediction=prediction,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        feature_timestamp=FEATURE_TIME,
        correlation_id="corr",
    )


@given(st.floats(min_value=0.0, max_value=1.0))
def test_probabilities_inside_the_unit_interval_are_accepted(probability):
    assert _response(probability).fraud_probability == probability


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_probabilities_outside_the_unit_interval_are_rejected(probability):
    assume(not 0.0 <= probability <= 1.0)
    with pytest.raises(ValidationError):
        _response(probability)
    with pytest.raises(ValidationError):
        ModelPrediction(
            fraud_probability=probability,
            fraud_prediction=0,
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
        )


@given(st.integers().filter(lambda value: value not in (0, 1)))
def test_predictions_are_binary(prediction):
    with pytest.raises(ValidationError):
        _response(0.5, prediction)
