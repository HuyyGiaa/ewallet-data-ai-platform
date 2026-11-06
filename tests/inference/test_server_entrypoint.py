"""Production entrypoint: telemetry providers, registry mirror and tracing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from opentelemetry.sdk.trace import TracerProvider

from ai_platform.inference import server
from ai_platform.inference.app import create_app
from tests.inference.test_service_and_app import (
    RecordingTelemetry,
    StubService,
    asgi_request,
    payload,
)


def fake_application(**_kwargs):
    """Stand in for ``create_app`` with an in-memory service."""
    return create_app(service=StubService(), telemetry=RecordingTelemetry())


class ConfigureTelemetryTest(unittest.TestCase):
    def test_providers_carry_the_deployment_identity(self):
        environment = {
            "EWALLET_GIT_SHA": "abc123",
            "EWALLET_MODEL_NAME": "fraud-model",
            "EWALLET_MODEL_VERSION": "7",
        }
        with patch.dict(os.environ, environment), patch.object(
            server, "PrometheusMetricReader"
        ) as reader, patch.object(server, "MeterProvider") as meter_provider, patch.object(
            server, "TracerProvider"
        ) as tracer_provider, patch.object(
            server.metrics, "set_meter_provider"
        ) as set_meter, patch.object(
            server.trace, "set_tracer_provider"
        ) as set_tracer:
            server.configure_telemetry()

        resource = tracer_provider.call_args.kwargs["resource"]
        self.assertEqual("fraud-api", resource.attributes["service.name"])
        self.assertEqual("abc123", resource.attributes["service.version"])
        self.assertEqual("fraud-model", resource.attributes["ewallet.model.name"])
        self.assertEqual("7", resource.attributes["ewallet.model.version"])
        self.assertEqual(
            [reader.return_value],
            meter_provider.call_args.kwargs["metric_readers"],
        )
        set_meter.assert_called_once_with(meter_provider.return_value)
        set_tracer.assert_called_once_with(tracer_provider.return_value)

    def test_git_revision_defaults_to_unknown(self):
        without_revision = {
            key: value for key, value in os.environ.items() if key != "EWALLET_GIT_SHA"
        }
        with patch.dict(os.environ, without_revision, clear=True), patch.object(
            server, "PrometheusMetricReader"
        ), patch.object(server, "MeterProvider"), patch.object(
            server, "TracerProvider"
        ) as tracer_provider, patch.object(
            server.metrics, "set_meter_provider"
        ), patch.object(
            server.trace, "set_tracer_provider"
        ):
            server.configure_telemetry()
        resource = tracer_provider.call_args.kwargs["resource"]
        self.assertEqual("unknown", resource.attributes["service.version"])


class BuildTest(unittest.TestCase):
    def setUp(self):
        for name, replacement in (
            ("configure_telemetry", MagicMock()),
            ("create_app", fake_application),
            ("mirror_from_environment", MagicMock(return_value=None)),
        ):
            patcher = patch.object(server, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        tracer = TracerProvider().get_tracer("test")
        patcher = patch.object(server.trace, "get_tracer", return_value=tracer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registry_mirror_precedes_telemetry_and_application(self):
        order = MagicMock()
        server.mirror_from_environment.side_effect = lambda: order.mirror()
        server.configure_telemetry.side_effect = lambda: order.telemetry()
        server.build()
        self.assertEqual(["mirror", "telemetry"], [c[0] for c in order.mock_calls])

    def test_mirrored_registry_is_logged_without_failing_startup(self):
        server.mirror_from_environment.return_value = SimpleNamespace(
            uri="gs://synthetic/registry.db", generation=42
        )
        with self.assertLogs("ewallet.fraud_inference", level="INFO") as captured:
            server.build()
        event = json.loads(captured.records[0].getMessage())
        self.assertEqual(
            {
                "event": "feast_registry_mirrored",
                "uri": "gs://synthetic/registry.db",
                "generation": 42,
            },
            event,
        )

    def test_metrics_endpoint_is_mounted(self):
        application = server.build()
        self.assertIn("/metrics", [route.path for route in application.routes])

    def test_prediction_logs_the_trace_and_correlation_ids(self):
        application = server.build()
        with self.assertLogs("ewallet.fraud_inference", level="INFO") as captured:
            status, _, _ = asyncio.run(
                asgi_request(
                    application,
                    "POST",
                    "/v1/fraud/predict",
                    payload(),
                    {"x-correlation-id": "corr-7"},
                )
            )
        self.assertEqual(200, status)
        finished = [
            json.loads(record.getMessage())
            for record in captured.records
            if "request_finished" in record.getMessage()
        ]
        self.assertEqual(1, len(finished))
        self.assertEqual("/v1/fraud/predict", finished[0]["path"])
        self.assertEqual(200, finished[0]["status"])
        self.assertEqual("corr-7", finished[0]["correlation_id"])
        self.assertRegex(finished[0]["trace_id"], "^[0-9a-f]{32}$")
        self.assertNotEqual("0" * 32, finished[0]["trace_id"])

    def test_other_routes_are_not_traced_or_logged(self):
        application = server.build()
        logger = logging.getLogger("ewallet.fraud_inference")
        with self.assertLogs(logger, level="INFO") as captured:
            status, _, _ = asyncio.run(
                asgi_request(application, "GET", "/health/live")
            )
            logger.info("sentinel")  # assertLogs fails when nothing is logged.
        self.assertEqual(200, status)
        self.assertEqual(["sentinel"], [r.getMessage() for r in captured.records])


class MainTest(unittest.TestCase):
    def test_server_binds_the_configured_port(self):
        application = object()
        with patch.dict(os.environ, {"PORT": "9090"}), patch.object(
            server, "build", return_value=application
        ), patch.object(server.uvicorn, "run") as run, patch.object(
            server.logging, "basicConfig"
        ):
            server.main()
        run.assert_called_once_with(application, host="0.0.0.0", port=9090)

    def test_default_port_matches_the_image(self):
        environment = {k: v for k, v in os.environ.items() if k != "PORT"}
        with patch.dict(os.environ, environment, clear=True), patch.object(
            server, "build", return_value=object()
        ), patch.object(server.uvicorn, "run") as run, patch.object(
            server.logging, "basicConfig"
        ):
            server.main()
        self.assertEqual(8080, run.call_args.kwargs["port"])


if __name__ == "__main__":
    unittest.main()
