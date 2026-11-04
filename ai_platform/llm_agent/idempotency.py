"""Durable business-request idempotency contracts.

The production implementation lives in :mod:`redis_idempotency`.  The in-memory
store is intentionally limited to deterministic tests and explicit local
injection; it is never selected by the production runtime factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import re
from threading import Lock
import time
from typing import Callable, Protocol


MAX_RESULT_BYTES = 32_768
_SCOPE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")


class IdempotencyError(RuntimeError):
    """Base failure for shared idempotency state."""


class IdempotencyConflict(IdempotencyError):
    """A scoped request ID was reused with different canonical input."""


class IdempotencyStateError(IdempotencyError):
    """An owner attempted an invalid or stale state transition."""


class IdempotencyBackendError(IdempotencyError):
    """The shared authority could not prove the request state."""


class ClaimStatus(str, Enum):
    ACQUIRED = "acquired"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class IdempotencyScope:
    tenant: str
    principal: str

    def __post_init__(self) -> None:
        for name, value in (("tenant", self.tenant), ("principal", self.principal)):
            if not _SCOPE_PART.fullmatch(value) or ".." in value:
                raise ValueError(f"{name} must be a bounded trusted identity")

    def key(self, request_id: str) -> str:
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id must contain 1..128 characters")
        digest = hashlib.sha256(
            b"ewallet-idempotency-v1\0"
            + self.tenant.encode("utf-8")
            + b"\0"
            + self.principal.encode("utf-8")
            + b"\0"
            + request_id.encode("utf-8")
        ).hexdigest()
        return f"ewallet:coordinator:idempotency:v1:{digest}"


@dataclass(frozen=True)
class Claim:
    status: ClaimStatus
    owner_token: str | None
    generation: int
    result_json: str | None = None


class IdempotencyStore(Protocol):
    def ready(self) -> None: ...

    def claim(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
        *,
        owner_token: str,
        lease_ms: int,
    ) -> Claim: ...

    def read(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
    ) -> Claim: ...

    def mark_started(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
        claim: Claim,
        *,
        lease_ms: int,
    ) -> None: ...

    def complete(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
        claim: Claim,
        *,
        result_json: str,
        completed_ttl_ms: int,
    ) -> None: ...

    def release_unstarted(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
        claim: Claim,
    ) -> None: ...


@dataclass
class _Record:
    fingerprint: str
    state: str
    owner_token: str
    generation: int
    lease_until_ms: int
    result_json: str | None = None
    completed_until_ms: int | None = None


class InMemoryIdempotencyStore:
    """Thread-safe executable specification used by tests only."""

    def __init__(self, *, clock_ms: Callable[[], int] | None = None) -> None:
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._records: dict[str, _Record] = {}
        self._lock = Lock()

    def ready(self) -> None:
        """The in-process executable specification is always locally reachable."""

    @staticmethod
    def _validate(fingerprint: str, owner_token: str | None = None) -> None:
        if not _FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("fingerprint must be a lowercase SHA-256 digest")
        if owner_token is not None and (not owner_token or len(owner_token) > 128):
            raise ValueError("owner_token must contain 1..128 characters")

    @staticmethod
    def _validate_duration(value: int, name: str) -> None:
        if not 1_000 <= value <= 30 * 24 * 60 * 60 * 1_000:
            raise ValueError(f"{name} is outside the bounded duration")

    @staticmethod
    def _decision(record: _Record) -> Claim:
        status = {
            "claimed": ClaimStatus.IN_PROGRESS,
            "started": ClaimStatus.IN_PROGRESS,
            "completed": ClaimStatus.COMPLETED,
            "ambiguous": ClaimStatus.AMBIGUOUS,
        }[record.state]
        return Claim(status, None, record.generation, record.result_json)

    def _record(self, key: str, fingerprint: str, now: int) -> _Record | None:
        record = self._records.get(key)
        if record and record.state == "completed" and record.completed_until_ms is not None:
            if now >= record.completed_until_ms:
                del self._records[key]
                record = None
        if record and record.fingerprint != fingerprint:
            raise IdempotencyConflict("request_id was reused with different canonical input")
        return record

    def claim(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
        *,
        owner_token: str,
        lease_ms: int,
    ) -> Claim:
        self._validate(fingerprint, owner_token)
        self._validate_duration(lease_ms, "lease_ms")
        key = scope.key(request_id)
        now = self._clock_ms()
        with self._lock:
            record = self._record(key, fingerprint, now)
            if record is None:
                self._records[key] = _Record(
                    fingerprint, "claimed", owner_token, 1, now + lease_ms,
                )
                return Claim(ClaimStatus.ACQUIRED, owner_token, 1)
            if record.state in {"completed", "ambiguous"}:
                return self._decision(record)
            if now < record.lease_until_ms:
                return self._decision(record)
            if record.state == "started":
                record.state = "ambiguous"
                record.owner_token = ""
                record.lease_until_ms = 0
                return self._decision(record)
            record.owner_token = owner_token
            record.generation += 1
            record.lease_until_ms = now + lease_ms
            return Claim(ClaimStatus.ACQUIRED, owner_token, record.generation)

    def read(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
    ) -> Claim:
        self._validate(fingerprint)
        key = scope.key(request_id)
        now = self._clock_ms()
        with self._lock:
            record = self._record(key, fingerprint, now)
            if record is None:
                raise IdempotencyStateError("idempotency record disappeared before completion")
            return self._decision(record)

    @staticmethod
    def _owned(record: _Record, fingerprint: str, claim: Claim, state: str) -> bool:
        return (
            record.fingerprint == fingerprint
            and record.state == state
            and claim.status is ClaimStatus.ACQUIRED
            and record.owner_token == claim.owner_token
            and record.generation == claim.generation
        )

    def mark_started(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
        claim: Claim,
        *,
        lease_ms: int,
    ) -> None:
        self._validate_duration(lease_ms, "lease_ms")
        key = scope.key(request_id)
        with self._lock:
            record = self._records.get(key)
            if record is None or not self._owned(record, fingerprint, claim, "claimed"):
                raise IdempotencyStateError("claim ownership was lost before delegation")
            record.state = "started"
            record.lease_until_ms = self._clock_ms() + lease_ms

    def complete(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
        claim: Claim,
        *,
        result_json: str,
        completed_ttl_ms: int,
    ) -> None:
        self._validate_duration(completed_ttl_ms, "completed_ttl_ms")
        if len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("completed result exceeds its byte budget")
        key = scope.key(request_id)
        with self._lock:
            record = self._records.get(key)
            if record is None or not self._owned(record, fingerprint, claim, "started"):
                raise IdempotencyStateError("stale owner cannot complete the request")
            record.state = "completed"
            record.result_json = result_json
            record.completed_until_ms = self._clock_ms() + completed_ttl_ms
            record.owner_token = ""
            record.lease_until_ms = 0

    def release_unstarted(
        self,
        scope: IdempotencyScope,
        request_id: str,
        fingerprint: str,
        claim: Claim,
    ) -> None:
        key = scope.key(request_id)
        with self._lock:
            record = self._records.get(key)
            if record is None or not self._owned(record, fingerprint, claim, "claimed"):
                raise IdempotencyStateError("only the active pre-delegation owner may release")
            del self._records[key]
