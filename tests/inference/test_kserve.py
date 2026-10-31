from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from ai_platform.inference.assembler import ModelInputVector
from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.errors import (
    ModelBackendError,
    ModelResponseError,
    ModelSignatureError,
    ModelTimeoutError,
)
from ai_platform.inference.kserve import KServeV2Client, UrllibJsonTransport
from ai_platform.inference.schemas import ModelPrediction


MODEL_NAME = "ewallet-fraud-classifier"
MODEL_VERSION = "7"


def vector(*, null_columns: frozenset[str] = frozenset()) -> ModelInputVector:
    values = tuple(
        None if column in null_columns else 1.5 for column in MODEL_INPUT_COLUMNS
    )
    return ModelInputVector(
        columns=MODEL_INPUT_COLUMNS,
        values=values,
        feature_timestamp=datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc),
    )


def metadata(*, include_versions: bool = True):
    result = {
        "name": MODEL_NAME,
        "platform": "mlflow",
        "inputs": [{"name": "reviewed-frame", "datatype": "BYTES", "shape": [-1]}],
        "outputs": [
            {"name": "reviewed-prediction", "datatype": "BYTES", "shape": [-1]}
        ],
    }
    if include_versions:
        result["versions"] = [MODEL_VERSION]
    return result


def prediction_response(correlation_id="corr-1"):
    return {
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "id": correlation_id,
        "outputs": [{"name": "reviewed-prediction", "data": [[0.75, 1]]}],
    }


class ReviewedTestWireContract:
    """Test double; deliberately not a claimed production artifact layout."""

    input_columns = MODEL_INPUT_COLUMNS

    def __init__(self, nullable_input_columns=frozenset()):
        self.nullable_input_columns = frozenset(nullable_input_columns)

    def validate_metadata(self, observed):
        expected = metadata()
        if observed.get("platform") != "mlflow":
            raise ModelSignatureError()
        if observed.get("inputs") != expected["inputs"]:
            raise ModelSignatureError()
        if observed.get("outputs") != expected["outputs"]:
            raise ModelSignatureError()

    def encode_request(self, model_vector, *, correlation_id):
        return {
            "id": correlation_id,
            "inputs": [
                {
                    "name": "reviewed-frame",
                    "datatype": "BYTES",
                    "shape": [1],
                    "data": [model_vector.as_dict()],
                }
            ],
        }

    def decode_response(
        self, response, *, model_name, model_version, correlation_id
    ):
        try:
            output = response["outputs"]
            if len(output) != 1 or output[0]["name"] != "reviewed-prediction":
                raise ValueError
            probability, decision = output[0]["data"][0]
            return ModelPrediction(
                fraud_probability=probability,
                fraud_prediction=decision,
                model_name=model_name,
                model_version=model_version,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelResponseError() from exc


class FakeTransport:
    def __init__(self, *, metadata_payload=None, prediction_payload=None):
        self.metadata_payload = metadata_payload or metadata()
        self.prediction_payload = prediction_payload or prediction_response()
        self.get_calls = []
        self.post_calls = []
        self.get_error = None
        self.post_error = None

    async def get_json(self, url, *, timeout_seconds):
        self.get_calls.append((url, timeout_seconds))
        if self.get_error:
            raise self.get_error
        if url.endswith("/ready"):
            return {}
        return self.metadata_payload

    async def post_json(self, url, payload, *, timeout_seconds, headers=None):
        self.post_calls.append((url, payload, timeout_seconds, headers))
        if self.post_error:
            raise self.post_error
        return self.prediction_payload


class NeverReturnsTransport(FakeTransport):
    async def post_json(self, url, payload, *, timeout_seconds, headers=None):
        await asyncio.sleep(10)


class KServeV2ClientTest(unittest.TestCase):
    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def client(self, transport, timeout=0.1, wire_contract=None):
        return KServeV2Client(
            base_url="http://kserve.internal",
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            timeout_seconds=timeout,
            transport=transport,
            wire_contract=wire_contract or ReviewedTestWireContract(),
        )

    def test_missing_registered_artifact_wire_contract_fails_before_remote_call(self):
        transport = FakeTransport()
        client = KServeV2Client(
            base_url="http://kserve.internal",
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            timeout_seconds=0.1,
            transport=transport,
        )
        with self.assertRaises(ModelSignatureError):
            self.run_async(client.ready())
        self.assertEqual([], transport.get_calls)

    def test_ready_accepts_optional_versions_on_exact_version_scoped_url(self):
        transport = FakeTransport(metadata_payload=metadata(include_versions=False))
        self.assertTrue(self.run_async(self.client(transport).ready()))
        self.assertIn(
            "/v2/models/ewallet-fraud-classifier/versions/7",
            transport.get_calls[0][0],
        )
        self.assertTrue(transport.get_calls[1][0].endswith("/ready"))

    def test_present_version_and_model_identity_remain_strict(self):
        for mutation in ("version", "model", "layout"):
            changed = metadata()
            if mutation == "version":
                changed["versions"] = ["8"]
            elif mutation == "model":
                changed["name"] = "another-model"
            else:
                changed["inputs"] = []
            with self.subTest(mutation=mutation):
                with self.assertRaises(ModelSignatureError):
                    self.run_async(
                        self.client(FakeTransport(metadata_payload=changed)).ready()
                    )

    def test_payload_layout_is_owned_by_injected_reviewed_contract(self):
        transport = FakeTransport()
        result = self.run_async(
            self.client(transport).predict(vector(), correlation_id="corr-1")
        )
        self.assertEqual(0.75, result.fraud_probability)
        payload = transport.post_calls[0][1]
        self.assertEqual(
            ["reviewed-frame"],
            [item["name"] for item in payload["inputs"]],
        )
        self.assertEqual(
            list(MODEL_INPUT_COLUMNS),
            list(payload["inputs"][0]["data"][0]),
        )

    def test_undefined_ratio_and_nullable_merchant_fail_closed_without_declaration(self):
        for column in (
            "amount_ratio_to_user_avg_30d",
            "merchant_behavior__merchant_tx_count_10m",
        ):
            transport = FakeTransport()
            with self.subTest(column=column):
                with self.assertRaises(ModelSignatureError):
                    self.run_async(
                        self.client(transport).predict(
                            vector(null_columns=frozenset({column})),
                            correlation_id="corr-1",
                        )
                    )
                self.assertEqual([], transport.post_calls)

    def test_explicit_nullable_declaration_preserves_null_without_zero_imputation(self):
        column = "amount_ratio_to_user_avg_30d"
        transport = FakeTransport()
        contract = ReviewedTestWireContract(frozenset({column}))
        self.run_async(
            self.client(transport, wire_contract=contract).predict(
                vector(null_columns=frozenset({column})), correlation_id="corr-1"
            )
        )
        encoded = transport.post_calls[0][1]["inputs"][0]["data"][0]
        self.assertIsNone(encoded[column])

    def test_response_identity_and_wire_output_are_validated(self):
        cases = []
        wrong_model = prediction_response()
        wrong_model["model_name"] = "wrong"
        cases.append(wrong_model)
        wrong_version = prediction_response()
        wrong_version["model_version"] = "8"
        cases.append(wrong_version)
        cases.append(prediction_response(correlation_id="other"))
        missing_output = prediction_response()
        missing_output["outputs"] = []
        cases.append(missing_output)
        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(ModelResponseError):
                    self.run_async(
                        self.client(FakeTransport(prediction_payload=response)).predict(
                            vector(), correlation_id="corr-1"
                        )
                    )

    def test_response_model_version_is_optional_but_strict_when_present(self):
        response = prediction_response()
        response.pop("model_version")
        result = self.run_async(
            self.client(FakeTransport(prediction_payload=response)).predict(
                vector(), correlation_id="corr-1"
            )
        )
        self.assertEqual(MODEL_VERSION, result.model_version)

    def test_timeout_and_backend_failure_are_distinct(self):
        with self.assertRaises(ModelTimeoutError):
            self.run_async(
                self.client(NeverReturnsTransport(), timeout=0.001).predict(
                    vector(), correlation_id="corr-1"
                )
            )
        transport = FakeTransport()
        transport.post_error = RuntimeError("token=secret")
        with self.assertRaises(ModelBackendError) as captured:
            self.run_async(
                self.client(transport).predict(
                    vector(), correlation_id="corr-1"
                )
            )
        self.assertNotIn("secret", str(captured.exception))

    def test_transport_socket_timeout_is_classified_as_model_timeout(self):
        with patch("ai_platform.inference.kserve.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(ModelTimeoutError):
                self.run_async(
                    self.client(UrllibJsonTransport()).predict(
                        vector(), correlation_id="corr-1"
                    )
                )

    def test_metadata_is_cached_after_successful_compatibility_check(self):
        transport = FakeTransport()
        client = self.client(transport)
        self.run_async(client.ready())
        self.run_async(client.predict(vector(), correlation_id="corr-1"))
        self.assertEqual(2, len(transport.get_calls))
        self.assertEqual(1, len(transport.post_calls))

    def test_current_w3c_trace_context_is_injected_into_remote_call(self):
        transport = FakeTransport()
        def inject(carrier):
            carrier["traceparent"] = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        with patch("opentelemetry.propagate.inject", side_effect=inject):
            self.run_async(self.client(transport).predict(vector(), correlation_id="corr-1"))
        self.assertIn("traceparent", transport.post_calls[0][3])


if __name__ == "__main__":
    unittest.main()
