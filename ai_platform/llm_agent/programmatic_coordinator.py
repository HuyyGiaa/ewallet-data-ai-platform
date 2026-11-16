"""Fail-closed core of the kagent BYO Coordinator.

The module deliberately does not expose an HTTP server; ``byo_coordinator``
adapts A2A request/event types to it. Outbound delegation is restricted to the
approved A2A Gateway and the two existing Specialists.

Three typed inputs are routed deterministically:

- ``G3BusinessRequest`` -> ``feature-rag-specialist`` (unchanged
  ``g3-orchestration/v1`` result);
- ``G3DriftRequest`` -> ``drift-specialist``;
- ``G3WorkflowRequest`` (combined) -> both Specialists concurrently.

Drift and combined workflows return ``g3-orchestration/v2``: each Specialist's
validated evidence under its own name, plus explicit ``unavailable`` entries
for a Specialist that failed. A missing result is never reconstructed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextvars
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import logging
import re
from threading import Event
import time
from typing import Callable, Mapping, Protocol, Union
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from opentelemetry import trace

from ai_platform.llm_agent.business_request import (
    G3BusinessRequest,
    G3DriftRequest,
    G3WorkflowRequest,
    SpecialistRequest,
    a2a_message_send,
    typed_request_from_a2a,
)
from ai_platform.llm_agent.agent_metrics import record_sandbox_admission_wait, track_sandbox_delegation
from ai_platform.llm_agent.contracts import Citation
from ai_platform.llm_agent.gateway_backoff import RATE_LIMITED, rate_limited_delay, retry_after_seconds
from ai_platform.llm_agent.drift_evidence import DriftFinding
from ai_platform.llm_agent.idempotency import (
    Claim,
    ClaimStatus,
    IdempotencyBackendError,
    IdempotencyConflict,
    IdempotencyScope,
    IdempotencyStateError,
    IdempotencyStore,
)


APPROVED_A2A_GATEWAY = "http://a2a-gateway.ewallet-agents.svc.cluster.local:8083/"
MAX_SPECIALIST_RESPONSE_BYTES = 16_384
MAX_A2A_RESPONSE_BYTES = 32_768
MAX_SYNTHESIS_CHARS = 512
MAX_A2A_TIMEOUT_SECONDS = 600.0
WORKFLOW_CONTRACT_VERSION = "g3-orchestration/v2"
_SPECIALISTS = frozenset({"feature-rag-specialist", "drift-specialist"})
_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_TRACER = trace.get_tracer(__name__)
_AUDIT = logging.getLogger("ewallet.agent.audit")
_LITERAL_PLACEHOLDER = re.compile(r"(?:<transaction_id>|\{transaction_id\}|TODO|REPLACE_ME)", re.I)


class CoordinatorFailure(ValueError):
    """Stable fail-closed error with no backend output fabrication."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RateLimitedFailure(CoordinatorFailure):
    """The A2A Gateway's rate limit shed a delegation with HTTP 429 before forwarding it."""

    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("a2a_rate_limited", "A2A Gateway rate limit refused the delegation")
        self.retry_after_seconds = retry_after_seconds


class SandboxCapacityFailure(RateLimitedFailure):
    """kagent refused a Sandbox delegation because the shared WorkerPool has no free worker.

    The refusal comes before any actor runs (JSON-RPC -32603 inside HTTP 200), so the delegation may be sent
    again once a worker frees up. On GKE a fixed 2-worker pool refused 2 of 6 concurrent delegations
    and 58 of 100 tasks at concurrency 8 (campaign agi-20261115T145608Z, final regression).
    """

    def __init__(self) -> None:
        CoordinatorFailure.__init__(self, "sandbox_capacity_exhausted", "Sandbox WorkerPool has no free worker")
        self.retry_after_seconds = None


_POOL_EXHAUSTED = "substrate worker pool has no free workers"


def _message_id(call: "GatewayCall") -> object:
    message = (call.payload.get("params") or {}).get("message") if isinstance(call.payload, Mapping) else None
    return message.get("messageId") if isinstance(message, Mapping) else None


def _pool_exhausted(response: Mapping[str, object]) -> bool:
    error = response.get("error") if isinstance(response, Mapping) else None
    return (isinstance(error, Mapping) and error.get("code") == -32603
            and _POOL_EXHAUSTED in str(error.get("message", "")))


@dataclass(frozen=True)
class RateLimitBackoff:
    """Bounded, jittered repeats of a delegation the A2A Gateway shed with 429.

    A shed delegation never reached a Specialist, so sending it again repeats no work. Every
    repeat stays inside the call's own timeout, and a repeat that would leave less than
    ``min_attempt_seconds`` is not started. ``max_attempts`` 1 turns repeats off.
    """

    max_attempts: int = 4
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    min_attempt_seconds: float = 1.0
    # A worker frees when an actor finishes (a cold turn took ~24 s on GKE): wait longer, still inside
    # the delegation timeout.
    capacity_max_attempts: int = 12
    capacity_base_delay_seconds: float = 2.0
    capacity_max_delay_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 6:
            raise ValueError("max_attempts must be between 1 and 6")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("backoff delays are invalid")
        if self.min_attempt_seconds <= 0:
            raise ValueError("min_attempt_seconds must be positive")

    @classmethod
    def from_env(cls, environment: Mapping[str, str]) -> "RateLimitBackoff":
        """Read the attempt budget from ``EWALLET_A2A_RATE_LIMIT_MAX_ATTEMPTS`` (unset: default)."""

        value = environment.get("EWALLET_A2A_RATE_LIMIT_MAX_ATTEMPTS", "").strip()
        return cls() if not value else cls(max_attempts=int(value))


class SpecialistEvidence(BaseModel):
    """Strict terminal business evidence returned by an existing Specialist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    request_id: str = Field(min_length=1, max_length=128)
    features: dict[str, object] = Field(default_factory=dict)
    fraud_score: dict[str, object] | None = None
    drift: list[object] = Field(default_factory=list, max_length=8)
    citations: list[dict[str, object]] = Field(default_factory=list, max_length=8)
    unavailable: list[object] = Field(default_factory=list, max_length=8)
    contract_version: str
    prompt_revision: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_status(self) -> "SpecialistEvidence":
        if self.status not in {"ok", "partial", "unavailable"}:
            raise ValueError("unsupported Specialist status")
        if self.contract_version != "g3-orchestration/v1":
            raise ValueError("unsupported Specialist contract version")
        for item in self.citations:
            Citation(**item)
        return self


@dataclass(frozen=True)
class GatewayCall:
    """One exact, authenticated-by-mesh A2A Gateway call."""

    url: str
    headers: Mapping[str, str]
    payload: Mapping[str, object]
    timeout_seconds: float
    # The Sandbox Specialist this call goes to (kagent SandboxAgent route); None for a Deployment Specialist.
    sandbox_specialist: str | None = None


class A2ATransport(Protocol):
    def __call__(self, call: GatewayCall) -> Mapping[str, object]: ...


class ResponseSynthesizer(Protocol):
    def __call__(self, request: object, evidence: object) -> str: ...


class JsonHttpTransport:
    """Bounded JSON-RPC transport; mesh identity supplies production AuthN."""

    def __init__(self, opener: Callable[..., object] = urlopen) -> None:
        self._opener = opener

    def __call__(self, call: GatewayCall) -> Mapping[str, object]:
        body = json.dumps(call.payload, separators=(",", ":")).encode("utf-8")
        request = Request(call.url, data=body, headers=dict(call.headers), method="POST")
        try:
            with self._opener(request, timeout=call.timeout_seconds) as response:
                raw = response.read(MAX_A2A_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code == RATE_LIMITED:
                raise RateLimitedFailure(retry_after_seconds(exc.headers)) from exc
            raise CoordinatorFailure("a2a_transport_error", "A2A Gateway request failed") from exc
        except (OSError, TimeoutError) as exc:
            raise CoordinatorFailure("a2a_transport_error", "A2A Gateway request failed") from exc
        if len(raw) > MAX_A2A_RESPONSE_BYTES:
            raise CoordinatorFailure("response_too_large", "A2A response exceeds its byte budget")
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CoordinatorFailure("invalid_a2a_response", "A2A Gateway returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise CoordinatorFailure("invalid_a2a_response", "A2A Gateway response must be an object")
        return decoded


@dataclass(frozen=True)
class GatewayPolicy:
    """Validate the immutable Gateway address and exact Specialist allowlist.

    Deployment Specialists are reached through the Gateway's exact ``x-kagent-host``
    routes. Specialists named in ``sandbox_specialists`` run as kagent SandboxAgents
    (Substrate actors) and are reached through the Gateway's per-agent controller route
    ``/api/a2a-sandboxes/<sandbox_namespace>/<agent>/`` (kagent 0.10.3 serves SandboxAgents there,
    not under ``/api/a2a/``). kagent selects the session actor by the
    message ``contextId``, which is the member ``request_id``, so a retried request
    resumes the same actor. The caller's ``X-User-Id`` is not sent from here: the
    Gateway sets it on that route, so a client cannot choose the principal.
    """

    endpoint: str = APPROVED_A2A_GATEWAY
    timeout_seconds: float = 30.0
    sandbox_specialists: frozenset[str] = frozenset()
    sandbox_namespace: str = ""

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "a2a-gateway.ewallet-agents.svc.cluster.local"
            or parsed.port != 8083
            or parsed.path != "/"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("A2A endpoint must be the approved in-cluster Gateway")
        if not 0.1 <= self.timeout_seconds <= MAX_A2A_TIMEOUT_SECONDS:
            raise ValueError("A2A timeout must be between 0.1 and 600 seconds")
        object.__setattr__(self, "sandbox_specialists", frozenset(self.sandbox_specialists))
        if not self.sandbox_specialists <= _SPECIALISTS:
            raise ValueError("sandbox_specialists must be approved Specialists")
        if self.sandbox_specialists and (
            not _NAMESPACE.fullmatch(self.sandbox_namespace) or self.sandbox_namespace == "ewallet-agents"
        ):
            raise ValueError("sandbox Specialists need a dedicated sandbox namespace")

    def call_for(self, request: SpecialistRequest) -> GatewayCall:
        if request.target_agent not in _SPECIALISTS:
            raise CoordinatorFailure("unsupported_capability", "target is not an approved Specialist")
        payload = a2a_message_send(request)
        if request.target_agent in self.sandbox_specialists:
            message = payload["params"]["message"]  # type: ignore[index]
            message["contextId"] = request.request_id
            return GatewayCall(
                url=f"{self.endpoint}api/a2a-sandboxes/{self.sandbox_namespace}/{request.target_agent}/",
                headers={"content-type": "application/json"},
                payload=payload,
                timeout_seconds=self.timeout_seconds,
                sandbox_specialist=request.target_agent,
            )
        return GatewayCall(
            url=self.endpoint,
            headers={
                "content-type": "application/json",
                "x-kagent-host": f"{request.target_agent}.ewallet-agents",
            },
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )

    @classmethod
    def from_env(cls, environment: Mapping[str, str], *, timeout_seconds: float) -> "GatewayPolicy":
        """``EWALLET_SANDBOX_SPECIALISTS`` (comma list) and ``EWALLET_SANDBOX_NAMESPACE``."""

        names = frozenset(item.strip() for item in environment.get("EWALLET_SANDBOX_SPECIALISTS", "").split(",")
                          if item.strip())
        return cls(timeout_seconds=timeout_seconds, sandbox_specialists=names,
                   sandbox_namespace=environment.get("EWALLET_SANDBOX_NAMESPACE", "").strip() if names else "")


def _terminal_evidence(response: Mapping[str, object], request: SpecialistRequest) -> SpecialistEvidence:
    """Extract one completed A2A task artifact and reject every ambiguous state."""

    try:
        if response["jsonrpc"] != "2.0" or response["id"] != request.request_id:
            raise CoordinatorFailure("invalid_a2a_response", "A2A response identity mismatch")
        result = response["result"]
        if not isinstance(result, Mapping) or result.get("kind") != "task":
            raise CoordinatorFailure("invalid_a2a_response", "A2A result must be a task")
        status = result["status"]
        if not isinstance(status, Mapping) or status.get("state") != "completed":
            raise CoordinatorFailure("specialist_not_completed", "Specialist task is not completed")
        artifacts = result["artifacts"]
        if not isinstance(artifacts, list) or len(artifacts) != 1:
            raise CoordinatorFailure("invalid_a2a_response", "exactly one terminal artifact is required")
        parts = artifacts[0]["parts"]
        if (
            not isinstance(parts, list)
            or len(parts) != 1
            or not isinstance(parts[0], Mapping)
            or parts[0].get("kind") != "text"
            or not isinstance(parts[0].get("text"), str)
        ):
            raise CoordinatorFailure("invalid_a2a_response", "terminal artifact must have one text part")
        raw = parts[0]["text"]
    except KeyError as exc:
        raise CoordinatorFailure("invalid_a2a_response", "A2A response is incomplete") from exc

    if len(raw.encode("utf-8")) > MAX_SPECIALIST_RESPONSE_BYTES:
        raise CoordinatorFailure("response_too_large", "Specialist response exceeds its byte budget")
    try:
        evidence = SpecialistEvidence.model_validate_json(raw)
    except ValidationError as exc:
        raise CoordinatorFailure("invalid_specialist_evidence", "Specialist evidence failed validation") from exc
    if evidence.request_id != request.request_id:
        raise CoordinatorFailure("invalid_specialist_evidence", "Specialist request_id mismatch")

    if isinstance(request, G3DriftRequest):
        return _validated_drift_evidence(evidence, request)
    requested = set(request.requested_tools)
    if not set(evidence.features) <= set(request.feature_names):
        raise CoordinatorFailure("invalid_specialist_evidence", "Specialist returned unrequested features")
    if evidence.fraud_score is not None and "fraud_scoring" not in requested:
        raise CoordinatorFailure("invalid_specialist_evidence", "Specialist returned an unrequested fraud score")
    if evidence.citations and "knowledge_search" not in requested:
        raise CoordinatorFailure("invalid_specialist_evidence", "Specialist returned unrequested citations")
    if evidence.drift and "drift_detection" not in requested:
        raise CoordinatorFailure("invalid_specialist_evidence", "Specialist returned unsolicited drift")
    return evidence


def _validated_drift_evidence(evidence: SpecialistEvidence, request: G3DriftRequest) -> SpecialistEvidence:
    """A drift Specialist returns only drift findings for exactly the requested inputs."""

    if evidence.features or evidence.fraud_score is not None or evidence.citations:
        raise CoordinatorFailure("invalid_specialist_evidence", "Drift Specialist returned non-drift evidence")
    if evidence.status == "unavailable":
        if evidence.drift or not evidence.unavailable:
            raise CoordinatorFailure("invalid_specialist_evidence", "Unavailable drift evidence must explain itself")
        return evidence
    if evidence.status != "ok" or len(evidence.drift) != 1 or evidence.unavailable:
        raise CoordinatorFailure("invalid_specialist_evidence", "Drift evidence must contain exactly one finding")
    item = evidence.drift[0]
    if not isinstance(item, Mapping):
        raise CoordinatorFailure("invalid_specialist_evidence", "Drift finding must be an object")
    finding = {key: value for key, value in item.items() if key != "tool_versions"}
    try:
        validated = DriftFinding.model_validate(finding)
    except ValidationError as exc:
        raise CoordinatorFailure("invalid_specialist_evidence", "Drift finding failed validation") from exc
    if not validated.matches(request):
        raise CoordinatorFailure("invalid_specialist_evidence", "Drift finding answers another request")
    return evidence


@dataclass(frozen=True)
class _MemberOutcome:
    request: SpecialistRequest
    evidence: SpecialistEvidence | None
    failure: CoordinatorFailure | None
    duration_ms: int


def _member_section(outcome: _MemberOutcome) -> dict[str, object]:
    """One Specialist's validated contribution, labelled with its own name."""

    request = outcome.request
    if outcome.evidence is None:
        assert outcome.failure is not None
        return {"agent": request.target_agent, "request_id": request.request_id, "status": "failed",
                "error": {"code": outcome.failure.code, "message": str(outcome.failure)[:256]},
                "duration_ms": outcome.duration_ms}
    evidence = outcome.evidence
    section: dict[str, object] = {
        "agent": request.target_agent,
        "request_id": request.request_id,
        "status": evidence.status,
        "specialist_revision": evidence.prompt_revision,
        "unavailable": evidence.unavailable,
        "duration_ms": outcome.duration_ms,
    }
    if isinstance(request, G3DriftRequest):
        section["drift"] = evidence.drift
    else:
        section.update({
            "features": evidence.features,
            "fraud_score": evidence.fraud_score,
            "citations": [asdict(Citation(**item)) for item in evidence.citations],
        })
    return section


class ProgrammaticCoordinator:
    """Deterministic routing with durable business-request idempotency."""

    def __init__(
        self,
        *,
        transport: A2ATransport,
        synthesizer: ResponseSynthesizer,
        idempotency_store: IdempotencyStore,
        gateway: GatewayPolicy | None = None,
        lease_ms: int = 600_000,
        completed_ttl_ms: int = 86_400_000,
        duplicate_wait_seconds: float = 15.0,
        duplicate_poll_seconds: float = 0.1,
        rate_limit_backoff: RateLimitBackoff | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1_000 <= lease_ms <= 600_000:
            raise ValueError("lease_ms must be between 1000 and 600000")
        if not 60_000 <= completed_ttl_ms <= 30 * 24 * 60 * 60 * 1_000:
            raise ValueError("completed_ttl_ms must be between one minute and 30 days")
        if not 0 <= duplicate_wait_seconds <= 60:
            raise ValueError("duplicate_wait_seconds must be between 0 and 60")
        if not 0.001 <= duplicate_poll_seconds <= 1:
            raise ValueError("duplicate_poll_seconds must be between 0.001 and 1")
        self._transport = transport
        self._synthesizer = synthesizer
        self._idempotency = idempotency_store
        self._gateway = gateway or GatewayPolicy()
        self._lease_ms = lease_ms
        self._completed_ttl_ms = completed_ttl_ms
        self._duplicate_wait_seconds = duplicate_wait_seconds
        self._duplicate_poll_seconds = duplicate_poll_seconds
        self._backoff = rate_limit_backoff or RateLimitBackoff()
        self._sleep = sleep

    def ready(self) -> None:
        """Verify that this replica can reach the shared idempotency authority."""

        self._idempotency.ready()

    @staticmethod
    def _completed_result(claim: Claim, request_id: str) -> dict[str, object]:
        if claim.status is not ClaimStatus.COMPLETED or claim.result_json is None:
            raise CoordinatorFailure("idempotency_state_invalid", "Completed result is unavailable")
        try:
            result = json.loads(claim.result_json)
        except json.JSONDecodeError as exc:
            raise CoordinatorFailure("idempotency_state_invalid", "Completed result is invalid") from exc
        if not isinstance(result, dict) or result.get("request_id") != request_id:
            raise CoordinatorFailure("idempotency_state_invalid", "Completed result identity mismatch")
        return result

    def _claim_or_wait(
        self,
        scope: IdempotencyScope,
        authority_id: str,
        result_request_id: str,
        fingerprint: str,
        owner_token: str,
        cancelled: Event | None,
    ) -> Claim | dict[str, object]:
        try:
            claim = self._idempotency.claim(
                scope,
                authority_id,
                fingerprint,
                owner_token=owner_token,
                lease_ms=self._lease_ms,
            )
            if claim.status is ClaimStatus.COMPLETED:
                return self._completed_result(claim, result_request_id)
            if claim.status is ClaimStatus.AMBIGUOUS:
                raise CoordinatorFailure(
                    "idempotency_ambiguous",
                    "Previous execution may have reached downstream systems; operator reconciliation is required",
                )
            if claim.status is ClaimStatus.ACQUIRED:
                return claim

            deadline = time.monotonic() + self._duplicate_wait_seconds
            while time.monotonic() < deadline:
                if cancelled and cancelled.is_set():
                    raise CoordinatorFailure("cancelled", "request was cancelled while awaiting its owner")
                time.sleep(self._duplicate_poll_seconds)
                current = self._idempotency.read(scope, authority_id, fingerprint)
                if current.status is ClaimStatus.COMPLETED:
                    return self._completed_result(current, result_request_id)
                if current.status is ClaimStatus.AMBIGUOUS:
                    raise CoordinatorFailure(
                        "idempotency_ambiguous",
                        "Previous execution may have reached downstream systems; operator reconciliation is required",
                    )
            raise CoordinatorFailure("idempotency_in_progress", "Identical request is already in progress")
        except IdempotencyConflict as exc:
            raise CoordinatorFailure("idempotency_conflict", str(exc)) from exc
        except IdempotencyBackendError as exc:
            raise CoordinatorFailure(
                "idempotency_backend_unavailable", "Shared idempotency authority is unavailable",
            ) from exc
        except IdempotencyStateError as exc:
            raise CoordinatorFailure("idempotency_state_invalid", str(exc)) from exc

    def _run_idempotently(
        self,
        *,
        scope: IdempotencyScope,
        authority_id: str,
        request_id: str,
        fingerprint: str,
        cancelled: Event | None,
        execute: Callable[[], dict[str, object]],
    ) -> dict[str, object]:
        owner_token = uuid4().hex
        ownership = self._claim_or_wait(
            scope,
            authority_id,
            request_id,
            fingerprint,
            owner_token,
            cancelled,
        )
        if isinstance(ownership, dict):
            return ownership
        if cancelled and cancelled.is_set():
            try:
                self._idempotency.release_unstarted(
                    scope, authority_id, fingerprint, ownership,
                )
            except (IdempotencyBackendError, IdempotencyStateError) as exc:
                raise CoordinatorFailure(
                    "idempotency_backend_unavailable", "Could not safely release the cancelled claim",
                ) from exc
            raise CoordinatorFailure("cancelled", "request was cancelled before delegation")

        try:
            self._idempotency.mark_started(
                scope,
                authority_id,
                fingerprint,
                ownership,
                lease_ms=self._lease_ms,
            )
        except IdempotencyBackendError as exc:
            raise CoordinatorFailure(
                "idempotency_backend_unavailable", "Could not persist delegation start",
            ) from exc
        except IdempotencyStateError as exc:
            raise CoordinatorFailure("idempotency_state_invalid", str(exc)) from exc

        result = execute()
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            self._idempotency.complete(
                scope,
                authority_id,
                fingerprint,
                ownership,
                result_json=result_json,
                completed_ttl_ms=self._completed_ttl_ms,
            )
        except IdempotencyBackendError as exc:
            raise CoordinatorFailure(
                "idempotency_backend_unavailable", "Could not durably record the completed result",
            ) from exc
        except IdempotencyStateError as exc:
            raise CoordinatorFailure("idempotency_state_invalid", str(exc)) from exc
        return result

    def _synthesize(self, request: object, evidence: object, evidence_json: str) -> str:
        decision_support = self._synthesizer(request, evidence).strip()
        if not decision_support or len(decision_support) > MAX_SYNTHESIS_CHARS:
            raise CoordinatorFailure("invalid_synthesis", "response synthesis is empty or too large")
        if "<tool_call>" in decision_support or "REQUEST_JSON=" in decision_support:
            raise CoordinatorFailure("invalid_synthesis", "response synthesis contains executable-looking text")
        for numeric_claim in re.findall(r"(?<![A-Za-z])[+-]?(?:\d+(?:\.\d+)?|\.\d+)%?", decision_support):
            if numeric_claim.rstrip("%") not in evidence_json:
                raise CoordinatorFailure("invalid_synthesis", "response synthesis introduced a new numeric claim")
        return decision_support

    def handle(
        self,
        inbound_a2a: object,
        *,
        scope: IdempotencyScope,
        session_id: str,
        cancelled: Event | None = None,
    ) -> dict[str, object]:
        if not session_id or len(session_id) > 128:
            raise CoordinatorFailure("invalid_session", "session_id must contain 1..128 characters")
        try:
            request = typed_request_from_a2a(inbound_a2a)
        except (ValueError, ValidationError) as exc:
            raise CoordinatorFailure("invalid_business_request", str(exc)) from exc
        if isinstance(request, G3BusinessRequest):
            return self._handle_fraud(request, scope=scope, cancelled=cancelled)
        return self._handle_workflow(request, scope=scope, cancelled=cancelled)

    def _handle_fraud(
        self,
        request: G3BusinessRequest,
        *,
        scope: IdempotencyScope,
        cancelled: Event | None,
    ) -> dict[str, object]:
        """The unchanged G3 fraud investigation path (``g3-orchestration/v1``)."""

        if _LITERAL_PLACEHOLDER.search(request.transaction.transaction_id):
            raise CoordinatorFailure("invalid_business_request", "literal transaction placeholder is forbidden")

        fingerprint = hashlib.sha256(request.to_compact_json().encode()).hexdigest()
        # The validated business subject separates identical request IDs for
        # different wallet users.  It is an idempotency dimension, not an
        # authentication authority; the trusted caller remains ``scope``.
        authority_id = hashlib.sha256(
            b"ewallet-business-subject-v1\0"
            + str(request.transaction.user_id).encode("utf-8")
            + b"\0"
            + request.request_id.encode("utf-8")
        ).hexdigest()

        def execute() -> dict[str, object]:
            with _TRACER.start_as_current_span("g3.coordinator.delegate") as span:
                _delegation_attributes(span, request)
                response = self._send(self._gateway.call_for(request), cancelled, span)
                if cancelled and cancelled.is_set():
                    raise CoordinatorFailure("cancelled", "request was cancelled during delegation")
                evidence = _terminal_evidence(response, request)
                span.set_attribute("ewallet.status", evidence.status)
            evidence_json = json.dumps(evidence.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            decision_support = self._synthesize(request, evidence, evidence_json)
            # Business values are copied from validated evidence, never reconstructed by the LLM.
            return {
                "status": evidence.status,
                "request_id": request.request_id,
                "decision_support": decision_support,
                "features": evidence.features,
                "fraud_score": evidence.fraud_score,
                "drift": evidence.drift,
                "citations": [asdict(Citation(**item)) for item in evidence.citations],
                "unavailable": evidence.unavailable,
                "contract_version": "g3-orchestration/v1",
                "coordinator_revision": "programmatic-coordinator/prototype-v1",
            }

        return self._run_idempotently(scope=scope, authority_id=authority_id, request_id=request.request_id,
                                      fingerprint=fingerprint, cancelled=cancelled, execute=execute)

    def _send(self, call: GatewayCall, cancelled: Event | None, span: trace.Span) -> Mapping[str, object]:
        """Send one delegation, waiting and repeating while the A2A Gateway sheds it (429).

        A Sandbox delegation is one in-flight unit from here until it ends (P2-D), whatever the retries.
        """

        if call.sandbox_specialist is None:
            return self._send_attempts(call, cancelled, span)
        with track_sandbox_delegation(call.sandbox_specialist):
            return self._send_attempts(call, cancelled, span)

    def _send_attempts(self, call: GatewayCall, cancelled: Event | None, span: trace.Span) -> Mapping[str, object]:
        deadline = time.monotonic() + call.timeout_seconds
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._transport(call)
                if _pool_exhausted(response):
                    raise SandboxCapacityFailure()
                return response
            except RateLimitedFailure as exc:
                capacity = isinstance(exc, SandboxCapacityFailure)
                if capacity and call.sandbox_specialist is not None:
                    record_sandbox_admission_wait(call.sandbox_specialist)
                budget = self._backoff.capacity_max_attempts if capacity else self._backoff.max_attempts
                if attempt >= budget or self._backoff.max_attempts == 1:
                    raise
                wait = rate_limited_delay(
                    attempt,
                    base_seconds=self._backoff.capacity_base_delay_seconds if capacity else self._backoff.base_delay_seconds,
                    max_seconds=self._backoff.capacity_max_delay_seconds if capacity else self._backoff.max_delay_seconds,
                    retry_after=exc.retry_after_seconds)
                remaining = deadline - time.monotonic() - wait
                if remaining < self._backoff.min_attempt_seconds:
                    raise
                span.add_event("a2a.sandbox_capacity" if capacity else "a2a.rate_limited",
                               {"ewallet.attempt": attempt, "ewallet.wait_ms": int(wait * 1000)})
                # One line per wait, so admission refusals and waiting time are countable from the logs.
                _AUDIT.info("coordinator.delegation_wait code=%s attempt=%d wait_ms=%d request_id=%s", exc.code, attempt,
                            int(wait * 1000), str(_message_id(call))[:128])
                if cancelled is not None:
                    if cancelled.wait(wait):
                        raise CoordinatorFailure("cancelled", "request was cancelled during delegation") from exc
                else:
                    self._sleep(wait)
                call = replace(call, timeout_seconds=remaining)

    def _delegate(self, request: SpecialistRequest, cancelled: Event | None) -> _MemberOutcome:
        started = time.monotonic()
        with _TRACER.start_as_current_span("g3.coordinator.delegate") as span:
            _delegation_attributes(span, request)
            try:
                response = self._send(self._gateway.call_for(request), cancelled, span)
                if cancelled and cancelled.is_set():
                    raise CoordinatorFailure("cancelled", "request was cancelled during delegation")
                evidence = _terminal_evidence(response, request)
            except CoordinatorFailure as exc:
                span.set_attribute("ewallet.status", exc.code)
                return _MemberOutcome(request, None, exc, int((time.monotonic() - started) * 1000))
            span.set_attribute("ewallet.status", evidence.status)
            return _MemberOutcome(request, evidence, None, int((time.monotonic() - started) * 1000))

    def _handle_workflow(
        self,
        request: Union[G3DriftRequest, G3WorkflowRequest],
        *,
        scope: IdempotencyScope,
        cancelled: Event | None,
    ) -> dict[str, object]:
        """Drift-only or combined workflow; Specialists run concurrently through the A2A Gateway."""

        members: tuple[SpecialistRequest, ...]
        if isinstance(request, G3WorkflowRequest):
            members, workflow = request.members, "combined"
            if _LITERAL_PLACEHOLDER.search(request.fraud.transaction.transaction_id):
                raise CoordinatorFailure("invalid_business_request", "literal transaction placeholder is forbidden")
            subject = str(request.fraud.transaction.user_id)
        else:
            members, workflow = (request,), "drift"
            subject = f"drift:{request.baseline_id}:{request.feature_name}"
        fingerprint = hashlib.sha256(request.to_compact_json().encode()).hexdigest()
        authority_id = hashlib.sha256(
            b"ewallet-business-subject-v1\0" + subject.encode("utf-8") + b"\0" + request.request_id.encode("utf-8")
        ).hexdigest()

        def execute() -> dict[str, object]:
            # Each worker runs in a copy of this context, so trace context reaches the A2A call.
            with ThreadPoolExecutor(max_workers=len(members), thread_name_prefix="g3-delegate") as pool:
                futures = [pool.submit(contextvars.copy_context().run, self._delegate, member, cancelled)
                           for member in members]
                outcomes = [future.result() for future in futures]
            if cancelled and cancelled.is_set():
                raise CoordinatorFailure("cancelled", "request was cancelled during delegation")
            failures = [outcome for outcome in outcomes if outcome.failure is not None]
            if len(failures) == len(outcomes):
                first = failures[0].failure
                assert first is not None
                if len(failures) == 1:
                    raise first
                raise CoordinatorFailure("specialists_unavailable", "No Specialist returned validated evidence")
            specialists = {outcome.request.target_agent: _member_section(outcome) for outcome in outcomes}
            unavailable: list[dict[str, object]] = []
            for outcome in outcomes:
                agent = outcome.request.target_agent
                if outcome.failure is not None:
                    unavailable.append({"agent": agent, "code": outcome.failure.code,
                                        "message": str(outcome.failure)[:256]})
                elif outcome.evidence is not None:
                    unavailable.extend({"agent": agent, "detail": item} for item in outcome.evidence.unavailable)
            statuses = [section["status"] for section in specialists.values()]
            status = (
                "complete" if all(item == "ok" for item in statuses)
                else "unavailable" if all(item in {"unavailable", "failed"} for item in statuses)
                else "partial"
            )
            evidence_json = json.dumps(specialists, sort_keys=True, separators=(",", ":"), default=str)
            decision_support = self._synthesize(request, specialists, evidence_json)
            return {
                "status": status,
                "request_id": request.request_id,
                "workflow": workflow,
                "decision_support": decision_support,
                "specialists": specialists,
                "unavailable": unavailable,
                "contract_version": WORKFLOW_CONTRACT_VERSION,
                "coordinator_revision": "programmatic-coordinator/workflow-v1",
            }

        return self._run_idempotently(scope=scope, authority_id=authority_id, request_id=request.request_id,
                                      fingerprint=fingerprint, cancelled=cancelled, execute=execute)


def _delegation_attributes(span: trace.Span, request: SpecialistRequest) -> None:
    """Non-sensitive correlation attributes for one Coordinator -> Specialist hop."""

    span.set_attribute("g3.request_id", request.request_id)
    span.set_attribute("ewallet.agent", "fraud-coordinator")
    span.set_attribute("ewallet.specialist", request.target_agent)
    span.set_attribute("ewallet.gateway", "a2a-gateway")
    span.set_attribute("ewallet.capability", request.capabilities[0])
    span.set_attribute("ewallet.requested_tools", list(request.requested_tools))


def encode_specialist_task(request_id: str, evidence: Mapping[str, object]) -> dict[str, object]:
    """Test/adapter helper that mirrors the A2A completed-task wire shape."""

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "kind": "task",
            "id": f"task-{request_id}",
            "status": {"state": "completed"},
            "artifacts": [{
                "artifactId": f"artifact-{request_id}",
                "parts": [{"kind": "text", "text": json.dumps(evidence, separators=(",", ":"))}],
            }],
        },
    }
