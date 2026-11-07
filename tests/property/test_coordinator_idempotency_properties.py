"""Business-request idempotency of the programmatic Coordinator.

The Coordinator is the only component that promises at-most-once delegation
for a scoped request id. These properties use an injected transport and the
in-memory authority; nothing reaches an agent, gateway or Redis.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from hypothesis import assume, given, strategies as st
import pytest

from ai_platform.llm_agent.business_request import a2a_message_send
from ai_platform.llm_agent.idempotency import (
    IdempotencyScope,
    InMemoryIdempotencyStore,
)
from ai_platform.llm_agent.programmatic_coordinator import (
    CoordinatorFailure,
    ProgrammaticCoordinator,
    encode_specialist_task,
)
from tests.llm_agent.test_g3_programmatic_coordinator import (
    SCOPE,
    TRANSACTION,
    Recorder,
    evidence,
    request,
)
from tests.property.strategies import distinct_pairs, duplicate_counts, request_ids
from tests.property.test_idempotency_store_properties import FakeClock


LEASE_MS = 1_000
SESSION = "session-a"
amounts = st.floats(
    min_value=0.01, max_value=1e9, allow_nan=False, allow_infinity=False
)
user_ids = st.uuids().map(str)


class FailingTransport:
    """Transport that records each delegation and then loses the reply."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, call):
        self.calls += 1
        raise ConnectionError("synthetic gateway failure")


def business_request(request_id: str, *, amount: float | None = None, user_id=None):
    """Build a request, overriding only the given transaction fields."""
    transaction = dict(TRANSACTION)
    if amount is not None:
        transaction["amount"] = amount
    if user_id is not None:
        transaction["user_id"] = user_id
    return request(request_id=request_id, transaction=transaction)


def coordinator(transport, store=None) -> ProgrammaticCoordinator:
    """Build a Coordinator that never waits for a duplicate's owner."""
    return ProgrammaticCoordinator(
        transport=transport,
        synthesizer=lambda _request, _evidence: "Evidence validated.",
        idempotency_store=store or InMemoryIdempotencyStore(),
        lease_ms=LEASE_MS,
        duplicate_wait_seconds=0,
    )


def replying_transport(req) -> Recorder:
    """Build a recording transport answering with valid evidence."""
    return Recorder(encode_specialist_task(req.request_id, evidence(req)))


def handle(runtime, req, scope=SCOPE, cancelled=None):
    """Send ``req`` to the Coordinator as an A2A message."""
    return runtime.handle(
        a2a_message_send(req), scope=scope, session_id=SESSION, cancelled=cancelled
    )


def failure_code(runtime, req) -> str:
    """Return the code of the failure ``req`` must produce."""
    with pytest.raises(CoordinatorFailure) as raised:
        handle(runtime, req)
    return raised.value.code


@given(request_ids, duplicate_counts)
def test_duplicates_replay_the_result_without_delegating_again(request_id, repeats):
    req = business_request(request_id)
    transport = replying_transport(req)
    runtime = coordinator(transport)
    first = handle(runtime, req)
    for _ in range(repeats):
        assert handle(runtime, req) == first
    assert len(transport.calls) == 1


@given(request_ids, distinct_pairs(amounts))
def test_a_reused_id_with_a_changed_payload_is_a_conflict(request_id, values):
    original = business_request(request_id, amount=values[0])
    changed = business_request(request_id, amount=values[1])
    assume(original.to_compact_json() != changed.to_compact_json())
    transport = replying_transport(original)
    runtime = coordinator(transport)
    first = handle(runtime, original)

    assert failure_code(runtime, changed) == "idempotency_conflict"
    assert len(transport.calls) == 1
    assert handle(runtime, original) == first


@given(request_ids, distinct_pairs(user_ids))
def test_the_same_id_for_different_users_is_independent(request_id, users):
    first_request = business_request(request_id, user_id=users[0])
    second_request = business_request(request_id, user_id=users[1])
    store = InMemoryIdempotencyStore()
    first_transport = replying_transport(first_request)
    second_transport = replying_transport(second_request)
    handle(coordinator(first_transport, store), first_request)
    handle(coordinator(second_transport, store), second_request)
    assert len(first_transport.calls) == len(second_transport.calls) == 1


@given(request_ids, distinct_pairs(st.sampled_from(["caller-a", "caller-b", "caller-c"])))
def test_trusted_callers_have_separate_idempotency_scopes(request_id, principals):
    req = business_request(request_id)
    transport = replying_transport(req)
    runtime = coordinator(transport)
    for principal in principals:
        handle(runtime, req, IdempotencyScope("ewallet-agents", principal))
    assert len(transport.calls) == 2


@given(request_ids, duplicate_counts)
def test_a_lost_delegation_is_never_executed_twice(request_id, retries):
    req = business_request(request_id)
    clock = FakeClock()
    transport = FailingTransport()
    runtime = coordinator(transport, InMemoryIdempotencyStore(clock_ms=clock))

    with pytest.raises(ConnectionError):
        handle(runtime, req)
    for _ in range(retries):
        assert failure_code(runtime, req) == "idempotency_in_progress"

    clock.now_ms = LEASE_MS
    for _ in range(retries):
        assert failure_code(runtime, req) == "idempotency_ambiguous"
    assert transport.calls == 1


@given(request_ids)
def test_cancellation_before_delegation_allows_a_clean_retry(request_id):
    req = business_request(request_id)
    transport = replying_transport(req)
    runtime = coordinator(transport)
    cancelled = Event()
    cancelled.set()

    with pytest.raises(CoordinatorFailure) as raised:
        handle(runtime, req, cancelled=cancelled)
    assert raised.value.code == "cancelled"
    assert transport.calls == []

    assert handle(runtime, req)["request_id"] == req.request_id
    assert len(transport.calls) == 1


@given(request_ids, st.integers(min_value=2, max_value=6))
def test_concurrent_duplicates_delegate_exactly_once(request_id, workers):
    req = business_request(request_id)
    transport = replying_transport(req)
    runtime = coordinator(transport)
    barrier = Barrier(workers)

    def attempt(_index: int) -> dict | str:
        barrier.wait()
        try:
            return handle(runtime, req)
        except CoordinatorFailure as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(attempt, range(workers)))

    assert len(transport.calls) == 1
    results = [item for item in outcomes if isinstance(item, dict)]
    assert results and all(item == results[0] for item in results)
    assert {item for item in outcomes if isinstance(item, str)} <= {
        "idempotency_in_progress"
    }
