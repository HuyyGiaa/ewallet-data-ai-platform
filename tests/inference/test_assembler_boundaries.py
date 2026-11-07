"""Boundary behaviour of online feature assembly beyond the happy path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import math
import unittest

import numpy as np
import pandas as pd

from ai_platform.inference.assembler import (
    CanonicalFeatureAssembler,
    HistoricalFeatureSet,
    ModelInputVector,
    _json_compatible,
    _validate_historical_type,
)
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.errors import FeatureContractError, MissingFeatureError
from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS
from tests.inference.test_service_and_app import EVENT_TIME, historical, payload


INTEGER_FEATURE = "user_behavior__user_tx_count_1h"
FLOAT_FEATURE = "user_behavior__user_amount_sum_1h"
NULLABLE_FEATURE = "user_behavior__user_avg_amount_30d"
MERCHANT_FEATURE = "merchant_behavior__merchant_tx_count_1h"
MERCHANT_COLUMNS = tuple(
    name for name in HISTORICAL_FEATURE_COLUMNS if name.startswith("merchant_behavior__")
)


def request(**overrides) -> FraudPredictionRequest:
    """Build a validated request from the default payload."""
    return FraudPredictionRequest.model_validate(payload(**overrides))


def no_merchant_request() -> FraudPredictionRequest:
    """Build a request that legitimately has no merchant."""
    return request(type="withdraw", merchant_id=None, channel="atm")


def features(*, values=None, timestamps=None, drop=()) -> HistoricalFeatureSet:
    """Copy the default feature set with overrides and dropped columns."""
    base = historical()
    merged_values = {**base.values, **(values or {})}
    merged_timestamps = {**base.timestamps, **(timestamps or {})}
    for name in drop:
        merged_values.pop(name, None)
        merged_timestamps.pop(name, None)
    return HistoricalFeatureSet(values=merged_values, timestamps=merged_timestamps)


def no_merchant_features() -> dict[str, dict]:
    """Return the null merchant fields the adapter produces."""
    return {
        "values": {name: None for name in MERCHANT_COLUMNS},
        "timestamps": {name: None for name in MERCHANT_COLUMNS},
    }


class UnknownOffset(tzinfo):
    """A timezone object that is attached but cannot state its offset."""

    def utcoffset(self, value):
        """Report that the offset is unknown."""
        return None

    def dst(self, value):
        """Report that daylight saving is unknown."""
        return None

    def tzname(self, value):
        """Name the unknown zone."""
        return "unknown"


class HistoricalFeatureSetTest(unittest.TestCase):
    def test_contents_are_read_only_copies_of_the_inputs(self):
        values = {"a": 1}
        timestamps = {"a": EVENT_TIME}
        feature_set = HistoricalFeatureSet(values=values, timestamps=timestamps)
        values["a"] = 2
        timestamps["a"] = None
        self.assertEqual(1, feature_set.values["a"])
        self.assertEqual(EVENT_TIME, feature_set.timestamps["a"])
        with self.assertRaises(TypeError):
            feature_set.values["a"] = 3
        with self.assertRaises(TypeError):
            feature_set.timestamps["a"] = None


class ModelInputVectorTest(unittest.TestCase):
    def test_rejects_reordered_columns_and_wrong_arity(self):
        values = (1.0,) * 41
        with self.assertRaises(FeatureContractError):
            ModelInputVector(tuple(reversed(MODEL_INPUT_COLUMNS)), values, EVENT_TIME)
        with self.assertRaises(FeatureContractError):
            ModelInputVector(MODEL_INPUT_COLUMNS, values[:-1], EVENT_TIME)

    def test_as_dict_pairs_every_column_with_its_value(self):
        values = tuple(range(41))
        vector = ModelInputVector(MODEL_INPUT_COLUMNS, values, EVENT_TIME)
        self.assertEqual(dict(zip(MODEL_INPUT_COLUMNS, values)), vector.as_dict())


class HistoricalTypeValidationTest(unittest.TestCase):
    def test_accepts_canonical_numeric_types(self):
        self.assertEqual(3, _validate_historical_type(INTEGER_FEATURE, np.int64(3)))
        self.assertEqual(2.5, _validate_historical_type(FLOAT_FEATURE, 2.5))
        self.assertIsInstance(_validate_historical_type(FLOAT_FEATURE, 2), float)

    def test_rejects_values_outside_the_frozen_types(self):
        cases = (
            (INTEGER_FEATURE, True),
            (FLOAT_FEATURE, False),
            (INTEGER_FEATURE, 1.0),
            (INTEGER_FEATURE, "1"),
            (FLOAT_FEATURE, "1.0"),
            (FLOAT_FEATURE, math.nan),
            (FLOAT_FEATURE, math.inf),
        )
        for column, value in cases:
            with self.subTest(column=column, value=value):
                with self.assertRaises(FeatureContractError):
                    _validate_historical_type(column, value)


class JsonCompatibilityTest(unittest.TestCase):
    def test_numpy_and_missing_values_become_plain_json_values(self):
        cases = (
            (None, None),
            (math.nan, None),
            (math.inf, None),
            (pd.NA, None),
            (pd.NaT, None),
            (np.bool_(True), True),
            (np.int64(4), 4),
            (np.float64(1.5), 1.5),
            ("payment", "payment"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                result = _json_compatible(value)
                self.assertEqual(expected, result)
                self.assertNotIsInstance(result, np.generic)

    def test_array_like_values_pass_through_unchanged(self):
        self.assertEqual([1, 2], _json_compatible([1, 2]))


class AssembleContractTest(unittest.TestCase):
    def setUp(self):
        self.assembler = CanonicalFeatureAssembler()

    def test_unknown_or_absent_columns_violate_the_contract(self):
        extra = features(values={"user_behavior__ssn": 1})
        missing_column = features(drop=(INTEGER_FEATURE,))
        timestamp_gap = HistoricalFeatureSet(
            values=historical().values,
            timestamps={
                name: value
                for name, value in historical().timestamps.items()
                if name != INTEGER_FEATURE
            },
        )
        for case in (extra, missing_column, timestamp_gap):
            with self.assertRaises(FeatureContractError):
                self.assembler.assemble(request(), case)

    def test_absent_merchant_must_not_receive_merchant_values(self):
        fields = no_merchant_features()
        for override in (
            {"values": {**fields["values"], MERCHANT_FEATURE: 1}},
            {"timestamps": {**fields["timestamps"], MERCHANT_FEATURE: EVENT_TIME}},
        ):
            with self.subTest(override=list(override)):
                with self.assertRaises(FeatureContractError):
                    self.assembler.assemble(
                        no_merchant_request(), features(**{**fields, **override})
                    )

    def test_absent_merchant_is_null_in_the_model_vector(self):
        vector = self.assembler.assemble(
            no_merchant_request(), features(**no_merchant_features())
        )
        as_dict = vector.as_dict()
        self.assertFalse(as_dict["merchant_present"])
        for column in (*MERCHANT_COLUMNS, "merchant_activity_rate_ratio_10m_vs_24h"):
            self.assertIsNone(as_dict[column])

    def test_null_is_missing_only_for_non_nullable_columns(self):
        with self.assertRaises(MissingFeatureError) as raised:
            self.assembler.assemble(
                request(), features(values={INTEGER_FEATURE: None})
            )
        self.assertEqual((INTEGER_FEATURE,), raised.exception.feature_names)
        vector = self.assembler.assemble(
            request(), features(values={NULLABLE_FEATURE: None})
        )
        self.assertIsNone(vector.as_dict()[NULLABLE_FEATURE])

    def test_every_missing_column_is_reported_in_contract_order(self):
        second = "account_behavior__account_tx_count_1h"
        with self.assertRaises(MissingFeatureError) as raised:
            self.assembler.assemble(
                request(),
                features(values={second: None, INTEGER_FEATURE: None}),
            )
        self.assertEqual((INTEGER_FEATURE, second), raised.exception.feature_names)

    def test_absent_timestamp_is_missing_even_for_a_nullable_column(self):
        with self.assertRaises(MissingFeatureError):
            self.assembler.assemble(
                request(), features(timestamps={NULLABLE_FEATURE: None})
            )

    def test_timestamps_without_a_usable_offset_violate_the_contract(self):
        for value in (
            datetime(2026, 10, 29, 11, 58),
            datetime(2026, 10, 29, 11, 58, tzinfo=UnknownOffset()),
        ):
            with self.subTest(value=value):
                with self.assertRaises(FeatureContractError):
                    self.assembler.assemble(
                        request(), features(timestamps={INTEGER_FEATURE: value})
                    )

    def test_feature_timestamp_is_the_oldest_contributing_timestamp(self):
        oldest = EVENT_TIME - timedelta(minutes=9)
        vector = self.assembler.assemble(
            request(), features(timestamps={FLOAT_FEATURE: oldest})
        )
        self.assertEqual(oldest, vector.feature_timestamp)

    def test_timezone_offsets_are_compared_as_instants(self):
        offset = timezone(timedelta(hours=7))
        earlier = (EVENT_TIME - timedelta(minutes=5)).astimezone(offset)
        vector = self.assembler.assemble(
            request(), features(timestamps={FLOAT_FEATURE: earlier})
        )
        self.assertEqual(earlier, vector.feature_timestamp)


if __name__ == "__main__":
    unittest.main()
