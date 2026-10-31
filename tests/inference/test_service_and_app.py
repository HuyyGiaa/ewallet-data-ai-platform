from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from ai_platform.features.feast.schema import FEATURE_SPECS
from ai_platform.inference.app import create_app
from ai_platform.inference.assembler import (
    CanonicalFeatureAssembler,
    HistoricalFeatureSet,
)
from ai_platform.inference.config import (
    InferenceSettings,
    configure_feast_redis_timeouts,
)
from ai_platform.inference.errors import ConfigurationError, FeatureBackendError
from ai_platform.inference.schemas import (
    FraudPredictionRequest,
    FraudPredictionResponse,
    ModelPrediction,
)
from ai_platform.inference.service import InferenceService
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS


UTC = timezone.utc
EVENT_TIME = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def payload(**overrides):
    value = {
        "transaction_id": "tx-sensitive-1",
        "event_timestamp": "2026-10-29T12:00:00Z",
        "user_id": "user-sensitive-1",
        "account_id": "account-sensitive-1",
        "device_id": "device-sensitive-1",
        "merchant_id": "merchant-sensitive-1",
        "counterparty_account_id": None,
        "type": "payment",
        "amount": 987654.0,
        "currency": "VND",
        "channel": "app",
    }
    value.update(overrides)
    return value


def historical() -> HistoricalFeatureSet:
    integer_names = {
        f"{view_name}__{name}"
        for view_name, spec in FEATURE_SPECS.items()
        for name in spec["integer_features"]
    }
    values = {
        name: (1 if name in integer_names else 1.0)
        for name in HISTORICAL_FEATURE_COLUMNS
    }
    values.update(
        {
            "user_behavior__user_avg_amount_30d": 100.0,
            "user_behavior__user_std_amount_30d": 20.0,
            "account_behavior__account_avg_amount_30d": 200.0,
            "account_behavior__account_std_amount_30d": 50.0,
            "user_behavior__user_tx_count_1h": 4,
            "account_behavior__account_tx_count_1h": 2,
            "merchant_behavior__merchant_tx_count_10m": 2,
            "merchant_behavior__merchant_tx_count_24h": 288,
        }
    )
    timestamp = EVENT_TIME - timedelta(minutes=2)
    return HistoricalFeatureSet(
        values=values,
        timestamps={name: timestamp for name in HISTORICAL_FEATURE_COLUMNS},
    )


class FakeProvider:
    def __init__(self, error=None):
        self.error = error
        self.requests = []
        self.ready_calls = 0

    async def get_features(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return historical()

    async def ready(self):
        self.ready_calls += 1
        if self.error:
            raise self.error
        return True


class FakePredictor:
    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.ready_calls = 0

    async def predict(self, vector, *, correlation_id):
        self.calls.append((vector, correlation_id))
        if self.error:
            raise self.error
        return ModelPrediction(
            fraud_probability=0.8,
            fraud_prediction=1,
            model_name="ewallet-fraud-classifier",
            model_version="7",
        )

    async def ready(self):
        self.ready_calls += 1
        if self.error:
            raise self.error
        return True


class RecordingTelemetry:
    def __init__(self):
        self.events = []

    @contextmanager
    def request(self, *, correlation_id):
        self.events.append(("request_start", correlation_id))
        try:
            yield
        except Exception as exc:
            self.events.append(("request_error", type(exc).__name__))
            raise
        finally:
            self.events.append(("request_end", correlation_id))

    @contextmanager
    def stage(self, name, *, correlation_id):
        self.events.append(("stage_start", name))
        try:
            yield
        finally:
            self.events.append(("stage_end", name))

    def record_error(self, code):
        self.events.append(("error", code))


class RecordingLogger:
    def __init__(self):
        self.events = []

    def event(self, event, *, correlation_id, **metadata):
        self.events.append(
            {"event": event, "correlation_id": correlation_id, **metadata}
        )


class StubService:
    def __init__(self, *, ready_error=None):
        self.ready_error = ready_error
        self.requests = []

    async def predict(self, request, *, correlation_id):
        self.requests.append((request, correlation_id))
        return FraudPredictionResponse(
            transaction_id=request.transaction_id,
            fraud_probability=0.8,
            fraud_prediction=1,
            model_name="ewallet-fraud-classifier",
            model_version="7",
            feature_timestamp=EVENT_TIME - timedelta(minutes=2),
            correlation_id=correlation_id,
        )

    async def ready(self):
        if self.ready_error:
            raise self.ready_error
        return True


async def asgi_request(app, method, path, body=None, headers=None):
    sent = []
    encoded = b"" if body is None else json.dumps(body).encode("utf-8")
    request_messages = [
        {"type": "http.request", "body": encoded, "more_body": False}
    ]

    async def receive():
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    raw_headers = [(b"host", b"testserver")]
    if body is not None:
        raw_headers.append((b"content-type", b"application/json"))
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    await app(scope, receive, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    return (
        start["status"],
        {key.decode(): value.decode() for key, value in start["headers"]},
        json.loads(response_body) if response_body else None,
    )


class InferenceSettingsTest(unittest.TestCase):
    def test_environment_configuration_is_validated_without_defaults_for_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "feature_store.yaml").write_text("project: ewallet\n")
            settings = InferenceSettings.from_env(
                {
                    "EWALLET_FEAST_REPO_PATH": str(repo),
                    "EWALLET_KSERVE_BASE_URL": "http://kserve.internal",
                    "EWALLET_MODEL_NAME": "ewallet-fraud-classifier",
                    "EWALLET_MODEL_VERSION": "7",
                    "EWALLET_KSERVE_TIMEOUT_SECONDS": "2.5",
                }
            )
            self.assertEqual(repo, settings.feast_repo_path)
            self.assertEqual(2.0, settings.feast_timeout_seconds)
            self.assertEqual(2.5, settings.kserve_timeout_seconds)

    def test_missing_invalid_or_credential_bearing_configuration_fails_closed(self):
        cases = (
            {},
            {
                "EWALLET_FEAST_REPO_PATH": "/missing",
                "EWALLET_KSERVE_BASE_URL": "http://kserve.internal",
                "EWALLET_MODEL_NAME": "m",
                "EWALLET_MODEL_VERSION": "1",
            },
            {
                "EWALLET_FEAST_REPO_PATH": "/tmp",
                "EWALLET_KSERVE_BASE_URL": "http://user:secret@kserve.internal",
                "EWALLET_MODEL_NAME": "m",
                "EWALLET_MODEL_VERSION": "1",
            },
        )
        for environment in cases:
            with self.subTest(environment=environment):
                with self.assertRaises(ConfigurationError):
                    InferenceSettings.from_env(environment)

    def test_non_finite_timeout_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "feature_store.yaml").write_text("project: ewallet\n")
            environment = {
                "EWALLET_FEAST_REPO_PATH": str(repo),
                "EWALLET_KSERVE_BASE_URL": "http://kserve.internal",
                "EWALLET_MODEL_NAME": "ewallet-fraud-classifier",
                "EWALLET_MODEL_VERSION": "7",
                "EWALLET_KSERVE_TIMEOUT_SECONDS": "nan",
            }
            with self.assertRaises(ConfigurationError):
                InferenceSettings.from_env(environment)

            environment["EWALLET_KSERVE_TIMEOUT_SECONDS"] = "2"
            environment["EWALLET_FEAST_TIMEOUT_SECONDS"] = "inf"
            with self.assertRaises(ConfigurationError):
                InferenceSettings.from_env(environment)

    def test_feast_redis_socket_timeouts_are_injected_before_client_use(self):
        online_store = SimpleNamespace(
            type="redis",
            connection_string="redis.internal:6379,db=0",
        )
        store = SimpleNamespace(
            config=SimpleNamespace(online_store=online_store)
        )

        configure_feast_redis_timeouts(store, timeout_seconds=1.5)

        self.assertEqual(
            "redis.internal:6379,db=0,socket_connect_timeout=1.5,socket_timeout=1.5",
            online_store.connection_string,
        )

    def test_feast_redis_timeout_configuration_fails_closed(self):
        cases = (
            None,
            SimpleNamespace(type="sqlite", connection_string="local.db"),
            SimpleNamespace(
                type="redis",
                connection_string=(
                    "redis.internal:6379,socket_connect_timeout=1,"
                    "socket_timeout=5"
                ),
            ),
        )
        for online_store in cases:
            with self.subTest(online_store=online_store):
                store = SimpleNamespace(
                    config=SimpleNamespace(online_store=online_store)
                )
                with self.assertRaises(ConfigurationError):
                    configure_feast_redis_timeouts(
                        store, timeout_seconds=2.0
                    )


class InferenceServiceTest(unittest.TestCase):
    def test_service_composes_dependencies_and_records_stage_boundaries(self):
        provider = FakeProvider()
        predictor = FakePredictor()
        telemetry = RecordingTelemetry()
        logger = RecordingLogger()
        service = InferenceService(
            feature_provider=provider,
            assembler=CanonicalFeatureAssembler(),
            predictor=predictor,
            telemetry=telemetry,
            event_logger=logger,
        )
        request = FraudPredictionRequest.model_validate(payload())
        response = asyncio.run(service.predict(request, correlation_id="corr-1"))
        self.assertEqual("tx-sensitive-1", response.transaction_id)
        self.assertEqual(0.8, response.fraud_probability)
        self.assertEqual("7", response.model_version)
        self.assertEqual(EVENT_TIME - timedelta(minutes=2), response.feature_timestamp)
        self.assertEqual(41, len(predictor.calls[0][0].values))
        self.assertEqual(
            ["feature_retrieval", "feature_assembly", "model_inference"],
            [value for kind, value in telemetry.events if kind == "stage_start"],
        )

        serialized_logs = json.dumps(logger.events)
        for sensitive in (
            "987654",
            "user-sensitive-1",
            "account-sensitive-1",
            "merchant-sensitive-1",
        ):
            self.assertNotIn(sensitive, serialized_logs)

    def test_service_classifies_dependency_error_and_checks_readiness(self):
        provider = FakeProvider(error=FeatureBackendError())
        telemetry = RecordingTelemetry()
        logger = RecordingLogger()
        service = InferenceService(
            feature_provider=provider,
            assembler=CanonicalFeatureAssembler(),
            predictor=FakePredictor(),
            telemetry=telemetry,
            event_logger=logger,
        )
        with self.assertRaises(FeatureBackendError):
            asyncio.run(
                service.predict(
                    FraudPredictionRequest.model_validate(payload()),
                    correlation_id="corr-1",
                )
            )
        self.assertIn(("error", "feature_backend_error"), telemetry.events)
        with self.assertRaises(FeatureBackendError):
            asyncio.run(service.ready())

    def test_ready_checks_both_dependencies(self):
        provider = FakeProvider()
        predictor = FakePredictor()
        service = InferenceService(
            feature_provider=provider,
            assembler=CanonicalFeatureAssembler(),
            predictor=predictor,
            telemetry=RecordingTelemetry(),
            event_logger=RecordingLogger(),
        )
        self.assertTrue(asyncio.run(service.ready()))
        self.assertEqual(1, provider.ready_calls)
        self.assertEqual(1, predictor.ready_calls)


class FastApiContractTest(unittest.TestCase):
    def test_predict_route_validates_payload_and_propagates_correlation(self):
        service = StubService()
        app = create_app(service=service, telemetry=RecordingTelemetry())
        status, headers, result = asyncio.run(
            asgi_request(
                app,
                "POST",
                "/v1/fraud/predict",
                payload(),
                {"x-correlation-id": "corr-123"},
            )
        )
        self.assertEqual(200, status)
        self.assertEqual("corr-123", headers["x-correlation-id"])
        self.assertEqual("corr-123", result["correlation_id"])
        self.assertEqual("tx-sensitive-1", result["transaction_id"])
        self.assertEqual("corr-123", service.requests[0][1])

    def test_invalid_payload_returns_structured_error_without_input_values(self):
        app = create_app(service=StubService(), telemetry=RecordingTelemetry())
        invalid = payload(amount=-987654.0, label=1)
        status, headers, result = asyncio.run(
            asgi_request(app, "POST", "/v1/fraud/predict", invalid)
        )
        self.assertEqual(422, status)
        self.assertEqual("invalid_request", result["error"]["code"])
        self.assertIn("x-correlation-id", headers)
        serialized = json.dumps(result)
        self.assertNotIn("-987654", serialized)
        self.assertNotIn("user-sensitive-1", serialized)

    def test_health_and_readiness_endpoints(self):
        app = create_app(service=StubService(), telemetry=RecordingTelemetry())
        live = asyncio.run(asgi_request(app, "GET", "/health/live"))
        ready = asyncio.run(asgi_request(app, "GET", "/health/ready"))
        self.assertEqual((200, "ok"), (live[0], live[2]["status"]))
        self.assertEqual((200, "ready"), (ready[0], ready[2]["status"]))
        self.assertEqual("dependency_connectivity", ready[2]["scope"])
        self.assertFalse(ready[2]["feature_materialization_verified"])
        self.assertFalse(ready[2]["offline_online_parity_verified"])

        failed = create_app(
            service=StubService(ready_error=FeatureBackendError()),
            telemetry=RecordingTelemetry(),
        )
        unavailable = asyncio.run(
            asgi_request(failed, "GET", "/health/ready")
        )
        self.assertEqual(503, unavailable[0])
        self.assertEqual("feature_backend_error", unavailable[2]["error"]["code"])

    def test_invalid_correlation_header_is_replaced(self):
        service = StubService()
        app = create_app(service=service, telemetry=RecordingTelemetry())
        _, headers, _ = asyncio.run(
            asgi_request(
                app,
                "POST",
                "/v1/fraud/predict",
                payload(),
                {"x-correlation-id": "bad value with spaces"},
            )
        )
        self.assertNotEqual("bad value with spaces", headers["x-correlation-id"])
        self.assertEqual(36, len(headers["x-correlation-id"]))


if __name__ == "__main__":
    unittest.main()
