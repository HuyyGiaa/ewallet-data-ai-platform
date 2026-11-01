"""LLM, agent and MCP telemetry contracts."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import unittest

from ai_platform.llm_agent.observability import (
    JsonTelemetry,
    metric_attributes,
    mcp_semantic_outcome,
    sanitize_metadata,
)


ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = ROOT / "infra/helm/llm-agent-platform/dashboards"
HAS_OTEL_SDK = bool(
    importlib.util.find_spec("opentelemetry")
    and importlib.util.find_spec("opentelemetry.sdk")
)


class AITelemetryTests(unittest.TestCase):
    def test_http_200_with_is_error_is_a_semantic_failure(self) -> None:
        self.assertEqual(mcp_semantic_outcome({"isError": True}), "error")
        self.assertEqual(
            mcp_semantic_outcome({"isError": False, "structuredContent": {"status": "ok"}}),
            "success",
        )
        self.assertEqual(
            mcp_semantic_outcome({"isError": False, "structuredContent": {"status": "unavailable"}}),
            "error",
        )

    def test_metric_attributes_are_low_cardinality(self) -> None:
        self.assertEqual(
            metric_attributes(service="drift-mcp", tool="drift_detection", outcome="error"),
            {"service": "drift-mcp", "tool": "drift_detection", "outcome": "error"},
        )
        for forbidden in ("user_id", "request_id", "transaction_id", "trace_id"):
            with self.subTest(forbidden):
                with self.assertRaises(ValueError):
                    metric_attributes(**{forbidden: "secret"})

    def test_structured_events_redact_sensitive_values_and_keep_trace_ids(self) -> None:
        safe = sanitize_metadata({
            "authorization": "Bearer secret",
            "api_key": "secret-key",
            "prompt": "private prompt",
            "tool_arguments": {"account_id": "A-1"},
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "outcome": "error",
        })
        self.assertEqual(safe["trace_id"], "a" * 32)
        self.assertEqual(safe["span_id"], "b" * 16)
        serialized = json.dumps(safe)
        for leaked in ("Bearer secret", "secret-key", "private prompt", "A-1"):
            self.assertNotIn(leaked, serialized)

        emitted: list[dict[str, object]] = []
        telemetry = JsonTelemetry("drift-mcp", sink=emitted.append)
        telemetry.record_tool_call(
            tool="drift_detection", outcome="error", duration_seconds=0.1,
            traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        )
        self.assertEqual(emitted[0]["trace_id"], "a" * 32)
        self.assertEqual(emitted[0]["span_id"], "b" * 16)

    def test_split_dashboards_do_not_turn_missing_series_into_zero(self) -> None:
        expectations = {
            "llm-runtime.json": (
                "vllm:prompt_tokens_total",
                "vllm:generation_tokens_total",
                "vllm:time_to_first_token_seconds_bucket",
                "vllm:e2e_request_latency_seconds_bucket",
                "vllm:num_requests_running",
                "vllm:num_requests_waiting",
            ),
            "agent-mcp.json": (
                "agentgateway_mcp_requests_total",
                "ewallet_mcp_tool_calls_total",
                "ewallet_mcp_tool_duration_seconds_bucket",
            ),
        }
        for filename, metrics in expectations.items():
            dashboard = json.loads((DASHBOARDS / filename).read_text())
            expressions = "\n".join(
                target["expr"] for panel in dashboard["panels"] for target in panel["targets"]
            )
            for metric in metrics:
                self.assertIn(metric, expressions)
            self.assertNotIn("vector(0)", expressions)
            self.assertNotIn(" or ", expressions)
            for panel in dashboard["panels"]:
                self.assertIn(panel["telemetryStatus"], ("source-verified", "runtime-pending"))

    @unittest.skipUnless(HAS_OTEL_SDK, "OTel SDK project test")
    def test_otel_tool_call_emits_child_span_with_semantic_status(self) -> None:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from ai_platform.llm_agent.observability import OpenTelemetryToolTelemetry

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        telemetry = OpenTelemetryToolTelemetry("drift-mcp")
        telemetry.record_tool_call(
            tool="drift_detection", outcome="error", duration_seconds=0.25,
            traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        )
        span = exporter.get_finished_spans()[0]
        self.assertEqual(span.name, "mcp.tool drift_detection")
        self.assertEqual(f"{span.context.trace_id:032x}", "a" * 32)
        self.assertEqual(f"{span.parent.span_id:016x}", "b" * 16)
        self.assertEqual(span.attributes["mcp.tool.name"], "drift_detection")
        self.assertEqual(span.attributes["mcp.outcome"], "error")
        self.assertEqual(span.status.status_code.name, "ERROR")


if __name__ == "__main__":
    unittest.main()
