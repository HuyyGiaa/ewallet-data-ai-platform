"""Failure handling is deterministic and leaves no state behind."""

from __future__ import annotations

from datetime import timedelta

from hypothesis import given, strategies as st

from ai_platform.inference import errors
from ai_platform.inference.errors import InferenceError
from ai_platform.inference.feast_online import DEFAULT_MAX_STALENESS
from tests.inference.support import (
    FEATURE_BACKEND_ERROR,
    FEATURE_MISSING,
    FEATURE_STALE,
    FEATURE_TIME,
    FEATURE_TIMEOUT,
    MODEL_BACKEND_ERROR,
    MODEL_TIMEOUT,
    DeterministicModelTransport,
    EntityFeatureStore,
)
from tests.property.assertions import assert_successful_prediction
from tests.property.fraud_api_harness import fraud_app, post_prediction
from tests.property.strategies import (
    canonical_ids,
    duplicate_counts,
    utc_offsets,
    valid_payloads,
)


FAULT_USER = "__synthetic_fault_user__"
FAULT_CORRELATION = "corr-synthetic-fault"
FEATURE_FAULTS = {
    FEATURE_BACKEND_ERROR: (503, "feature_backend_error", True),
    FEATURE_TIMEOUT: (504, "feature_timeout", True),
    FEATURE_MISSING: (424, "missing_feature", False),
    FEATURE_STALE: (424, "stale_feature", False),
}
MODEL_FAULTS = {
    MODEL_BACKEND_ERROR: (502, "model_backend_error", True),
    MODEL_TIMEOUT: (504, "model_timeout", True),
}
MAX_FRESH_SECONDS = int(DEFAULT_MAX_STALENESS.total_seconds())


def error_summary(response) -> tuple[int, str, bool]:
    """Reduce a response to its status, code and retry flag."""
    status, _, body = response
    return status, body["error"]["code"], body["error"]["retryable"]


def _inference_error_classes() -> list[type[InferenceError]]:
    return [
        item
        for item in vars(errors).values()
        if isinstance(item, type) and issubclass(item, InferenceError)
    ]


@given(st.sampled_from(sorted(FEATURE_FAULTS)), valid_payloads(), duplicate_counts)
def test_feature_faults_always_map_to_the_same_error(fault, payload, repeats):
    app = fraud_app(store=EntityFeatureStore(entity_faults={FAULT_USER: fault}))
    request = {**payload, "user_id": FAULT_USER}
    for _ in range(repeats):
        assert error_summary(post_prediction(app, request)) == FEATURE_FAULTS[fault]


@given(st.sampled_from(sorted(MODEL_FAULTS)), valid_payloads(), duplicate_counts)
def test_model_faults_always_map_to_the_same_error(fault, payload, repeats):
    app = fraud_app(
        transport=DeterministicModelTransport(
            correlation_faults={FAULT_CORRELATION: fault}
        )
    )
    for _ in range(repeats):
        response = post_prediction(app, payload, FAULT_CORRELATION)
        assert error_summary(response) == MODEL_FAULTS[fault]


@given(st.sampled_from(sorted(FEATURE_FAULTS)), valid_payloads())
def test_a_failed_request_does_not_affect_the_next_one(fault, payload):
    app = fraud_app(store=EntityFeatureStore(entity_faults={FAULT_USER: fault}))
    baseline = post_prediction(fraud_app(), payload)
    post_prediction(app, {**payload, "user_id": FAULT_USER})
    assert post_prediction(app, payload) == baseline


@given(st.sampled_from(sorted(MODEL_FAULTS)), valid_payloads())
def test_a_model_failure_does_not_affect_the_next_request(fault, payload):
    app = fraud_app(
        transport=DeterministicModelTransport(
            correlation_faults={FAULT_CORRELATION: fault}
        )
    )
    post_prediction(app, payload, FAULT_CORRELATION)
    status, _, body = post_prediction(app, payload, "corr-recovered")
    assert status == 200
    assert_successful_prediction(body, payload["transaction_id"])


@given(st.sampled_from(sorted(FEATURE_FAULTS)), valid_payloads(), canonical_ids)
def test_error_bodies_never_echo_request_values(fault, payload, marker):
    app = fraud_app(store=EntityFeatureStore(entity_faults={FAULT_USER: fault}))
    request = {**payload, "user_id": FAULT_USER, "device_id": f"pii-{marker}"}
    _, _, body = post_prediction(app, request)
    assert f"pii-{marker}" not in str(body)
    assert FAULT_USER not in str(body)


@given(valid_payloads(), st.integers(0, MAX_FRESH_SECONDS), utc_offsets)
def test_features_up_to_the_staleness_limit_are_served(payload, age, offset):
    event_time = (FEATURE_TIME + timedelta(seconds=age)).astimezone(offset)
    status, _, _ = post_prediction(
        fraud_app(), {**payload, "event_timestamp": event_time.isoformat()}
    )
    assert status == 200


@given(valid_payloads(), st.integers(1, 10**7), utc_offsets)
def test_features_beyond_the_staleness_limit_are_refused(payload, excess, offset):
    event_time = (
        FEATURE_TIME + DEFAULT_MAX_STALENESS + timedelta(seconds=excess)
    ).astimezone(offset)
    response = post_prediction(
        fraud_app(), {**payload, "event_timestamp": event_time.isoformat()}
    )
    assert error_summary(response) == (424, "stale_feature", False)


@given(valid_payloads(), st.integers(1, 10**7), utc_offsets)
def test_features_newer_than_the_event_are_refused(payload, lead, offset):
    event_time = (FEATURE_TIME - timedelta(seconds=lead)).astimezone(offset)
    response = post_prediction(
        fraud_app(), {**payload, "event_timestamp": event_time.isoformat()}
    )
    assert error_summary(response) == (424, "invalid_feature_timestamp", False)


def test_error_codes_are_unique_and_retryable_errors_are_transient():
    classes = [item for item in _inference_error_classes() if item is not InferenceError]
    codes = [item.code for item in classes]
    assert len(codes) == len(set(codes))
    for item in classes:
        assert 400 <= item.status_code < 600
        if item.retryable:
            assert item.status_code >= 500
