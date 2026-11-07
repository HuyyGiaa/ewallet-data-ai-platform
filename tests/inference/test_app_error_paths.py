"""HTTP error mapping, correlation handling and production wiring of the app.

Every failure is driven through the real adapters with an injected network
fault, so these tests pin the status, code and retry flag a client observes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_platform.inference.app import create_app, normalize_correlation_id
from ai_platform.inference.config import InferenceSettings
from ai_platform.inference.feast_online import FeastOnlineAdapter
from ai_platform.inference.kserve import KServeV2Client
from tests.inference.support import (
    FEATURE_BACKEND_ERROR,
    FEATURE_MISSING,
    FEATURE_STALE,
    FEATURE_TIMEOUT,
    MODEL_BACKEND_ERROR,
    MODEL_TIMEOUT,
    DeterministicModelTransport,
    EntityFeatureStore,
    build_fraud_service,
)
from tests.inference.test_service_and_app import (
    RecordingTelemetry,
    StubService,
    asgi_request,
    payload,
)


PREDICT = "/v1/fraud/predict"
FAULT_USER = "user-fault"
FAULT_CORRELATION = "corr-fault"


def tolerate_reraised_server_error(app):
    """Starlette re-raises after answering 500; keep the sent response."""

    async def wrapper(scope, receive, send):
        try:
            await app(scope, receive, send)
        except RuntimeError:
            pass

    return wrapper


class RaisingService(StubService):
    """Service failing with an exception outside the sanitized hierarchy."""

    async def predict(self, request, *, correlation_id):
        """Fail with an exception outside the sanitized hierarchy."""
        raise RuntimeError("secret-internal-detail")


class DependencyFailureMappingTest(unittest.TestCase):
    def post(self, *, store=None, transport=None):
        app = create_app(
            service=build_fraud_service(store=store, transport=transport),
            telemetry=RecordingTelemetry(),
        )
        return asyncio.run(
            asgi_request(
                app,
                "POST",
                PREDICT,
                payload(user_id=FAULT_USER),
                {"x-correlation-id": FAULT_CORRELATION},
            )
        )

    def assert_error(self, response, status, code, retryable):
        observed_status, headers, body = response
        self.assertEqual(status, observed_status)
        self.assertEqual(code, body["error"]["code"])
        self.assertEqual(retryable, body["error"]["retryable"])
        self.assertEqual(FAULT_CORRELATION, body["error"]["correlation_id"])
        self.assertEqual(FAULT_CORRELATION, headers["x-correlation-id"])

    def test_feature_failures_keep_their_status_and_retry_semantics(self):
        cases = (
            (FEATURE_BACKEND_ERROR, 503, "feature_backend_error", True),
            (FEATURE_TIMEOUT, 504, "feature_timeout", True),
            (FEATURE_MISSING, 424, "missing_feature", False),
            (FEATURE_STALE, 424, "stale_feature", False),
        )
        for fault, status, code, retryable in cases:
            with self.subTest(fault=fault):
                store = EntityFeatureStore(entity_faults={FAULT_USER: fault})
                self.assert_error(self.post(store=store), status, code, retryable)

    def test_model_failures_keep_their_status_and_retry_semantics(self):
        cases = (
            (MODEL_BACKEND_ERROR, 502, "model_backend_error", True),
            (MODEL_TIMEOUT, 504, "model_timeout", True),
        )
        for fault, status, code, retryable in cases:
            with self.subTest(fault=fault):
                transport = DeterministicModelTransport(
                    correlation_faults={FAULT_CORRELATION: fault}
                )
                self.assert_error(
                    self.post(transport=transport), status, code, retryable
                )

    def test_public_failure_message_hides_the_backend_exception(self):
        store = EntityFeatureStore(
            entity_faults={FAULT_USER: FEATURE_BACKEND_ERROR}
        )
        _, _, body = self.post(store=store)
        self.assertNotIn("synthetic", json.dumps(body))


class UnexpectedExceptionTest(unittest.TestCase):
    def test_unexpected_exception_is_sanitized_to_internal_error(self):
        telemetry = RecordingTelemetry()
        app = create_app(service=RaisingService(), telemetry=telemetry)
        status, _, body = asyncio.run(
            asgi_request(
                tolerate_reraised_server_error(app),
                "POST",
                PREDICT,
                payload(),
                {"x-correlation-id": "corr-500"},
            )
        )
        self.assertEqual(500, status)
        self.assertEqual("internal_error", body["error"]["code"])
        self.assertFalse(body["error"]["retryable"])
        self.assertEqual("corr-500", body["error"]["correlation_id"])
        self.assertNotIn("secret-internal-detail", json.dumps(body))
        self.assertIn(("error", "internal_error"), telemetry.events)


class RequestParsingTest(unittest.TestCase):
    def test_malformed_json_is_a_structured_validation_error(self):
        app = create_app(service=StubService(), telemetry=RecordingTelemetry())

        async def malformed():
            sent = []
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": PREDICT,
                "raw_path": PREDICT.encode(),
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
                "root_path": "",
            }

            async def receive():
                return {"type": "http.request", "body": b"{not-json", "more_body": False}

            async def send(message):
                sent.append(message)

            await app(scope, receive, send)
            return sent

        messages = asyncio.run(malformed())
        self.assertEqual(422, messages[0]["status"])
        body = json.loads(messages[1]["body"])
        self.assertEqual("invalid_request", body["error"]["code"])

    def test_validation_failure_is_counted_once(self):
        telemetry = RecordingTelemetry()
        app = create_app(service=StubService(), telemetry=telemetry)
        asyncio.run(asgi_request(app, "POST", PREDICT, payload(amount=0)))
        self.assertEqual(
            1, telemetry.events.count(("error", "invalid_request"))
        )


class CorrelationIdTest(unittest.TestCase):
    def test_valid_identifiers_are_preserved_up_to_the_length_boundary(self):
        for value in ("a", "A.b_c:d-1", "x" * 128):
            with self.subTest(value=value):
                self.assertEqual(value, normalize_correlation_id(value))

    def test_absent_or_unsafe_identifiers_are_replaced(self):
        for value in (None, "", "x" * 129, "has space", "new\nline"):
            with self.subTest(value=value):
                generated = normalize_correlation_id(value)
                self.assertNotEqual(value, generated)
                self.assertEqual(36, len(generated))

    def test_requests_are_served_when_opentelemetry_is_not_installed(self):
        app = create_app(service=StubService(), telemetry=RecordingTelemetry())
        with patch.dict(sys.modules, {"opentelemetry": None}):
            status, headers, _ = asyncio.run(
                asgi_request(
                    app, "GET", "/health/live", None, {"x-correlation-id": "corr-1"}
                )
            )
        self.assertEqual(200, status)
        self.assertEqual("corr-1", headers["x-correlation-id"])


class ProductionWiringTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        (self.repo / "feature_store.yaml").write_text("project: synthetic\n")
        self.online_store = SimpleNamespace(
            type="redis", connection_string="redis.invalid:6379"
        )
        self.store = SimpleNamespace(
            config=SimpleNamespace(online_store=self.online_store)
        )

    def settings(self):
        return InferenceSettings(
            feast_repo_path=self.repo,
            feast_timeout_seconds=1.5,
            kserve_base_url="http://kserve.invalid",
            model_name="fraud-model",
            model_version="9",
            kserve_timeout_seconds=2.5,
        )

    def test_explicit_settings_compose_real_adapters(self):
        with patch("feast.FeatureStore", return_value=self.store) as feature_store:
            app = create_app(
                settings=self.settings(), telemetry=RecordingTelemetry()
            )
        feature_store.assert_called_once_with(repo_path=str(self.repo))
        service = app.state.inference_service
        self.assertIsInstance(service._features, FeastOnlineAdapter)
        self.assertIsInstance(service._predictor, KServeV2Client)
        self.assertEqual("9", service._predictor.model_version)
        self.assertEqual(2.5, service._predictor.timeout_seconds)
        self.assertIn("socket_timeout=1.5", self.online_store.connection_string)

    def test_missing_settings_are_read_from_the_environment(self):
        environment = {
            "EWALLET_FEAST_REPO_PATH": str(self.repo),
            "EWALLET_KSERVE_BASE_URL": "http://kserve.invalid",
            "EWALLET_MODEL_NAME": "fraud-model",
            "EWALLET_MODEL_VERSION": "9",
        }
        with patch.dict("os.environ", environment), patch(
            "feast.FeatureStore", return_value=self.store
        ):
            app = create_app(telemetry=RecordingTelemetry())
        self.assertEqual(
            "fraud-model", app.state.inference_service._predictor.model_name
        )

    def test_default_telemetry_is_used_when_none_is_injected(self):
        hooks = RecordingTelemetry()
        with patch(
            "ai_platform.inference.app.default_telemetry", return_value=hooks
        ):
            app = create_app(service=StubService())
        self.assertIs(hooks, app.state.telemetry)


if __name__ == "__main__":
    unittest.main()
