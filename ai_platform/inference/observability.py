"""OpenTelemetry hooks and redacted structured events for Block B."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import time
from typing import Iterator, Mapping, Protocol

class TelemetryHooks(Protocol):
    def request(self, *, correlation_id: str): ...

    def stage(self, name: str, *, correlation_id: str): ...

    def record_error(self, code: str) -> None: ...


class EventLogger(Protocol):
    def event(
        self, event: str, *, correlation_id: str, **metadata: object
    ) -> None: ...


class BackendTelemetry(Protocol):
    def operation(
        self,
        backend: str,
        operation: str,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> Iterator[None]: ...

    def record_feast_freshness(self, seconds: float) -> None: ...


class NoopTelemetry:
    @contextmanager
    def request(self, *, correlation_id: str):
        yield

    @contextmanager
    def stage(self, name: str, *, correlation_id: str):
        yield

    def record_error(self, code: str) -> None:
        return None


class NoopBackendTelemetry:
    @contextmanager
    def operation(
        self,
        backend: str,
        operation: str,
        *,
        attributes: Mapping[str, str] | None = None,
    ):
        yield

    def record_feast_freshness(self, seconds: float) -> None:
        return None


class OpenTelemetryBackendTelemetry:
    """Bounded backend metrics plus explicit Feast/KServe child spans."""

    _BACKENDS = frozenset({"feast-online", "kserve"})
    _OPERATIONS = frozenset({"get_features", "ready", "infer"})

    def __init__(self) -> None:
        from opentelemetry import metrics, trace

        self._tracer = trace.get_tracer("ewallet.ml_backends")
        meter = metrics.get_meter("ewallet.ml_backends")
        self._requests = meter.create_counter("backend_client_requests_total")
        self._latency = meter.create_histogram(
            "backend_client_latency_seconds", unit="s"
        )
        self._freshness = meter.create_histogram(
            "feast_feature_freshness_seconds", unit="s"
        )

    @contextmanager
    def operation(
        self,
        backend: str,
        operation: str,
        *,
        attributes: Mapping[str, str] | None = None,
    ):
        if backend not in self._BACKENDS or operation not in self._OPERATIONS:
            raise ValueError("backend telemetry dimension is not allowlisted")
        safe_attributes = {
            key: value
            for key, value in (attributes or {}).items()
            if key in {"model.name", "model.version"}
        }
        dimensions = {"backend": backend, "operation": operation}
        started = time.perf_counter()
        outcome = "success"
        with self._tracer.start_as_current_span(
            f"{backend}.{operation}",
            attributes={**dimensions, **safe_attributes},
        ) as span:
            try:
                yield
            except Exception as error:
                outcome = "error"
                span.record_exception(error)
                raise
            finally:
                measured = {**dimensions, "outcome": outcome}
                self._requests.add(1, measured)
                self._latency.record(time.perf_counter() - started, measured)

    def record_feast_freshness(self, seconds: float) -> None:
        if seconds >= 0:
            self._freshness.record(seconds, {"backend": "feast-online"})


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


_BACKEND_TELEMETRY: BackendTelemetry | None = None


def default_backend_telemetry() -> BackendTelemetry:
    global _BACKEND_TELEMETRY
    if _BACKEND_TELEMETRY is None:
        try:
            _BACKEND_TELEMETRY = OpenTelemetryBackendTelemetry()
        except ImportError:
            _BACKEND_TELEMETRY = NoopBackendTelemetry()
    return _BACKEND_TELEMETRY
