"""N11 P1: bounded OTLP audit logs from the BYO agents (to the shared Loki pipeline)."""

from __future__ import annotations

import logging
import os
import unittest
from unittest.mock import patch

from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider

from ai_platform.llm_agent import byo_runtime
from ai_platform.llm_agent.byo_runtime import AUDIT_LOGGER_NAME, audit, configure_audit_logs


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.exporter = InMemoryLogExporter()
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(self.exporter))
        self.handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
        self.logger = logging.getLogger(AUDIT_LOGGER_NAME)
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)

    def test_audit_record_carries_only_allowlisted_attributes_and_trace_context(self):
        tracer = TracerProvider().get_tracer("test")
        with tracer.start_as_current_span("g3.coordinator.execute") as span:
            audit("coordinator.task_completed", request_id="nl-1", task_id="t-1", mode="natural-language",
                  status="ok", duration_ms=12, prompt_tokens=594)
        [record] = [item.log_record for item in self.exporter.get_finished_logs()]
        attributes = dict(record.attributes)
        self.assertEqual(attributes["ewallet.event"], "coordinator.task_completed")
        self.assertEqual(attributes["ewallet.request_id"], "nl-1")
        self.assertEqual(attributes["ewallet.duration_ms"], 12)
        self.assertEqual(record.trace_id, span.get_span_context().trace_id)
        self.assertFalse([key for key in attributes if key.startswith("ewallet.")
                          and key[8:] not in byo_runtime.AUDIT_FIELDS | {"event"}])

    def test_non_allowlisted_fields_and_free_text_events_are_refused(self):
        for kwargs in ({"prompt": "giao dịch ..."}, {"transaction": {"amount": 1}}, {"api_key": "x"}):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                audit("coordinator.task_started", **kwargs)
        with self.assertRaises(ValueError):
            audit("free text with spaces")

    def test_values_are_bounded(self):
        audit("coordinator.task_failed", code="x" * 500)
        [record] = [item.log_record for item in self.exporter.get_finished_logs()]
        self.assertEqual(len(record.attributes["ewallet.code"]), 128)


class AuditConfigurationTests(unittest.TestCase):
    def test_disabled_by_default_and_requires_endpoint(self):
        with patch.dict(os.environ, {"EWALLET_OTEL_LOGS_ENABLED": "false"}, clear=False):
            self.assertFalse(configure_audit_logs("svc", "1"))
        with patch.dict(os.environ, {"EWALLET_OTEL_LOGS_ENABLED": "true"}, clear=True), \
                patch.object(byo_runtime, "_AUDIT_PROVIDER", None):
            self.assertFalse(configure_audit_logs("svc", "1"))

    def test_enabled_with_collector_endpoint_installs_one_handler(self):
        env = {"EWALLET_OTEL_LOGS_ENABLED": "true",
               "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "otel-collector.ewallet-agents.svc.cluster.local:4317"}
        logger = logging.getLogger(AUDIT_LOGGER_NAME)
        before = list(logger.handlers)
        try:
            with patch.dict(os.environ, env, clear=True), patch.object(byo_runtime, "_AUDIT_PROVIDER", None):
                self.assertTrue(configure_audit_logs("fraud-coordinator", "1.0.0"))
                self.assertTrue(configure_audit_logs("fraud-coordinator", "1.0.0"))  # idempotent
            added = [handler for handler in logger.handlers if handler not in before]
            self.assertEqual(len(added), 1)
            self.assertIsInstance(added[0], LoggingHandler)
        finally:
            for handler in list(logger.handlers):
                if handler not in before:
                    logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
