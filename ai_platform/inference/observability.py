"""OpenTelemetry hooks and redacted structured events for Block B."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import time
from typing import Protocol

class TelemetryHooks(Protocol):
    def request(self, *, correlation_id: str): ...

    def stage(self, name: str, *, correlation_id: str): ...

    def record_error(self, code: str) -> None: ...


class EventLogger(Protocol):
    def event(
        self, event: str, *, correlation_id: str, **metadata: object
    ) -> None: ...


class NoopTelemetry:
    @contextmanager
    def request(self, *, correlation_id: str):
        yield

    @contextmanager
    def stage(self, name: str, *, correlation_id: str):
        yield

    def record_error(self, code: str) -> None:
        return None


class OpenTelemetryHooks:
    """Backend-neutral metrics and spans; exporters remain external config."""

    def __init__(self) -> None:
        from opentelemetry import metrics, trace

        self._tracer = trace.get_tracer("ewallet.fraud_inference")
        meter = metrics.get_meter("ewallet.fraud_inference")
        self._request_total = meter.create_counter(
            "fraud_inference_requests_total"
        )
        self._error_total = meter.create_counter(
            "fraud_inference_errors_total"
        )
        self._active = meter.create_up_down_counter(
            "fraud_inference_active_requests"
        )
        self._latency = meter.create_histogram(
            "fraud_inference_stage_latency_seconds", unit="s"
        )

    @contextmanager
    def request(self, *, correlation_id: str):
        started = time.perf_counter()
        outcome = "success"
        self._active.add(1)
        with self._tracer.start_as_current_span(
            "fraud_inference.request",
            attributes={"correlation.id": correlation_id},
        ):
            try:
                yield
            except Exception:
                outcome = "error"
                raise
            finally:
                self._latency.record(
                    time.perf_counter() - started,
                    {"stage": "request", "outcome": outcome},
                )
                self._request_total.add(1, {"outcome": outcome})
                self._active.add(-1)

    @contextmanager
    def stage(self, name: str, *, correlation_id: str):
        started = time.perf_counter()
        outcome = "success"
        with self._tracer.start_as_current_span(
            f"fraud_inference.{name}",
            attributes={
                "correlation.id": correlation_id,
                "inference.stage": name,
            },
        ):
            try:
                yield
            except Exception:
                outcome = "error"
                raise
            finally:
                self._latency.record(
                    time.perf_counter() - started,
                    {"stage": name, "outcome": outcome},
                )

    def record_error(self, code: str) -> None:
        self._error_total.add(1, {"error.code": code})


class JsonEventLogger:
    """Emit only caller-supplied safe metadata; never serialize requests."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("ewallet.fraud_inference")

    def event(
        self, event: str, *, correlation_id: str, **metadata: object
    ) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": "INFO",
            "service.name": "fraud-inference",
            "event": event,
            "correlation_id": correlation_id,
            **metadata,
        }
        self._logger.info(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )


def default_telemetry() -> TelemetryHooks:
    try:
        return OpenTelemetryHooks()
    except ImportError:
        return NoopTelemetry()
