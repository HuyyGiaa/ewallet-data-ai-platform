"""End-to-end prediction invariants through the real application.

The Fraud API holds no idempotency store. Its repeat guarantee is purely
functional: the same inputs and backend state give the same score. A reused
``transaction_id`` with a different payload is scored independently and is not
a conflict; conflict detection exists only in the Coordinator.
"""

from __future__ import annotations

from datetime import datetime

from hypothesis import given, strategies as st

from tests.inference.support import FEATURE_TIME
from tests.property.assertions import (
    assert_sanitized_validation_error,
    assert_successful_prediction,
    without_correlation,
)
from tests.property.fraud_api_harness import (
    fraud_app,
    post_concurrently,
    post_prediction,
)
from tests.property.strategies import (
    duplicate_counts,
    invalid_payloads,
    positive_amounts,
    utc_offsets,
    valid_payloads,
)


APP = fraud_app()


@given(valid_payloads())
def test_valid_requests_produce_a_bounded_prediction(payload):
    status, headers, body = post_prediction(APP, payload, "corr-valid")
    assert status == 200
    assert_successful_prediction(body, payload["transaction_id"])
    assert body["correlation_id"] == headers["x-correlation-id"] == "corr-valid"


@given(valid_payloads())
def test_feature_timestamp_is_the_stored_feature_time(payload):
    _, _, body = post_prediction(APP, payload)
    assert datetime.fromisoformat(
        body["feature_timestamp"].replace("Z", "+00:00")
    ) == FEATURE_TIME


@given(invalid_payloads())
def test_invalid_requests_are_rejected_before_scoring(payload):
    status, _, body = post_prediction(APP, payload)
    assert_sanitized_validation_error(status, body)


@given(valid_payloads(), duplicate_counts)
def test_repeating_a_request_returns_an_identical_response(payload, repeats):
    first = post_prediction(APP, payload, "corr-repeat")
    for _ in range(repeats):
        assert post_prediction(APP, payload, "corr-repeat")[2] == first[2]


@given(valid_payloads(), st.text("abcdef0123456789", min_size=1, max_size=32))
def test_correlation_id_is_the_only_field_that_varies(payload, correlation_id):
    _, _, first = post_prediction(APP, payload, "corr-baseline")
    _, _, second = post_prediction(APP, payload, correlation_id)
    assert without_correlation(first) == without_correlation(second)


@given(valid_payloads(), utc_offsets)
def test_the_same_instant_in_another_offset_scores_identically(payload, offset):
    moved = datetime.fromisoformat(payload["event_timestamp"]).astimezone(offset)
    _, _, first = post_prediction(APP, payload)
    _, _, second = post_prediction(
        APP, {**payload, "event_timestamp": moved.isoformat()}
    )
    assert first == second


@given(valid_payloads(), positive_amounts)
def test_reused_transaction_id_is_scored_independently(payload, other_amount):
    """Documents the absence of dedupe: no conflict, no cross-request state."""
    conflicting = {**payload, "amount": other_amount}
    expected = post_prediction(fraud_app(), conflicting)
    post_prediction(APP, payload)
    observed = post_prediction(APP, conflicting)
    assert observed[0] == expected[0] == 200
    assert observed[2] == expected[2]


@given(valid_payloads(), st.integers(min_value=2, max_value=8))
def test_concurrent_identical_requests_agree_and_keep_their_correlation(
    payload, count
):
    calls = [(payload, f"corr-{index}") for index in range(count)]
    responses = post_concurrently(APP, calls)
    assert {response[0] for response in responses} == {200}
    baseline = without_correlation(responses[0][2])
    for index, (_, headers, body) in enumerate(responses):
        assert without_correlation(body) == baseline
        assert body["correlation_id"] == headers["x-correlation-id"] == f"corr-{index}"


@given(st.lists(valid_payloads(), min_size=2, max_size=6))
def test_concurrent_distinct_requests_match_their_sequential_results(payloads):
    sequential = [post_prediction(APP, payload)[2] for payload in payloads]
    concurrent = post_concurrently(
        APP, [(payload, "corr-property") for payload in payloads]
    )
    assert [response[2] for response in concurrent] == sequential
