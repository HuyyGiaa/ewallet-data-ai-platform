"""Shared kagent 0.10.3 runtime wiring for first-class BYO Agents.

This module owns only controller task persistence, projected-token injection
and OpenTelemetry bootstrap. Domain execution remains in each Agent module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
from typing import Mapping

import httpx
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from kagent.core.a2a import KAgentTaskStore, get_request_user_id
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


LOGGER = logging.getLogger(__name__)
DEFAULT_TOKEN_PATH = Path("/var/run/secrets/tokens/kagent-token")
AUDIT_LOGGER_NAME = "ewallet.agent.audit"
# Only these bounded identifiers/status fields may leave the process as log
# attributes; prompts, transaction contents, features and tokens never do.
AUDIT_FIELDS = frozenset({
    "request_id", "task_id", "context_id", "status", "code", "mode",
    "tool", "target_agent", "duration_ms", "prompt_tokens", "completion_tokens", "pii_types",
})
_AUDIT_PROVIDER: object | None = None
_METER_PROVIDER: object | None = None


class ControllerTokenHook:
    """Read the rotating projected token for every kagent controller request."""

    def __init__(
        self,
        app_name: str = "fraud-coordinator",
        token_path: Path = DEFAULT_TOKEN_PATH,
    ) -> None:
        if not app_name.strip():
            raise ValueError("app_name is required")
        self.app_name = app_name
        self.token_path = token_path

    def available(self) -> bool:
        try:
            return bool(self.token_path.read_text(encoding="utf-8").strip())
        except OSError:
            return False

    async def add_headers(self, request: httpx.Request) -> None:
        token = await asyncio.to_thread(self._read)
        request.headers["X-Agent-Name"] = self.app_name
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        if user_id := get_request_user_id():
            request.headers["X-User-Id"] = user_id

    def _read(self) -> str | None:
        try:
            value = self.token_path.read_text(encoding="utf-8").strip()
            return value or None
        except OSError:
            return None


@dataclass
class AppResources:
    controller_client: httpx.AsyncClient | None
    token_hook: ControllerTokenHook | None


def listen_port(environment: Mapping[str, str]) -> int:
    """Return the agent's listen port from ``EWALLET_AGENT_PORT``, 8080 by default.

    A kagent BYO Deployment serves on 8080. A Substrate sandbox probes port 80 instead, and
    the same image has to serve both.

    Raises:
        ValueError: The value is not a port number.
    """

    port = int(environment.get("EWALLET_AGENT_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("EWALLET_AGENT_PORT must be between 1 and 65535")
    return port


def controller_task_store(app_name: str) -> tuple[TaskStore, AppResources]:
    """Return the controller-backed store in-cluster and an in-memory local store."""

    kagent_url = os.getenv("KAGENT_URL", "").strip()
    if not kagent_url:
        return InMemoryTaskStore(), AppResources(None, None)
    token_hook = ControllerTokenHook(app_name=app_name)
    client = httpx.AsyncClient(
        base_url=kagent_url,
        event_hooks={"request": [token_hook.add_headers]},
    )
    return KAgentTaskStore(client), AppResources(client, token_hook)


def configure_audit_logs(service_name: str, service_version: str) -> bool:
    """Export the bounded audit logger through the shared OTLP collector (Loki).

    Opt-in with ``EWALLET_OTEL_LOGS_ENABLED=true``; kagent's own
    ``OTEL_LOGGING_ENABLED`` stays untouched because it controls kagent-managed
    runtimes, not these BYO processes. Records keep stdout output as well.
    """

    global _AUDIT_PROVIDER
    if os.getenv("EWALLET_OTEL_LOGS_ENABLED", "false").lower() != "true":
        return False
    if _AUDIT_PROVIDER is not None:
        return True
    endpoint = (
        os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "").strip()
        or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    )
    if not endpoint:
        LOGGER.warning("OTEL audit logs enabled without an exporter endpoint")
        return False
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    provider = LoggerProvider(resource=Resource.create({
        "service.name": service_name,
        "service.version": service_version,
    }))
    provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True))
    )
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    audit_logger.setLevel(logging.INFO)
    audit_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))
    _AUDIT_PROVIDER = provider
    return True


def _instance_id() -> str:
    """Pod name (HOSTNAME) in Kubernetes; a random id elsewhere."""

    value = os.getenv("HOSTNAME", "").strip()
    if value:
        return value[:128]
    from uuid import uuid4
    return uuid4().hex


def configure_agent_metrics(service_name: str, service_version: str) -> bool:
    """Export the bounded agent counters (agent_metrics.py) to the shared collector.

    Opt-in with ``EWALLET_OTEL_METRICS_ENABLED=true``. The collector's
    Prometheus exporter publishes them as ``ewallet_<name>``.
    """

    global _METER_PROVIDER
    if os.getenv("EWALLET_OTEL_METRICS_ENABLED", "false").lower() != "true":
        return False
    if _METER_PROVIDER is not None:
        return True
    endpoint = (
        os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "").strip()
        or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    )
    if not endpoint:
        LOGGER.warning("OTEL agent metrics enabled without an exporter endpoint")
        return False
    from opentelemetry import metrics
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    # service.instance.id keeps replicas apart: without it the collector's
    # Prometheus exporter merges identical cumulative series from different
    # pods and one replica's counts overwrite the other's (N12 Gate F).
    provider = MeterProvider(
        resource=Resource.create({
            "service.name": service_name,
            "service.version": service_version,
            "service.instance.id": _instance_id(),
        }),
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True),
            export_interval_millis=int(os.getenv("EWALLET_OTEL_METRICS_INTERVAL_MS", "15000")),
        )],
    )
    metrics.set_meter_provider(provider)
    _METER_PROVIDER = provider
    return True


def flush_agent_metrics(timeout_millis: int = 5_000) -> None:
    """Export pending points now (used right after the zero initialization)."""

    if _METER_PROVIDER is not None:
        try:
            _METER_PROVIDER.force_flush(timeout_millis=timeout_millis)
        except Exception:
            LOGGER.warning("agent metrics flush failed", exc_info=True)


def audit(event: str, **fields: object) -> None:
    """Emit one structured, allowlisted audit record correlated with the active span."""

    if not event.replace(".", "").replace("_", "").isalnum():
        raise ValueError("audit event names are dotted identifiers")
    unknown = set(fields) - AUDIT_FIELDS
    if unknown:
        raise ValueError(f"audit fields are not allowlisted: {sorted(unknown)}")
    attributes = {"ewallet.event": event}
    for key, value in fields.items():
        if value is None:
            continue
        attributes[f"ewallet.{key}"] = value if isinstance(value, (int, float, bool)) else str(value)[:128]
    rendered = " ".join(f"{key[8:]}={value}" for key, value in attributes.items() if key != "ewallet.event")
    logging.getLogger(AUDIT_LOGGER_NAME).info("%s %s", event, rendered, extra=attributes)


def configure_telemetry(service_name: str, service_version: str) -> None:
    """Configure one OTLP provider when the cluster endpoint is present."""

    configure_audit_logs(service_name, service_version)
    configure_agent_metrics(service_name, service_version)
    if os.getenv("OTEL_TRACING_ENABLED", "false").lower() != "true":
        return
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if not endpoint:
        LOGGER.warning("OTEL tracing enabled without an exporter endpoint")
        return
    provider = TracerProvider(
        resource=Resource.create({
            "service.name": service_name,
            "service.version": service_version,
        })
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
