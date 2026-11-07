"""OpenTelemetry hooks measured with in-memory SDK readers and exporters."""

from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import patch

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from ai_platform.inference import observability
from ai_platform.inference.observability import (
    JsonEventLogger,
    NoopBackendTelemetry,
    NoopTelemetry,
    OpenTelemetryBackendTelemetry,
    OpenTelemetryHooks,
    default_backend_telemetry,
    default_telemetry,
)


class TelemetryHarness(unittest.TestCase):
    """Route the module's OpenTelemetry lookups to in-memory providers."""

    def setUp(self):
        self.reader = InMemoryMetricReader()
        self.exporter = InMemorySpanExporter()
        meter_provider = MeterProvider(metric_readers=[self.reader])
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        for target, replacement in (
            ("opentelemetry.metrics.get_meter", meter_provider.get_meter),
            ("opentelemetry.trace.get_tracer", tracer_provider.get_tracer),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def points(self, metric_name):
        """Return the data points of one metric."""
        data = self.reader.get_metrics_data()
        if data is None:
            return []
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == metric_name
            for point in metric.data.data_points
        ]

    def counter_values(self, metric_name):
        """Return a metric's values keyed by sorted attributes."""
        return {
            tuple(sorted(point.attributes.items())): point.value
            for point in self.points(metric_name)
        }

    def span(self, name):
        """Return the finished span with this name."""
        return next(
            span for span in self.exporter.get_finished_spans() if span.name == name
        )


class RequestHooksTest(TelemetryHarness):
    def test_success_is_counted_and_active_requests_return_to_zero(self):
        hooks = OpenTelemetryHooks()
        with hooks.request(correlation_id="corr-1"):
            pass
        self.assertEqual(
            {(("outcome", "success"),): 1},
            self.counter_values("fraud_inference_requests_total"),
        )
        self.assertEqual(
            {(): 0}, self.counter_values("fraud_inference_active_requests")
        )
        span = self.span("fraud_inference.request")
        self.assertEqual("corr-1", span.attributes["correlation.id"])

    def test_failure_is_counted_and_propagates(self):
        hooks = OpenTelemetryHooks()
        with self.assertRaises(ValueError):
            with hooks.request(correlation_id="corr-1"):
                raise ValueError("boom")
        self.assertEqual(
            {(("outcome", "error"),): 1},
            self.counter_values("fraud_inference_requests_total"),
        )
        self.assertEqual(
            {(): 0}, self.counter_values("fraud_inference_active_requests")
        )

    def test_stage_latency_is_labelled_by_stage_and_outcome(self):
        hooks = OpenTelemetryHooks()
        with hooks.stage("feature_assembly", correlation_id="corr-1"):
            pass
        with self.assertRaises(ValueError):
            with hooks.stage("model_inference", correlation_id="corr-1"):
                raise ValueError("boom")
        observed = {
            tuple(sorted(point.attributes.items())): point.count
            for point in self.points("fraud_inference_stage_latency_seconds")
        }
        self.assertEqual(
            {
                (("outcome", "success"), ("stage", "feature_assembly")): 1,
                (("outcome", "error"), ("stage", "model_inference")): 1,
            },
            observed,
        )
        span = self.span("fraud_inference.feature_assembly")
        self.assertEqual("feature_assembly", span.attributes["inference.stage"])

    def test_errors_are_counted_by_code(self):
        hooks = OpenTelemetryHooks()
        hooks.record_error("missing_feature")
        hooks.record_error("missing_feature")
        self.assertEqual(
            {(("error.code", "missing_feature"),): 2},
            self.counter_values("fraud_inference_errors_total"),
        )


class BackendTelemetryTest(TelemetryHarness):
    def test_operation_outcomes_are_measured(self):
        telemetry = OpenTelemetryBackendTelemetry()
        with telemetry.operation("kserve", "infer"):
            pass
        with self.assertRaises(ValueError):
            with telemetry.operation("feast-online", "ready"):
                raise ValueError("boom")
        self.assertEqual(
            {
                (
                    ("backend", "kserve"),
                    ("operation", "infer"),
                    ("outcome", "success"),
                ): 1,
                (
                    ("backend", "feast-online"),
                    ("operation", "ready"),
                    ("outcome", "error"),
                ): 1,
            },
            self.counter_values("backend_client_requests_total"),
        )
        self.assertEqual(2, len(self.points("backend_client_latency_seconds")))
        failed = self.span("feast-online.ready")
        self.assertEqual("exception", failed.events[0].name)

    def test_unlisted_dimensions_are_rejected_before_any_metric(self):
        telemetry = OpenTelemetryBackendTelemetry()
        for backend, operation in (("redis", "infer"), ("kserve", "delete")):
            with self.subTest(backend=backend, operation=operation):
                with self.assertRaises(ValueError):
                    with telemetry.operation(backend, operation):
                        pass
        self.assertEqual({}, self.counter_values("backend_client_requests_total"))

    def test_only_allowlisted_attributes_reach_the_span(self):
        telemetry = OpenTelemetryBackendTelemetry()
        with telemetry.operation(
            "kserve",
            "infer",
            attributes={"model.name": "fraud", "user.id": "synthetic-user"},
        ):
            pass
        span = self.span("kserve.infer")
        self.assertEqual("fraud", span.attributes["model.name"])
        self.assertNotIn("user.id", span.attributes)

    def test_negative_freshness_is_not_recorded(self):
        telemetry = OpenTelemetryBackendTelemetry()
        telemetry.record_feast_freshness(-1)
        self.assertEqual([], self.points("feast_feature_freshness_seconds"))
        telemetry.record_feast_freshness(0)
        telemetry.record_feast_freshness(12.5)
        (point,) = self.points("feast_feature_freshness_seconds")
        self.assertEqual(2, point.count)
        self.assertEqual(12.5, point.sum)


class NoopTelemetryTest(unittest.TestCase):
    def test_noop_hooks_propagate_exceptions_and_record_nothing(self):
        hooks = NoopTelemetry()
        backend = NoopBackendTelemetry()
        with self.assertRaises(ValueError):
            with hooks.request(correlation_id="corr-1"):
                raise ValueError("boom")
        with hooks.stage("stage", correlation_id="corr-1"):
            pass
        with backend.operation("kserve", "infer"):
            pass
        self.assertIsNone(hooks.record_error("code"))
        self.assertIsNone(backend.record_feast_freshness(1.0))


class JsonEventLoggerTest(unittest.TestCase):
    def test_events_are_structured_and_keep_only_supplied_metadata(self):
        logger = logging.getLogger("test.fraud_inference.events")
        with self.assertLogs(logger, level="INFO") as captured:
            JsonEventLogger(logger).event(
                "prediction_failed", correlation_id="corr-1", error_code="x"
            )
        event = json.loads(captured.records[0].getMessage())
        self.assertEqual("prediction_failed", event["event"])
        self.assertEqual("corr-1", event["correlation_id"])
        self.assertEqual("x", event["error_code"])
        self.assertEqual("fraud-inference", event["service.name"])
        self.assertEqual("INFO", event["severity"])
        self.assertTrue(event["timestamp"].endswith("+00:00"))

    def test_default_logger_is_the_service_logger(self):
        with self.assertLogs("ewallet.fraud_inference", level="INFO"):
            JsonEventLogger().event("event", correlation_id="corr-1")


class TelemetryFactoryTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(observability, "_BACKEND_TELEMETRY", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_telemetry_prefers_opentelemetry(self):
        self.assertIsInstance(default_telemetry(), OpenTelemetryHooks)

    def test_default_telemetry_degrades_without_opentelemetry(self):
        with patch.object(
            observability, "OpenTelemetryHooks", side_effect=ImportError
        ):
            self.assertIsInstance(default_telemetry(), NoopTelemetry)

    def test_backend_telemetry_is_a_process_singleton(self):
        first = default_backend_telemetry()
        self.assertIsInstance(first, OpenTelemetryBackendTelemetry)
        self.assertIs(first, default_backend_telemetry())

    def test_backend_telemetry_degrades_without_opentelemetry(self):
        with patch.object(
            observability,
            "OpenTelemetryBackendTelemetry",
            side_effect=ImportError,
        ):
            self.assertIsInstance(default_backend_telemetry(), NoopBackendTelemetry)


if __name__ == "__main__":
    unittest.main()
