"""The ``ewallet-fraud-v2-wire-v1`` encoding, metadata and decoding rules."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import unittest

from ai_platform.inference.assembler import ModelInputVector
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS, MODEL_SCHEMA_SHA256
from ai_platform.inference.errors import ModelResponseError, ModelSignatureError
from ai_platform.inference.wire import (
    NULLABLE_INPUT_COLUMNS,
    WIRE_CONTRACT_ID,
    FraudV2WireContract,
    _encode,
    request_parameters,
)
from pydantic import ValidationError
from tests.inference.support import (
    MODEL_NAME,
    MODEL_VERSION,
    model_metadata,
    v2_input_datatype,
)


REQUEST_TIME_COLUMNS = ("amount", "type", "channel", "merchant_present")


def ready_contract() -> FraudV2WireContract:
    """Return a contract that has learned the V2 datatypes."""
    contract = FraudV2WireContract()
    contract.validate_metadata(model_metadata())
    return contract


def sample_vector() -> ModelInputVector:
    """Build a vector whose values suit every column's datatype."""
    values = []
    for column in MODEL_INPUT_COLUMNS:
        if column in ("type", "channel"):
            values.append("payment" if column == "type" else "app")
        elif column == "merchant_present":
            values.append(True)
        else:
            values.append(1)
    return ModelInputVector(
        MODEL_INPUT_COLUMNS,
        tuple(values),
        datetime(2026, 10, 29, 11, 58, tzinfo=timezone.utc),
    )


def v2_response(probability=0.25, prediction=0) -> dict[str, object]:
    """Build a V2 response body with the two contract outputs."""
    return {
        "outputs": [
            {"name": "fraud_probability", "datatype": "FP64", "data": [probability]},
            {"name": "fraud_prediction", "datatype": "INT64", "data": [prediction]},
        ]
    }


def decode(contract, response):
    """Decode ``response`` for the shared model identity."""
    return contract.decode_response(
        response,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        correlation_id="corr-1",
    )


class RequestParameterTest(unittest.TestCase):
    def test_requests_identify_the_wire_and_source_schema(self):
        self.assertEqual(
            {
                "wire_contract": WIRE_CONTRACT_ID,
                "feature_schema_sha256": MODEL_SCHEMA_SHA256,
            },
            request_parameters(),
        )

    def test_request_time_features_are_never_nullable(self):
        self.assertFalse(NULLABLE_INPUT_COLUMNS.intersection(REQUEST_TIME_COLUMNS))
        self.assertEqual(37, len(NULLABLE_INPUT_COLUMNS))


class ValueEncodingTest(unittest.TestCase):
    def test_values_are_coerced_to_their_declared_datatype(self):
        cases = (
            (None, "FP64", None),
            ("payment", "BYTES", "payment"),
            (True, "BOOL", True),
            (1, "BOOL", True),
            (0, "BOOL", False),
            (3.0, "INT64", 3),
            (3, "INT32", 3),
            (2, "FP32", 2.0),
            (2.5, "FP64", 2.5),
        )
        for value, datatype, expected in cases:
            with self.subTest(value=value, datatype=datatype):
                encoded = _encode(value, datatype)
                self.assertEqual(expected, encoded)
                self.assertIs(type(expected), type(encoded))

    def test_values_incompatible_with_the_datatype_are_rejected(self):
        cases = (
            (math.nan, "FP64"),
            (math.inf, "INT64"),
            (1, "BYTES"),
            (2, "BOOL"),
            (True, "FP64"),
            (True, "INT64"),
            ("1", "INT64"),
            (1.5, "INT64"),
        )
        for value, datatype in cases:
            with self.subTest(value=value, datatype=datatype):
                with self.assertRaises(ModelSignatureError):
                    _encode(value, datatype)


class MetadataValidationTest(unittest.TestCase):
    def assert_rejected(self, metadata):
        with self.assertRaises(ModelSignatureError):
            FraudV2WireContract().validate_metadata(metadata)

    def test_non_list_sections_are_rejected(self):
        self.assert_rejected({"inputs": None, "outputs": []})
        self.assert_rejected({"inputs": [], "outputs": None})

    def test_reordered_or_incomplete_inputs_are_rejected(self):
        metadata = model_metadata()
        metadata["inputs"] = list(reversed(metadata["inputs"]))
        self.assert_rejected(metadata)
        metadata = model_metadata()
        metadata["inputs"] = metadata["inputs"][:-1]
        self.assert_rejected(metadata)

    def test_unknown_datatype_or_shape_is_rejected(self):
        metadata = model_metadata()
        metadata["inputs"][0]["datatype"] = "FP16"
        self.assert_rejected(metadata)
        metadata = model_metadata()
        metadata["inputs"][0]["shape"] = [1]
        self.assert_rejected(metadata)

    def test_unexpected_output_signature_is_rejected(self):
        metadata = model_metadata()
        metadata["outputs"][0]["datatype"] = "FP32"
        self.assert_rejected(metadata)
        metadata = model_metadata()
        metadata["outputs"] = list(reversed(metadata["outputs"]))
        self.assert_rejected(metadata)


class RequestEncodingTest(unittest.TestCase):
    def test_encoding_requires_validated_metadata(self):
        with self.assertRaises(ModelSignatureError):
            FraudV2WireContract().encode_request(
                sample_vector(), correlation_id="corr-1"
            )

    def test_every_column_is_one_named_single_value_input_in_order(self):
        encoded = ready_contract().encode_request(
            sample_vector(), correlation_id="corr-1"
        )
        self.assertEqual("corr-1", encoded["id"])
        self.assertEqual(request_parameters(), encoded["parameters"])
        self.assertEqual(
            list(MODEL_INPUT_COLUMNS), [item["name"] for item in encoded["inputs"]]
        )
        for item, value in zip(encoded["inputs"], sample_vector().values):
            self.assertEqual([1], item["shape"])
            self.assertEqual(v2_input_datatype(item["name"]), item["datatype"])
            self.assertEqual([value], item["data"])

    def test_vector_with_mutated_columns_is_rejected(self):
        vector = sample_vector()
        object.__setattr__(vector, "columns", tuple(reversed(MODEL_INPUT_COLUMNS)))
        with self.assertRaises(ModelSignatureError):
            ready_contract().encode_request(vector, correlation_id="corr-1")


class ResponseDecodingTest(unittest.TestCase):
    def test_valid_response_becomes_a_typed_prediction(self):
        prediction = decode(FraudV2WireContract(), v2_response(0.25, 0))
        self.assertEqual(0.25, prediction.fraud_probability)
        self.assertEqual(0, prediction.fraud_prediction)
        self.assertEqual(MODEL_NAME, prediction.model_name)
        self.assertEqual(MODEL_VERSION, prediction.model_version)

    def test_malformed_output_sections_are_rejected(self):
        extra = v2_response()
        extra["outputs"].append({"name": "label", "datatype": "BYTES", "data": ["x"]})
        wrong_type = v2_response()
        wrong_type["outputs"][0]["datatype"] = "FP32"
        two_rows = v2_response()
        two_rows["outputs"][0]["data"] = [0.1, 0.2]
        no_list = v2_response()
        no_list["outputs"][1]["data"] = 1
        cases = (
            {},
            {"outputs": "invalid"},
            {"outputs": ["not-an-object"]},
            {"outputs": v2_response()["outputs"][:1]},
            extra,
            wrong_type,
            two_rows,
            no_list,
        )
        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(ModelResponseError):
                    decode(FraudV2WireContract(), response)

    def test_out_of_contract_scores_are_rejected_by_the_schema(self):
        for probability, prediction in ((1.5, 1), (-0.1, 0), (math.nan, 0), (0.5, 2)):
            with self.subTest(probability=probability, prediction=prediction):
                with self.assertRaises(ValidationError):
                    decode(
                        FraudV2WireContract(), v2_response(probability, prediction)
                    )


if __name__ == "__main__":
    unittest.main()
