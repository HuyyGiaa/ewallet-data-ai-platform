"""KServe client guards and the urllib transport's failure classification."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import io
import json
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from ai_platform.inference.contracts import MODEL_INPUT_COLUMNS
from ai_platform.inference.errors import (
    ModelBackendError,
    ModelResponseError,
    ModelSignatureError,
    ModelTimeoutError,
)
from ai_platform.inference.kserve import KServeV2Client, UrllibJsonTransport
from ai_platform.inference.observability import NoopBackendTelemetry
from ai_platform.inference.schemas import ModelPrediction
from ai_platform.inference.wire import FraudV2WireContract
from tests.inference.support import (
    FAKE_KSERVE_URL,
    MODEL_NAME,
    MODEL_VERSION,
    DeterministicModelTransport,
    model_metadata,
)
from tests.inference.test_wire_contract import sample_vector


URL = f"{FAKE_KSERVE_URL}/v2/models/{MODEL_NAME}"


class ScriptedTransport(DeterministicModelTransport):
    """Deterministic transport with optional replacement responses."""

    def __init__(self, *, metadata=None, infer=None, infer_error=None):
        super().__init__()
        self.metadata = metadata
        self.infer = infer
        self.infer_error = infer_error
        self.get_urls = []
        self.headers = None

    async def get_json(self, url, *, timeout_seconds):
        """Record the URL and answer with scripted metadata if set."""
        self.get_urls.append(url)
        if self.metadata is not None and not url.endswith("/ready"):
            return self.metadata
        return await super().get_json(url, timeout_seconds=timeout_seconds)

    async def post_json(self, url, payload, *, timeout_seconds, headers=None):
        """Record the headers and answer as scripted."""
        self.headers = dict(headers or {})
        if self.infer_error is not None:
            raise self.infer_error
        if self.infer is not None:
            return self.infer
        return await super().post_json(
            url, payload, timeout_seconds=timeout_seconds, headers=headers
        )


def client(transport=None, *, wire_contract=None, timeout_seconds=1.0):
    """Build a client around the real wire contract."""
    return KServeV2Client(
        base_url=FAKE_KSERVE_URL,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        timeout_seconds=timeout_seconds,
        transport=transport or ScriptedTransport(),
        wire_contract=wire_contract or FraudV2WireContract(),
        backend_telemetry=NoopBackendTelemetry(),
    )


def predict(model_client, correlation_id="corr-1"):
    """Run one prediction for the valid sample vector."""
    return asyncio.run(
        model_client.predict(sample_vector(), correlation_id=correlation_id)
    )


class ClientConstructionTest(unittest.TestCase):
    def test_invalid_configuration_is_rejected(self):
        valid = {
            "base_url": FAKE_KSERVE_URL,
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "timeout_seconds": 1.0,
        }
        for override in (
            {"base_url": "ftp://kserve.invalid"},
            {"model_name": " "},
            {"model_version": " "},
            {"timeout_seconds": 0},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    KServeV2Client(**{**valid, **override})

    def test_model_name_is_escaped_in_the_route(self):
        model_client = KServeV2Client(
            base_url=FAKE_KSERVE_URL + "/",
            model_name="team/model",
            model_version="1",
            timeout_seconds=1.0,
            backend_telemetry=NoopBackendTelemetry(),
        )
        self.assertEqual(
            f"{FAKE_KSERVE_URL}/v2/models/team%2Fmodel", model_client._model_url
        )


class WireContractRequirementTest(unittest.TestCase):
    def assert_contract_rejected(self, contract):
        model_client = KServeV2Client(
            base_url=FAKE_KSERVE_URL,
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            timeout_seconds=1.0,
            transport=ScriptedTransport(),
            wire_contract=contract,
            backend_telemetry=NoopBackendTelemetry(),
        )
        with self.assertRaises(ModelSignatureError):
            asyncio.run(model_client.ready())

    def test_absent_contract_is_rejected(self):
        self.assert_contract_rejected(None)

    def test_incomplete_contract_object_is_rejected(self):
        self.assert_contract_rejected(object())

    def test_contract_for_another_schema_is_rejected(self):
        class OtherSchema(FraudV2WireContract):
            input_columns = MODEL_INPUT_COLUMNS[:-1]

        self.assert_contract_rejected(OtherSchema())

    def test_nullable_columns_must_be_a_frozen_subset(self):
        class MutableNullable(FraudV2WireContract):
            nullable_input_columns = {"amount"}

        class UnknownNullable(FraudV2WireContract):
            nullable_input_columns = frozenset({"not_a_column"})

        self.assert_contract_rejected(MutableNullable())
        self.assert_contract_rejected(UnknownNullable())


class ReadinessTest(unittest.TestCase):
    def test_readiness_checks_metadata_then_the_ready_route(self):
        transport = ScriptedTransport()
        self.assertTrue(asyncio.run(client(transport).ready()))
        self.assertEqual([URL, URL + "/ready"], transport.get_urls)

    def test_metadata_identity_is_enforced(self):
        wrong_version = model_metadata(model_version="8")
        malformed_versions = {**model_metadata(), "versions": "7"}
        cases = (
            [],
            model_metadata(model_name="other"),
            wrong_version,
            malformed_versions,
        )
        for metadata in cases:
            with self.subTest(metadata=str(metadata)[:40]):
                with self.assertRaises(ModelSignatureError):
                    asyncio.run(client(ScriptedTransport(metadata=metadata)).ready())

    def test_null_versions_defer_version_checks_to_each_response(self):
        metadata = {**model_metadata(), "versions": None}
        self.assertTrue(
            asyncio.run(client(ScriptedTransport(metadata=metadata)).ready())
        )

    def test_unexpected_contract_exception_is_a_signature_error(self):
        metadata = model_metadata()
        metadata["inputs"] = ["not-an-object"]
        with self.assertRaises(ModelSignatureError):
            asyncio.run(client(ScriptedTransport(metadata=metadata)).ready())


class PredictionGuardTest(unittest.TestCase):
    def test_signature_is_validated_only_on_the_first_prediction(self):
        transport = ScriptedTransport()
        model_client = client(transport)
        predict(model_client)
        predict(model_client)
        self.assertEqual(2, len(transport.get_urls))

    def test_correlation_id_is_sent_with_the_request(self):
        transport = ScriptedTransport()
        predict(client(transport), "corr-9")
        self.assertEqual("corr-9", transport.headers["x-correlation-id"])

    def test_prediction_succeeds_without_opentelemetry(self):
        model_client = client()
        asyncio.run(model_client.ready())
        with patch.dict(sys.modules, {"opentelemetry": None}):
            prediction = predict(model_client)
        self.assertIsInstance(prediction, ModelPrediction)

    def test_null_in_a_required_column_never_reaches_the_model(self):
        transport = ScriptedTransport()
        with self.assertRaises(ModelSignatureError):
            asyncio.run(
                client(transport).predict(
                    replace(sample_vector(), values=(None, *sample_vector().values[1:])),
                    correlation_id="corr-1",
                )
            )
        self.assertIsNone(transport.headers)

    def test_encoder_exception_becomes_a_signature_error(self):
        class BrokenEncoder(FraudV2WireContract):
            def encode_request(self, vector, *, correlation_id):
                raise KeyError("column")

        with self.assertRaises(ModelSignatureError):
            predict(client(wire_contract=BrokenEncoder()))

    def test_encoder_signature_error_is_preserved(self):
        error = ModelSignatureError("specific")

        class RejectingEncoder(FraudV2WireContract):
            def encode_request(self, vector, *, correlation_id):
                raise error

        with self.assertRaises(ModelSignatureError) as raised:
            predict(client(wire_contract=RejectingEncoder()))
        self.assertIs(error, raised.exception)

    def test_payload_must_echo_the_correlation_id(self):
        class WrongId(FraudV2WireContract):
            def encode_request(self, vector, *, correlation_id):
                return {"id": "other"}

        class NotAnObject(FraudV2WireContract):
            def encode_request(self, vector, *, correlation_id):
                return ["payload"]

        for contract in (WrongId(), NotAnObject()):
            with self.subTest(contract=type(contract).__name__):
                with self.assertRaises(ModelSignatureError):
                    predict(client(wire_contract=contract))


class BackendCallTest(unittest.TestCase):
    def test_transport_errors_are_classified(self):
        cases = (
            (TimeoutError("slow"), ModelTimeoutError),
            (asyncio.TimeoutError(), ModelTimeoutError),
            (ConnectionError("down"), ModelBackendError),
            (ModelTimeoutError(), ModelTimeoutError),
            (ModelBackendError(), ModelBackendError),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(expected):
                    predict(client(ScriptedTransport(infer_error=error)))

    def test_deadline_is_enforced_by_the_client(self):
        class Hanging(ScriptedTransport):
            async def post_json(self, url, payload, *, timeout_seconds, headers=None):
                await asyncio.Event().wait()

        with self.assertRaises(ModelTimeoutError):
            predict(client(Hanging(), timeout_seconds=0.01))


class ResponseIdentityTest(unittest.TestCase):
    def valid_response(self):
        return asyncio.run(
            DeterministicModelTransport().post_json(
                URL, {"id": "corr-1", "inputs": []}, timeout_seconds=1.0
            )
        )

    def test_response_must_identify_this_model_and_request(self):
        base = self.valid_response()
        cases = (
            ["not-an-object"],
            {**base, "model_name": "other"},
            {**base, "model_version": "8"},
            {**base, "id": "corr-other"},
        )
        for response in cases:
            with self.subTest(response=str(response)[:40]):
                with self.assertRaises(ModelResponseError):
                    predict(client(ScriptedTransport(infer=response)))

    def test_decoder_results_are_verified(self):
        class WrongModel(FraudV2WireContract):
            def decode_response(self, response, **kwargs):
                return ModelPrediction(
                    fraud_probability=0.1,
                    fraud_prediction=0,
                    model_name="other",
                    model_version=MODEL_VERSION,
                )

        class WrongVersion(FraudV2WireContract):
            def decode_response(self, response, **kwargs):
                return ModelPrediction(
                    fraud_probability=0.1,
                    fraud_prediction=0,
                    model_name=MODEL_NAME,
                    model_version="8",
                )

        class Untyped(FraudV2WireContract):
            def decode_response(self, response, **kwargs):
                return {"fraud_probability": 0.1}

        class Crashing(FraudV2WireContract):
            def decode_response(self, response, **kwargs):
                raise KeyError("outputs")

        for contract in (WrongModel(), WrongVersion(), Untyped(), Crashing()):
            with self.subTest(contract=type(contract).__name__):
                with self.assertRaises(ModelResponseError):
                    predict(
                        client(
                            ScriptedTransport(infer=self.valid_response()),
                            wire_contract=contract,
                        )
                    )

    def test_decoder_response_error_is_preserved(self):
        error = ModelResponseError("specific")

        class Rejecting(FraudV2WireContract):
            def decode_response(self, response, **kwargs):
                raise error

        with self.assertRaises(ModelResponseError) as raised:
            predict(
                client(
                    ScriptedTransport(infer=self.valid_response()),
                    wire_contract=Rejecting(),
                )
            )
        self.assertIs(error, raised.exception)


class FakeHttpResponse(io.BytesIO):
    """``urlopen`` context manager returning a fixed body."""


class UrllibTransportTest(unittest.TestCase):
    def request(self, *, body=b"{}", error=None):
        with patch("ai_platform.inference.kserve.urlopen") as urlopen:
            if error is not None:
                urlopen.side_effect = error
            else:
                urlopen.return_value = FakeHttpResponse(body)
            result = asyncio.run(
                UrllibJsonTransport().post_json(
                    URL,
                    {"id": "corr-1"},
                    timeout_seconds=1.5,
                    headers={"x-correlation-id": "corr-1"},
                )
            )
        return result, urlopen

    def test_post_serializes_json_with_bounded_timeout_and_headers(self):
        result, urlopen = self.request(body=b'{"ok":true}')
        self.assertEqual({"ok": True}, result)
        request = urlopen.call_args.args[0]
        self.assertEqual("POST", request.get_method())
        self.assertEqual({"id": "corr-1"}, json.loads(request.data))
        self.assertEqual("application/json", request.get_header("Content-type"))
        self.assertEqual("application/json", request.get_header("Accept"))
        self.assertEqual("corr-1", request.get_header("X-correlation-id"))
        self.assertEqual(1.5, urlopen.call_args.kwargs["timeout"])

    def test_get_sends_no_body_or_content_type(self):
        with patch("ai_platform.inference.kserve.urlopen") as urlopen:
            urlopen.return_value = FakeHttpResponse(b"{}")
            asyncio.run(UrllibJsonTransport().get_json(URL, timeout_seconds=1.0))
        request = urlopen.call_args.args[0]
        self.assertIsNone(request.data)
        self.assertIsNone(request.get_header("Content-type"))

    def test_non_finite_payload_is_refused_before_any_request(self):
        with patch("ai_platform.inference.kserve.urlopen") as urlopen:
            with self.assertRaises(ValueError):
                asyncio.run(
                    UrllibJsonTransport().post_json(
                        URL, {"value": float("nan")}, timeout_seconds=1.0
                    )
                )
        urlopen.assert_not_called()

    def test_empty_body_is_an_empty_object(self):
        result, _ = self.request(body=b"")
        self.assertEqual({}, result)

    def test_timeouts_stay_timeouts(self):
        for error in (TimeoutError("slow"), URLError(TimeoutError("slow"))):
            with self.subTest(error=repr(error)):
                with self.assertRaises(TimeoutError):
                    self.request(error=error)

    def test_transport_failures_are_sanitized(self):
        http_error = HTTPError(URL, 503, "secret-detail", None, io.BytesIO())
        self.addCleanup(http_error.close)
        for error in (http_error, URLError("secret-detail"), OSError("secret-detail")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(RuntimeError) as raised:
                    self.request(error=error)
                self.assertNotIn("secret-detail", str(raised.exception))

    def test_invalid_response_bodies_are_rejected(self):
        for body in (b"{not-json", b"\xff\xfe", b"[1, 2]"):
            with self.subTest(body=body):
                with self.assertRaises(RuntimeError):
                    self.request(body=body)


if __name__ == "__main__":
    unittest.main()
