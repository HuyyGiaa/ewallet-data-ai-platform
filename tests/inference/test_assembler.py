from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.inference.assembler import (
    CanonicalFeatureAssembler,
    HistoricalFeatureSet,
)
from ai_platform.inference.errors import (
    FeatureContractError,
    MissingFeatureError,
)
from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.ml.training.contract import (
    HISTORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
)


UTC = timezone.utc
EVENT_TIME = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def request(merchant_id: str | None = "merchant-1") -> FraudPredictionRequest:
    return FraudPredictionRequest.model_validate(
        {
            "transaction_id": "tx-1",
            "event_timestamp": EVENT_TIME,
            "user_id": "user-1",
            "account_id": "account-1",
            "device_id": "device-1",
            "merchant_id": merchant_id,
            "counterparty_account_id": None,
            "type": "payment" if merchant_id else "withdraw",
            "amount": 120.0,
            "currency": "VND",
            "channel": "app",
        }
    )


def historical(
    *,
    merchant_id: str | None = "merchant-1",
) -> HistoricalFeatureSet:
    values: dict[str, object] = {}
    timestamps: dict[str, datetime | None] = {}
    integer_names = {
        f"{view_name}__{name}"
        for view_name, spec in FEATURE_SPECS.items()
        for name in spec["integer_features"]
    }
    for index, column in enumerate(HISTORICAL_FEATURE_COLUMNS):
        values[column] = index + 1 if column in integer_names else float(index + 1)
        timestamps[column] = EVENT_TIME - timedelta(minutes=2)
    values.update(
        {
            "user_behavior__user_avg_amount_30d": 60.0,
            "user_behavior__user_std_amount_30d": 30.0,
            "account_behavior__account_avg_amount_30d": 40.0,
            "account_behavior__account_std_amount_30d": 20.0,
            "user_behavior__user_tx_count_1h": 4,
            "account_behavior__account_tx_count_1h": 2,
            "merchant_behavior__merchant_tx_count_10m": 2,
            "merchant_behavior__merchant_tx_count_24h": 288,
        }
    )
    if merchant_id is None:
        for column in HISTORICAL_FEATURE_COLUMNS:
            if column.startswith("merchant_behavior__"):
                values[column] = None
                timestamps[column] = None
    return HistoricalFeatureSet(values=values, timestamps=timestamps)


class CanonicalFeatureAssemblerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assembler = CanonicalFeatureAssembler()

    def test_assembles_exact_order_and_frozen_formulas(self) -> None:
        vector = self.assembler.assemble(request(), historical())
        self.assertEqual(MODEL_FEATURE_COLUMNS, vector.columns)
        self.assertEqual(41, len(vector.values))
        values = vector.as_dict()
        self.assertEqual(120.0, values["amount"])
        self.assertEqual("payment", values["type"])
        self.assertEqual("app", values["channel"])
        self.assertIs(True, values["merchant_present"])
        self.assertEqual(2.0, values["amount_ratio_to_user_avg_30d"])
        self.assertEqual(2.0, values["amount_zscore_user_30d"])
        self.assertEqual(3.0, values["amount_ratio_to_account_avg_30d"])
        self.assertEqual(4.0, values["amount_zscore_account_30d"])
        self.assertEqual(0.5, values["account_tx_share_1h"])
        self.assertEqual(
            1.0, values["merchant_activity_rate_ratio_10m_vs_24h"]
        )
        self.assertFalse(
            {
                "transaction_id",
                "user_id",
                "account_id",
                "device_id",
                "merchant_id",
                "label",
            }.intersection(values)
        )

    def test_assembly_is_deterministic_and_uses_oldest_feature_timestamp(self) -> None:
        features = historical()
        timestamps = dict(features.timestamps)
        oldest = EVENT_TIME - timedelta(minutes=9)
        timestamps["device_behavior__device_age_seconds"] = oldest
        features = HistoricalFeatureSet(features.values, timestamps)
        first = self.assembler.assemble(request(), features)
        second = self.assembler.assemble(request(), features)
        self.assertEqual(first, second)
        self.assertEqual(oldest, first.feature_timestamp)

    def test_rejects_missing_extra_null_and_wrong_type_contract_drift(self) -> None:
        base = historical()
        cases = []

        missing = dict(base.values)
        missing.pop("user_behavior__user_tx_count_5m")
        cases.append(HistoricalFeatureSet(missing, base.timestamps))

        extra = dict(base.values)
        extra["user_behavior__not_frozen"] = 1
        cases.append(HistoricalFeatureSet(extra, base.timestamps))

        null_value = dict(base.values)
        null_value["account_behavior__account_tx_count_1h"] = None
        cases.append(HistoricalFeatureSet(null_value, base.timestamps))

        wrong_type = dict(base.values)
        wrong_type["device_behavior__device_tx_count_1h"] = 1.5
        cases.append(HistoricalFeatureSet(wrong_type, base.timestamps))

        for features in cases:
            with self.subTest(features=features):
                with self.assertRaises((FeatureContractError, MissingFeatureError)):
                    self.assembler.assemble(request(), features)

    def test_merchant_null_preserves_explicit_nulls_without_defaults(self) -> None:
        vector = self.assembler.assemble(
            request(merchant_id=None), historical(merchant_id=None)
        )
        values = vector.as_dict()
        merchant_values = [
            values[column]
            for column in HISTORICAL_FEATURE_COLUMNS
            if column.startswith("merchant_behavior__")
        ]
        self.assertEqual([None] * 7, merchant_values)
        self.assertIsNone(
            values["merchant_activity_rate_ratio_10m_vs_24h"]
        )
        self.assertIs(False, values["merchant_present"])

    def test_zero_denominator_remains_null_not_zero_or_nan(self) -> None:
        features = historical()
        values = dict(features.values)
        values["user_behavior__user_avg_amount_30d"] = 0.0
        values["account_behavior__account_std_amount_30d"] = 0.0
        values["user_behavior__user_tx_count_1h"] = 0
        vector = self.assembler.assemble(
            request(), HistoricalFeatureSet(values, features.timestamps)
        )
        result = vector.as_dict()
        self.assertIsNone(result["amount_ratio_to_user_avg_30d"])
        self.assertIsNone(result["amount_zscore_account_30d"])
        self.assertIsNone(result["account_tx_share_1h"])

    def test_non_null_merchant_request_rejects_null_merchant_features(self) -> None:
        features = historical()
        values = dict(features.values)
        values["merchant_behavior__merchant_tx_count_10m"] = None
        with self.assertRaises(MissingFeatureError):
            self.assembler.assemble(
                request(), HistoricalFeatureSet(values, features.timestamps)
            )


if __name__ == "__main__":
    unittest.main()
