"""Scenario catalog shared by the mock server, Locust and the report.

The module is dependency-free on purpose. The mock server imports it from the
API environment and Locust imports it under gevent, so both agree on which
synthetic identifiers trigger which dependency failure.
"""

from __future__ import annotations

from dataclasses import dataclass


VALID = "valid"
INVALID = "invalid"
CONCURRENT = "concurrent"
DEPENDENCY_FAILURE = "dependency_failure"
HEALTH = "health"
SCENARIOS = (VALID, INVALID, CONCURRENT, DEPENDENCY_FAILURE, HEALTH)

LOCAL_PROFILE = "local"
INTEGRATION_PROFILE = "integration"
DEFAULT_MIX = "valid=60,invalid=10,concurrent=15,dependency_failure=10,health=5"
# A real deployment cannot honour the synthetic fault identifiers.
INTEGRATION_MIX = "valid=80,invalid=10,health=10"


@dataclass(frozen=True)
class DependencyFault:
    """One controlled failure and the response it must produce."""

    label: str
    backend: str
    behaviour: str
    expected_status: int
    expected_code: str

    @property
    def user_id(self) -> str:
        """User id selecting this fault in the fake feature store."""
        return f"synthetic-fault-{self.label}"

    @property
    def correlation_id(self) -> str:
        """Correlation id selecting this fault in the fake model."""
        return f"synthetic-fault-{self.label}"


FEATURE_BACKEND = "feature"
MODEL_BACKEND = "model"
DEPENDENCY_FAULTS = (
    DependencyFault("feature-outage", FEATURE_BACKEND, "backend_error", 503, "feature_backend_error"),
    DependencyFault("feature-timeout", FEATURE_BACKEND, "timeout", 504, "feature_timeout"),
    DependencyFault("feature-missing", FEATURE_BACKEND, "missing", 424, "missing_feature"),
    DependencyFault("feature-stale", FEATURE_BACKEND, "stale", 424, "stale_feature"),
    DependencyFault("model-outage", MODEL_BACKEND, "backend_error", 502, "model_backend_error"),
    DependencyFault("model-timeout", MODEL_BACKEND, "timeout", 504, "model_timeout"),
)
