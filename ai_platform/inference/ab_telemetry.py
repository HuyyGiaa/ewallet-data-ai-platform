"""Prometheus-facing metrics for the model experiment.

Series are labelled by experiment, arm and model version only, so cardinality is
fixed by the configuration. Scores go into a histogram: distribution comparison
needs no per-request data, and no transaction or user identifier is exported.
"""

from __future__ import annotations

from ai_platform.inference.ab_router import ABTelemetry, NoopABTelemetry
from ai_platform.inference.schemas import ExperimentAttribution, ModelPrediction


class OpenTelemetryABTelemetry:
    """Per-arm counters and histograms through the OpenTelemetry meter."""

    def __init__(self) -> None:
        from opentelemetry import metrics

        meter = metrics.get_meter("ewallet.fraud_ab")
        self._requests = meter.create_counter("fraud_ab_requests_total")
        self._failures = meter.create_counter("fraud_ab_candidate_failures_total")
        self._positive = meter.create_counter("fraud_ab_positive_predictions_total")
        self._latency = meter.create_histogram("fraud_ab_model_latency_seconds", unit="s")
        self._score = meter.create_histogram("fraud_ab_score")
        self._available = meter.create_counter("fraud_ab_readiness_checks_total")

    def record(
        self,
        attribution: ExperimentAttribution,
        *,
        latency_seconds: float,
        prediction: ModelPrediction | None,
    ) -> None:
        """Count one served or failed prediction."""
        labels = {
            "experiment": attribution.experiment_id,
            "assigned_arm": attribution.assigned_arm,
            "served_arm": attribution.served_arm,
            "model_version": attribution.model_version,
            "fallback": str(attribution.fallback).lower(),
            "outcome": "success" if prediction else "error",
        }
        self._requests.add(1, labels)
        if prediction is None:
            return
        served = {"served_arm": attribution.served_arm, "model_version": attribution.model_version}
        self._latency.record(latency_seconds, served)
        self._score.record(prediction.fraud_probability, served)
        if prediction.fraud_prediction == 1:
            self._positive.add(1, served)

    def record_candidate_failure(self, arm: str, code: str) -> None:
        """Count a candidate failure that the control then covered."""
        self._failures.add(1, {"arm": arm, "error_code": code})

    def record_availability(self, arm: str, available: bool) -> None:
        """Count one readiness result per arm."""
        self._available.add(1, {"arm": arm, "available": str(available).lower()})


def default_ab_telemetry() -> ABTelemetry:
    """Return OpenTelemetry metrics, or a no-op when the SDK is absent."""
    try:
        return OpenTelemetryABTelemetry()
    except ImportError:
        return NoopABTelemetry()
