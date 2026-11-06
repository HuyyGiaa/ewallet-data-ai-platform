"""Independent oracles and assertions for the property suites.

The derived-feature oracle is written with plain Python floats, on purpose: it
restates the F4D formulas without pandas, so a defect in the production
implementation cannot also hide in the expectation.
"""

from __future__ import annotations

import json
import math
from typing import Mapping

from ai_platform.inference.assembler import HistoricalFeatureSet, ModelInputVector
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.ml.training.contract import (
    DERIVED_FEATURE_COLUMNS,
    HISTORICAL_FEATURE_COLUMNS,
    REQUEST_TIME_FEATURE_COLUMNS,
)
from tests.inference.support import MODEL_NAME, MODEL_VERSION


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """F4D division: unavailable inputs and zero denominators stay missing."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    result = numerator / denominator
    return result if math.isfinite(result) else None


def _difference(left: float, right: float | None) -> float | None:
    return None if right is None else left - right


def expected_derived_features(
    request: FraudPredictionRequest, values: Mapping[str, object]
) -> dict[str, float | None]:
    """Compute the six derived features independently of production code."""
    amount = float(request.amount)
    user_avg = values["user_behavior__user_avg_amount_30d"]
    user_std = values["user_behavior__user_std_amount_30d"]
    account_avg = values["account_behavior__account_avg_amount_30d"]
    account_std = values["account_behavior__account_std_amount_30d"]
    merchant_10m = values["merchant_behavior__merchant_tx_count_10m"]
    return {
        "amount_ratio_to_user_avg_30d": safe_ratio(amount, user_avg),
        "amount_zscore_user_30d": safe_ratio(_difference(amount, user_avg), user_std),
        "amount_ratio_to_account_avg_30d": safe_ratio(amount, account_avg),
        "amount_zscore_account_30d": safe_ratio(
            _difference(amount, account_avg), account_std
        ),
        "account_tx_share_1h": safe_ratio(
            values["account_behavior__account_tx_count_1h"],
            values["user_behavior__user_tx_count_1h"],
        ),
        "merchant_activity_rate_ratio_10m_vs_24h": safe_ratio(
            None if merchant_10m is None else merchant_10m * 144.0,
            values["merchant_behavior__merchant_tx_count_24h"],
        ),
    }


def assert_close_or_both_missing(
    actual: object, expected: float | None, column: str
) -> None:
    """Compare numbers with float tolerance, requiring missingness to match."""
    if expected is None:
        assert actual is None, column
        return
    assert actual is not None, column
    assert math.isclose(float(actual), expected, rel_tol=1e-12, abs_tol=0.0), column


def assert_vector_contract(
    vector: ModelInputVector,
    request: FraudPredictionRequest,
    features: HistoricalFeatureSet,
) -> None:
    """Check the frozen 41-input layout, positions and JSON safety."""
    assert vector.columns == MODEL_INPUT_COLUMNS
    assert len(vector.values) == 41
    json.dumps(list(vector.values), allow_nan=False)

    by_name = vector.as_dict()
    time_features = request.request_time_features()
    for column in REQUEST_TIME_FEATURE_COLUMNS:
        assert by_name[column] == time_features[column], column
    for column in HISTORICAL_FEATURE_COLUMNS:
        assert by_name[column] == features.values[column], column
    expected = expected_derived_features(request, features.values)
    for column in DERIVED_FEATURE_COLUMNS:
        assert_close_or_both_missing(by_name[column], expected[column], column)


def assert_successful_prediction(body: Mapping[str, object], request_id: str) -> None:
    """Check the fraud response contract for one HTTP 200 body."""
    probability = body["fraud_probability"]
    assert isinstance(probability, float)
    assert 0.0 <= probability <= 1.0
    assert body["fraud_prediction"] in (0, 1)
    assert body["transaction_id"] == request_id
    assert body["model_name"] == MODEL_NAME
    assert body["model_version"] == MODEL_VERSION


def assert_sanitized_validation_error(status: int, body: Mapping[str, object]) -> None:
    """Check a 422 exposes only location, message and type per detail."""
    assert status == 422
    assert "fraud_probability" not in body
    error = body["error"]
    assert error["code"] == "invalid_request"
    assert error["retryable"] is False
    assert error["details"]
    for detail in error["details"]:
        assert set(detail) == {"location", "message", "type"}


def without_correlation(body: Mapping[str, object]) -> dict[str, object]:
    """Drop the only response field that legitimately varies per call."""
    return {key: value for key, value in body.items() if key != "correlation_id"}
