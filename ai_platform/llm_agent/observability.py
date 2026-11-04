"""Low-cardinality telemetry and redacted JSON events for agents and MCP."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Callable, Mapping, Protocol


_METRIC_KEYS = frozenset({"service", "operation", "agent", "tool", "outcome", "model"})
_SENSITIVE_FRAGMENTS = (
    "authorization", "credential", "password", "secret", "token", "api_key",
    "apikey", "cookie", "prompt", "completion", "message", "argument",
    "chain_of_thought", "account_id", "user_id", "transaction_id",
)
_OTEL_CONFIGURED = False
_OTEL_LOGGER_PROVIDER: object | None = None


def metric_attributes(**attributes: str) -> dict[str, str]:
    """Return bounded metric dimensions or reject high-cardinality keys."""

    unknown = set(attributes) - _METRIC_KEYS
    if unknown:
        raise ValueError(f"unbounded metric attributes are forbidden: {sorted(unknown)}")
    return dict(attributes)


def sanitize_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    """Remove sensitive fields recursively; callers cannot override the policy."""

    safe: dict[str, object] = {}
    for key, value in metadata.items():
        normalized = key.lower().replace("-", "_")
        if any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS):
            safe[key] = "[REDACTED]"
        elif isinstance(value, Mapping):
            safe[key] = sanitize_metadata(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
    return safe


def mcp_semantic_outcome(result: Mapping[str, object]) -> str:
    """Classify MCP semantics independently from the HTTP status code."""

    if result.get("isError") is True:
        return "error"
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping) and structured.get("status") not in (None, "ok"):
        return "error"
    return "success"


def trace_ids(traceparent: str) -> tuple[str, str]:
    parts = traceparent.split("-")
    if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
        return parts[1], parts[2]
    return "", ""


class ToolTelemetry(Protocol):
    def record_tool_call(
        self, *, tool: str, outcome: str, duration_seconds: float, traceparent: str
    ) -> None: ...


class SafeToolTelemetry:
    """Keep telemetry best-effort and outside the business response contract."""

    def __init__(
        self, delegate: ToolTelemetry, *, logger: logging.Logger | None = None
    ) -> None:
        self.delegate = delegate
        self.logger = logger or logging.getLogger("ewallet.llm_agent.telemetry")

    def record_tool_call(
        self, *, tool: str, outcome: str, duration_seconds: float, traceparent: str
    ) -> None:
        try:
            self.delegate.record_tool_call(
                tool=tool,
                outcome=outcome,
                duration_seconds=duration_seconds,
                traceparent=traceparent,
            )
        except Exception:
            # Never serialize the exception: exporter errors may contain endpoint
            # credentials or request metadata.
            self.logger.warning(
                "telemetry_export_failed service=%s tool=%s outcome=%s",
                getattr(self.delegate, "service", "unknown"),
                tool,
                outcome,
            )


def safe_tool_telemetry(
    telemetry: ToolTelemetry, logger: logging.Logger | None = None
) -> ToolTelemetry:
    if isinstance(telemetry, SafeToolTelemetry):
        return telemetry
    return SafeToolTelemetry(telemetry, logger=logger)


class JsonTelemetry:
    """Always-available structured audit events with trace correlation."""

    def __init__(
        self,
        service: str,
        *,
        logger: logging.Logger | None = None,
        mirror_logger: logging.Logger | None = None,
        sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.service = service
        self.logger = logger or logging.getLogger("ewallet.llm_agent.telemetry")
        self.mirror_logger = mirror_logger
        self.sink = sink

    def record_tool_call(
        self, *, tool: str, outcome: str, duration_seconds: float, traceparent: str
    ) -> None:
        trace_id, span_id = trace_ids(traceparent)
        payload = sanitize_metadata({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": "INFO" if outcome == "success" else "ERROR",
            "service": self.service,
            "event": "mcp.tool.completed",
            "tool": tool,
            "outcome": outcome,
            "duration_seconds": round(duration_seconds, 6),
            "trace_id": trace_id,
            "span_id": span_id,
        })
        if self.sink is not None:
            self.sink(payload)
        else:
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            self.logger.info(serialized)
            if self.mirror_logger is not None and self.mirror_logger is not self.logger:
                self.mirror_logger.info(serialized)


class OpenTelemetryToolTelemetry(JsonTelemetry):
    """OTel metrics and spans; provider/exporter configuration stays external."""

    def __init__(self, service: str, *, logger: logging.Logger | None = None) -> None:
        # Structured events must always pass through the dedicated logger that
        # owns the OTLP LoggingHandler. The optional application audit logger is
        # a mirror for JSON stdout evidence; it must not replace the exporter.
        super().__init__(
            service,
            logger=logging.getLogger("ewallet.llm_agent.telemetry"),
            mirror_logger=logger,
        )
        from opentelemetry import metrics, trace

        meter = metrics.get_meter("ewallet.llm_agent")
        self.tracer = trace.get_tracer("ewallet.llm_agent")
        self.calls = meter.create_counter("ewallet_mcp_tool_calls_total")
        self.duration = meter.create_histogram("ewallet_mcp_tool_duration_seconds", unit="s")

    def record_tool_call(
        self, *, tool: str, outcome: str, duration_seconds: float, traceparent: str
    ) -> None:
        from opentelemetry import propagate
        from opentelemetry.trace import Status, StatusCode

        attributes = metric_attributes(service=self.service, tool=tool, outcome=outcome)
        self.calls.add(1, attributes)
        self.duration.record(duration_seconds, attributes)
        ended_ns = time.time_ns()
        span = self.tracer.start_span(
            f"mcp.tool {tool}",
            context=propagate.extract({"traceparent": traceparent}),
            start_time=ended_ns - int(duration_seconds * 1_000_000_000),
            attributes={"mcp.tool.name": tool, "mcp.outcome": outcome},
        )
        span.set_status(Status(StatusCode.ERROR if outcome == "error" else StatusCode.OK))
        span.end(end_time=ended_ns)
        context = span.get_span_context()
        emitted_traceparent = (
            f"00-{context.trace_id:032x}-{context.span_id:016x}-01"
            if context.is_valid else traceparent
        )
        super().record_tool_call(
            tool=tool, outcome=outcome, duration_seconds=duration_seconds,
            traceparent=emitted_traceparent,
        )


def configure_otlp(service: str) -> None:
    """Configure one process-wide OTLP provider when an endpoint is supplied."""

    global _OTEL_CONFIGURED, _OTEL_LOGGER_PROVIDER
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if _OTEL_CONFIGURED or not endpoint:
        return
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    insecure = os.environ.get("OTEL_EXPORTER_OTLP_INSECURE", "false").lower() == "true"
    resource = Resource.create({"service.name": service})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
    )
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=insecure)
        )],
    ))
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=insecure))
    )
    telemetry_logger = logging.getLogger("ewallet.llm_agent.telemetry")
    telemetry_logger.setLevel(logging.INFO)
    telemetry_logger.addHandler(
        LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    )
    # Keep the provider alive for the lifetime of the process so its batch
    # processor can flush redacted structured events asynchronously.
    _OTEL_LOGGER_PROVIDER = logger_provider
    _OTEL_CONFIGURED = True


def default_tool_telemetry(service: str, logger: logging.Logger | None = None) -> ToolTelemetry:
    try:
        configure_otlp(service)
        telemetry: ToolTelemetry = OpenTelemetryToolTelemetry(service, logger=logger)
    except Exception:
        telemetry = JsonTelemetry(service, logger=logger)
    return safe_tool_telemetry(telemetry, logger)
