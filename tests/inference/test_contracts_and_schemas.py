from __future__ import annotations

from datetime import datetime, timezone
import unittest

from pydantic import ValidationError

from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.errors import (
    FeatureBackendError,
    MissingFeatureError,
    ModelBackendError,
    ModelTimeoutError,
    StaleFeatureError,
)
from ai_platform.inference.schemas import (
    FraudPredictionRequest,
    FraudPredictionResponse,
)


EXPECTED_INPUT_COLUMNS = (
    "amount", "type", "channel", "merchant_present",
    "user_behavior__user_tx_count_5m", "user_behavior__user_tx_count_1h",
    "user_behavior__user_tx_count_24h", "user_behavior__user_amount_sum_1h",
    "user_behavior__user_amount_observation_count_30d",
    "user_behavior__user_avg_amount_30d", "user_behavior__user_std_amount_30d",
    "user_behavior__user_failed_rate_24h",
    "user_behavior__user_distinct_merchants_24h",
    "account_behavior__account_tx_count_5m",
    "account_behavior__account_tx_count_1h",
    "account_behavior__account_tx_count_24h",
    "account_behavior__account_amount_sum_1h",
    "account_behavior__account_amount_sum_24h",
    "account_behavior__account_amount_observation_count_30d",
    "account_behavior__account_avg_amount_30d",
    "account_behavior__account_std_amount_30d",
    "account_behavior__account_failed_rate_24h",
    "account_behavior__account_seconds_since_last_tx",
    "device_behavior__device_age_seconds", "device_behavior__device_tx_count_1h",
    "device_behavior__device_tx_count_24h",
    "device_behavior__device_amount_sum_24h",
    "device_behavior__device_failed_rate_24h",
    "merchant_behavior__merchant_tx_count_10m",
    "merchant_behavior__merchant_tx_count_1h",
    "merchant_behavior__merchant_tx_count_24h",
    "merchant_behavior__merchant_unique_users_10m",
    "merchant_behavior__merchant_unique_users_1h",
    "merchant_behavior__merchant_amount_sum_1h",
    "merchant_behavior__merchant_avg_amount_24h",
    "amount_ratio_to_user_avg_30d", "amount_zscore_user_30d",
    "amount_ratio_to_account_avg_30d", "amount_zscore_account_30d",
    "account_tx_share_1h", "merchant_activity_rate_ratio_10m_vs_24h",
)


def valid_payload(**overrides):
    payload = {
        "transaction_id": "tx-001",
        "event_timestamp": "2026-10-29T12:00:00Z",
        "user_id": "user-001",
        "account_id": "account-001",
        "device_id": "device-001",
        "merchant_id": "merchant-001",
        "counterparty_account_id": None,
        "type": "payment",
        "amount": 125000.0,
        "currency": "VND",
        "channel": "app",
    }
    payload.update(overrides)
    return payload


class ModelInputContractTest(unittest.TestCase):
    def test_exact_frozen_41_dataframe_column_order(self) -> None:
        self.assertEqual(EXPECTED_INPUT_COLUMNS, MODEL_INPUT_COLUMNS)
        self.assertEqual(41, len(MODEL_INPUT_COLUMNS))


class FraudPredictionRequestTest(unittest.TestCase):
    def test_valid_request_exposes_only_four_request_time_features(self) -> None:
        request = FraudPredictionRequest.model_validate(valid_payload())
        self.assertEqual(
            {
                "amount": 125000.0,
                "type": "payment",
                "channel": "app",
                "merchant_present": True,
            },
            request.request_time_features(),
        )

    def test_merchant_null_is_valid_for_non_payment(self) -> None:
        request = FraudPredictionRequest.model_validate(
            valid_payload(
                type="withdraw",
                merchant_id=None,
                counterparty_account_id=None,
                channel="atm",
            )
        )
        self.assertFalse(request.merchant_present)

    def test_payment_requires_merchant(self) -> None:
        with self.assertRaises(ValidationError):
            FraudPredictionRequest.model_validate(
                valid_payload(merchant_id=None)
            )

    def test_transfer_requires_counterparty(self) -> None:
        with self.assertRaises(ValidationError):
            FraudPredictionRequest.model_validate(
                valid_payload(
                    type="transfer",
                    merchant_id=None,
                    counterparty_account_id=None,
                )
            )

    def test_rejects_naive_timestamp_non_positive_amount_and_extra_fields(self) -> None:
        invalid_payloads = (
            valid_payload(event_timestamp="2026-10-29T12:00:00"),
            valid_payload(amount=0),
            valid_payload(label=1),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    FraudPredictionRequest.model_validate(payload)

    def test_rejects_noncanonical_domains(self) -> None:
        for field, value in (
            ("type", "cashout"),
            ("channel", "mobile"),
            ("currency", "USD"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    FraudPredictionRequest.model_validate(
                        valid_payload(**{field: value})
                    )

    def test_ids_are_required_nonblank_strings(self) -> None:
        for field in ("transaction_id", "user_id", "account_id", "device_id"):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    FraudPredictionRequest.model_validate(
                        valid_payload(**{field: "   "})
                    )


class ResponseAndErrorContractTest(unittest.TestCase):
    def test_response_enforces_existing_model_output_semantics(self) -> None:
        response = FraudPredictionResponse(
            transaction_id="tx-001",
            fraud_probability=0.75,
            fraud_prediction=1,
            model_name="ewallet-fraud-classifier",
            model_version="7",
            feature_timestamp=datetime(2026, 10, 29, 11, 59, tzinfo=timezone.utc),
            correlation_id="corr-001",
        )
        self.assertEqual(1, response.fraud_prediction)
        with self.assertRaises(ValidationError):
            FraudPredictionResponse(
                transaction_id="tx-001",
                fraud_probability=1.1,
                fraud_prediction=1,
                model_name="ewallet-fraud-classifier",
                model_version="7",
                feature_timestamp=datetime(
                    2026, 10, 29, 11, 59, tzinfo=timezone.utc
                ),
                correlation_id="corr-001",
            )

    def test_error_classes_are_distinct_and_safely_classified(self) -> None:
        cases = (
            (MissingFeatureError(["a"]), "missing_feature", 424),
            (StaleFeatureError(["a"]), "stale_feature", 424),
            (FeatureBackendError(), "feature_backend_error", 503),
            (ModelTimeoutError(), "model_timeout", 504),
            (ModelBackendError(), "model_backend_error", 502),
        )
        self.assertEqual(
            [(code, status) for _, code, status in cases],
            [(error.code, error.status_code) for error, _, _ in cases],
        )


if __name__ == "__main__":
    unittest.main()
