"""Idempotency authority invariants, checked against a reference model.

``InMemoryIdempotencyStore`` is the executable specification of the shared
contract. The state machine drives random claim, start, complete, release and
clock operations and compares every answer with an independent model, using a
fake clock so no assertion depends on real time.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import re
from threading import Barrier
import time

from hypothesis import example, given, settings, strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    rule,
)
import pytest

from ai_platform.llm_agent.idempotency import (
    MAX_RESULT_BYTES,
    Claim,
    ClaimStatus,
    IdempotencyConflict,
    IdempotencyScope,
    IdempotencyStateError,
    InMemoryIdempotencyStore,
)
from tests.property.strategies import (
    distinct_pairs,
    duplicate_counts,
    request_ids,
    result_documents,
    scope_parts,
    sha256_fingerprints,
)


LEASE_MS = 1_000
COMPLETED_TTL_MS = 5_000
MINIMUM_DURATION_MS = 1_000
MAXIMUM_DURATION_MS = 30 * 24 * 60 * 60 * 1_000
SCOPE = IdempotencyScope("tenant", "principal")
REQUEST_ID = "request-1"
FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64


class FakeClock:
    """Millisecond clock advanced explicitly by the test."""

    def __init__(self) -> None:
        self.now_ms = 0

    def __call__(self) -> int:
        return self.now_ms


def new_store() -> tuple[InMemoryIdempotencyStore, FakeClock]:
    """Build a store driven by a fake clock."""
    clock = FakeClock()
    return InMemoryIdempotencyStore(clock_ms=clock), clock


def acquire(store, scope, request_id, fingerprint, owner="owner-1") -> Claim:
    """Claim a request id and require ownership."""
    claim = store.claim(
        scope, request_id, fingerprint, owner_token=owner, lease_ms=LEASE_MS
    )
    assert claim.status is ClaimStatus.ACQUIRED
    return claim


def complete(store, scope, request_id, fingerprint, claim, result_json) -> None:
    """Move an owned claim through start to completion."""
    store.mark_started(scope, request_id, fingerprint, claim, lease_ms=LEASE_MS)
    store.complete(
        scope,
        request_id,
        fingerprint,
        claim,
        result_json=result_json,
        completed_ttl_ms=COMPLETED_TTL_MS,
    )


@given(scope_parts, scope_parts, request_ids)
def test_scope_key_is_stable_and_opaque(tenant, principal, request_id):
    key = IdempotencyScope(tenant, principal).key(request_id)
    assert key == IdempotencyScope(tenant, principal).key(request_id)
    prefix, digest = key.rsplit(":", 1)
    assert prefix == "ewallet:coordinator:idempotency:v1"
    assert re.fullmatch("[0-9a-f]{64}", digest)


@given(distinct_pairs(st.tuples(scope_parts, scope_parts, request_ids)))
def test_distinct_identities_never_share_a_key(pair):
    (tenant_a, principal_a, id_a), (tenant_b, principal_b, id_b) = pair
    assert IdempotencyScope(tenant_a, principal_a).key(id_a) != IdempotencyScope(
        tenant_b, principal_b
    ).key(id_b)


@given(st.text(min_size=129, max_size=160))
def test_request_id_length_is_bounded(request_id):
    with pytest.raises(ValueError):
        SCOPE.key(request_id)


def test_key_derivation_is_pinned_because_stored_records_depend_on_it():
    digest = hashlib.sha256(
        b"ewallet-idempotency-v1\0tenant\0principal\0request-1"
    ).hexdigest()
    assert SCOPE.key(REQUEST_ID) == f"ewallet:coordinator:idempotency:v1:{digest}"


@example(128)
@given(st.integers(min_value=1, max_value=128))
def test_request_ids_up_to_the_limit_are_accepted(length):
    assert SCOPE.key("r" * length)


def test_empty_request_id_is_rejected():
    with pytest.raises(ValueError):
        SCOPE.key("")


@given(scope_parts, scope_parts)
def test_path_traversal_is_refused_in_trusted_identities(left, right):
    with pytest.raises(ValueError):
        IdempotencyScope(f"{left}..{right}", "principal")
    with pytest.raises(ValueError):
        IdempotencyScope("tenant", f"{left}..{right}")


@example(128)
@given(st.integers(min_value=1, max_value=128))
def test_owner_tokens_up_to_the_limit_are_accepted(length):
    store, _ = new_store()
    acquire(store, SCOPE, REQUEST_ID, FINGERPRINT, owner="o" * length)


@given(st.sampled_from(["", "o" * 129, "o" * 200]))
def test_owner_tokens_outside_the_limit_are_rejected(owner):
    store, _ = new_store()
    with pytest.raises(ValueError):
        store.claim(
            SCOPE, REQUEST_ID, FINGERPRINT, owner_token=owner, lease_ms=LEASE_MS
        )


def test_durations_are_bounded_above_at_thirty_days():
    store, _ = new_store()
    claim = store.claim(
        SCOPE,
        REQUEST_ID,
        FINGERPRINT,
        owner_token="owner",
        lease_ms=MAXIMUM_DURATION_MS,
    )
    assert claim.status is ClaimStatus.ACQUIRED
    with pytest.raises(ValueError):
        store.claim(
            SCOPE,
            "other-request",
            FINGERPRINT,
            owner_token="owner",
            lease_ms=MAXIMUM_DURATION_MS + 1,
        )


def test_default_clock_reports_whole_milliseconds_since_the_epoch():
    observed = InMemoryIdempotencyStore()._clock_ms()
    assert isinstance(observed, int)
    assert abs(observed - time.time() * 1_000) < 60_000


@given(sha256_fingerprints)
def test_an_expired_unstarted_claim_passes_to_the_next_owner(fingerprint):
    store, clock = new_store()
    stale = acquire(store, SCOPE, REQUEST_ID, fingerprint, owner="first")
    clock.now_ms = LEASE_MS

    current = acquire(store, SCOPE, REQUEST_ID, fingerprint, owner="second")
    assert (current.owner_token, current.generation) == ("second", 2)

    clock.now_ms = 2 * LEASE_MS - 1
    duplicate = store.claim(
        SCOPE, REQUEST_ID, fingerprint, owner_token="third", lease_ms=LEASE_MS
    )
    assert duplicate.status is ClaimStatus.IN_PROGRESS

    with pytest.raises(IdempotencyStateError):
        store.mark_started(SCOPE, REQUEST_ID, fingerprint, stale, lease_ms=LEASE_MS)
    complete(store, SCOPE, REQUEST_ID, fingerprint, current, "{}")
    assert store.read(SCOPE, REQUEST_ID, fingerprint).status is ClaimStatus.COMPLETED


@given(sha256_fingerprints, duplicate_counts)
def test_an_active_claim_has_one_owner(fingerprint, duplicates):
    store, _ = new_store()
    acquire(store, SCOPE, REQUEST_ID, fingerprint)
    for index in range(duplicates):
        duplicate = store.claim(
            SCOPE,
            REQUEST_ID,
            fingerprint,
            owner_token=f"other-{index}",
            lease_ms=LEASE_MS,
        )
        assert duplicate.status is ClaimStatus.IN_PROGRESS
        assert duplicate.owner_token is None


@given(distinct_pairs(sha256_fingerprints), st.sampled_from(["claimed", "started", "completed"]))
def test_a_different_fingerprint_conflicts_in_every_live_state(fingerprints, state):
    first, second = fingerprints
    store, _ = new_store()
    claim = acquire(store, SCOPE, REQUEST_ID, first)
    if state != "claimed":
        store.mark_started(SCOPE, REQUEST_ID, first, claim, lease_ms=LEASE_MS)
    if state == "completed":
        store.complete(
            SCOPE,
            REQUEST_ID,
            first,
            claim,
            result_json="{}",
            completed_ttl_ms=COMPLETED_TTL_MS,
        )
    with pytest.raises(IdempotencyConflict):
        store.claim(SCOPE, REQUEST_ID, second, owner_token="other", lease_ms=LEASE_MS)
    with pytest.raises(IdempotencyConflict):
        store.read(SCOPE, REQUEST_ID, second)


@given(result_documents, sha256_fingerprints, duplicate_counts)
def test_completed_results_replay_unchanged_until_the_ttl(document, fingerprint, repeats):
    store, clock = new_store()
    result_json = json.dumps(document, sort_keys=True)
    claim = acquire(store, SCOPE, REQUEST_ID, fingerprint)
    complete(store, SCOPE, REQUEST_ID, fingerprint, claim, result_json)
    for index in range(repeats):
        clock.now_ms = (COMPLETED_TTL_MS - 1) * index // repeats
        replay = store.claim(
            SCOPE, REQUEST_ID, fingerprint, owner_token="other", lease_ms=LEASE_MS
        )
        assert replay.status is ClaimStatus.COMPLETED
        assert replay.result_json == result_json
        assert store.read(SCOPE, REQUEST_ID, fingerprint).result_json == result_json

    clock.now_ms = COMPLETED_TTL_MS
    expired = store.claim(
        SCOPE, REQUEST_ID, fingerprint, owner_token="next", lease_ms=LEASE_MS
    )
    assert (expired.status, expired.generation) == (ClaimStatus.ACQUIRED, 1)


def test_a_result_of_exactly_the_budget_is_stored():
    store, _ = new_store()
    claim = acquire(store, SCOPE, REQUEST_ID, FINGERPRINT)
    result_json = "x" * MAX_RESULT_BYTES
    complete(store, SCOPE, REQUEST_ID, FINGERPRINT, claim, result_json)
    assert store.read(SCOPE, REQUEST_ID, FINGERPRINT).result_json == result_json


@given(st.integers(min_value=MAX_RESULT_BYTES + 1, max_value=MAX_RESULT_BYTES + 64))
def test_completed_results_are_bounded(size):
    store, _ = new_store()
    claim = acquire(store, SCOPE, REQUEST_ID, FINGERPRINT)
    store.mark_started(SCOPE, REQUEST_ID, FINGERPRINT, claim, lease_ms=LEASE_MS)
    with pytest.raises(ValueError):
        store.complete(
            SCOPE,
            REQUEST_ID,
            FINGERPRINT,
            claim,
            result_json="x" * size,
            completed_ttl_ms=COMPLETED_TTL_MS,
        )


@given(st.integers(min_value=0, max_value=MINIMUM_DURATION_MS - 1))
def test_lease_durations_are_bounded(lease_ms):
    store, _ = new_store()
    with pytest.raises(ValueError):
        store.claim(
            SCOPE, REQUEST_ID, FINGERPRINT, owner_token="owner", lease_ms=lease_ms
        )


@given(st.text(max_size=70).filter(lambda value: len(value) != 64 or value != value.lower()))
def test_fingerprints_must_be_sha256_digests(fingerprint):
    store, _ = new_store()
    with pytest.raises(ValueError):
        store.claim(
            SCOPE, REQUEST_ID, fingerprint, owner_token="owner", lease_ms=LEASE_MS
        )


@given(st.integers(min_value=2, max_value=8), sha256_fingerprints)
def test_concurrent_claims_elect_exactly_one_owner(workers, fingerprint):
    store, _ = new_store()
    barrier = Barrier(workers)

    def claim(index: int) -> ClaimStatus:
        barrier.wait()
        return store.claim(
            SCOPE,
            REQUEST_ID,
            fingerprint,
            owner_token=f"owner-{index}",
            lease_ms=LEASE_MS,
        ).status

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(claim, range(workers)))
    assert statuses.count(ClaimStatus.ACQUIRED) == 1
    assert statuses.count(ClaimStatus.IN_PROGRESS) == workers - 1


@dataclass
class ReferenceRecord:
    """Independent statement of one record's expected state."""

    state: str
    owner: str
    generation: int
    lease_until: int
    result: str | None = None
    completed_until: int | None = None


class IdempotencyStoreMachine(RuleBasedStateMachine):
    """Compare the store with a reference model under random operations."""

    claims = Bundle("claims")

    def __init__(self) -> None:
        super().__init__()
        self.clock = FakeClock()
        self.store = InMemoryIdempotencyStore(clock_ms=self.clock)
        self.record: ReferenceRecord | None = None
        self.owner_sequence = 0
        self.highest_generation = 0
        self.stored_result: str | None = None

    def _expire_completed(self) -> None:
        record = self.record
        if (
            record is not None
            and record.state == "completed"
            and self.clock.now_ms >= record.completed_until
        ):
            self.record = None
            self.stored_result = None

    def _owns(self, claim: Claim, state: str) -> bool:
        record = self.record
        return (
            record is not None
            and record.state == state
            and record.owner == claim.owner_token
            and record.generation == claim.generation
        )

    def _expected_decision(self) -> ClaimStatus:
        return {
            "claimed": ClaimStatus.IN_PROGRESS,
            "started": ClaimStatus.IN_PROGRESS,
            "completed": ClaimStatus.COMPLETED,
            "ambiguous": ClaimStatus.AMBIGUOUS,
        }[self.record.state]

    def _transition_claim(self, owner: str) -> ClaimStatus:
        """Apply one claim to the model and return the expected status."""
        self._expire_completed()
        now = self.clock.now_ms
        record = self.record
        if record is None:
            self.record = ReferenceRecord("claimed", owner, 1, now + LEASE_MS)
            return ClaimStatus.ACQUIRED
        if record.state in {"completed", "ambiguous"} or now < record.lease_until:
            return self._expected_decision()
        if record.state == "started":
            record.state, record.owner, record.lease_until = "ambiguous", "", 0
            return ClaimStatus.AMBIGUOUS
        record.owner = owner
        record.generation += 1
        record.lease_until = now + LEASE_MS
        return ClaimStatus.ACQUIRED

    @rule(target=claims)
    def claim(self):
        """Claim with a new owner and compare with the model."""
        self.owner_sequence += 1
        owner = f"owner-{self.owner_sequence}"
        expected = self._transition_claim(owner)
        claim = self.store.claim(
            SCOPE, REQUEST_ID, FINGERPRINT, owner_token=owner, lease_ms=LEASE_MS
        )
        assert claim.status is expected
        if self.record is not None:
            assert claim.generation == self.record.generation
        return claim

    @rule(milliseconds=st.integers(min_value=0, max_value=3 * LEASE_MS))
    def advance_clock(self, milliseconds):
        """Move the fake clock forward."""
        self.clock.now_ms += milliseconds

    @rule(claim=claims)
    def mark_started(self, claim):
        """Start delegation; only the current owner may succeed."""
        allowed = claim.status is ClaimStatus.ACQUIRED and self._owns(claim, "claimed")
        if allowed:
            self.store.mark_started(
                SCOPE, REQUEST_ID, FINGERPRINT, claim, lease_ms=LEASE_MS
            )
            self.record.state = "started"
            self.record.lease_until = self.clock.now_ms + LEASE_MS
        else:
            with pytest.raises(IdempotencyStateError):
                self.store.mark_started(
                    SCOPE, REQUEST_ID, FINGERPRINT, claim, lease_ms=LEASE_MS
                )

    @rule(claim=claims, document=result_documents)
    def complete(self, claim, document):
        """Complete the request; only a started current owner may succeed."""
        result_json = json.dumps(document, sort_keys=True)
        allowed = claim.status is ClaimStatus.ACQUIRED and self._owns(claim, "started")
        arguments = {"result_json": result_json, "completed_ttl_ms": COMPLETED_TTL_MS}
        if allowed:
            self.store.complete(SCOPE, REQUEST_ID, FINGERPRINT, claim, **arguments)
            record = self.record
            record.state, record.owner, record.lease_until = "completed", "", 0
            record.result = result_json
            record.completed_until = self.clock.now_ms + COMPLETED_TTL_MS
            self.stored_result = result_json
        else:
            with pytest.raises(IdempotencyStateError):
                self.store.complete(SCOPE, REQUEST_ID, FINGERPRINT, claim, **arguments)

    @rule(claim=consumes(claims))
    def release_unstarted(self, claim):
        """Release a claim; only an unstarted current owner may succeed."""
        allowed = claim.status is ClaimStatus.ACQUIRED and self._owns(claim, "claimed")
        if allowed:
            self.store.release_unstarted(SCOPE, REQUEST_ID, FINGERPRINT, claim)
            self.record = None
        else:
            with pytest.raises(IdempotencyStateError):
                self.store.release_unstarted(SCOPE, REQUEST_ID, FINGERPRINT, claim)

    @rule()
    def read(self):
        """Read the decision and compare it with the model."""
        self._expire_completed()
        if self.record is None:
            with pytest.raises(IdempotencyStateError):
                self.store.read(SCOPE, REQUEST_ID, FINGERPRINT)
            return
        decision = self.store.read(SCOPE, REQUEST_ID, FINGERPRINT)
        assert decision.status is self._expected_decision()
        assert decision.owner_token is None
        assert decision.result_json == self.record.result

    @rule()
    def conflicting_claim_changes_nothing(self):
        """Require a conflict for any other fingerprint."""
        self._expire_completed()
        if self.record is None:
            return
        with pytest.raises(IdempotencyConflict):
            self.store.claim(
                SCOPE,
                REQUEST_ID,
                OTHER_FINGERPRINT,
                owner_token="intruder",
                lease_ms=LEASE_MS,
            )

    @invariant()
    def generation_never_decreases_while_the_record_lives(self):
        """Generations only move forward within one record."""
        if self.record is None:
            self.highest_generation = 0
            return
        assert self.record.generation >= self.highest_generation
        self.highest_generation = self.record.generation

    @invariant()
    def a_completed_result_is_never_rewritten(self):
        """A completed record keeps its first stored result."""
        if self.record is not None and self.record.state == "completed":
            assert self.record.result == self.stored_result


TestIdempotencyStoreMachine = IdempotencyStoreMachine.TestCase
TestIdempotencyStoreMachine.settings = settings(stateful_step_count=25)
