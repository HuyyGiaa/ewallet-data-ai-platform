"""Fail-closed core for a bounded kagent BYO Coordinator feasibility probe.

The module deliberately does not expose an HTTP server.  A future kagent BYO
executor can adapt its A2A request/event types to this core while the existing
controller remains the only UI entrypoint.  Outbound delegation is restricted
to the approved A2A Gateway and the two existing Specialists.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from threading import Event
import time
from typing import Callable, Mapping, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_platform.llm_agent.business_request import (
    G3BusinessRequest,
    a2a_message_send,
    business_request_from_a2a,
)
from ai_platform.llm_agent.contracts import Citation
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
_SPECIALISTS = frozenset({"feature-rag-specialist", "drift-specialist"})
_LITERAL_PLACEHOLDER = re.compile(r"(?:<transaction_id>|\{transaction_id\}|TODO|REPLACE_ME)", re.I)


class CoordinatorFailure(ValueError):
    """Stable fail-closed error with no backend output fabrication."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


class A2ATransport(Protocol):
    def __call__(self, call: GatewayCall) -> Mapping[str, object]: ...


class ResponseSynthesizer(Protocol):
    def __call__(self, request: G3BusinessRequest, evidence: SpecialistEvidence) -> str: ...


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
    """Validate the immutable Gateway address and exact Specialist allowlist."""

    endpoint: str = APPROVED_A2A_GATEWAY
    timeout_seconds: float = 30.0

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

    def call_for(self, request: G3BusinessRequest) -> GatewayCall:
        if request.target_agent not in _SPECIALISTS:
            raise CoordinatorFailure("unsupported_capability", "target is not an approved Specialist")
        return GatewayCall(
            url=self.endpoint,
            headers={
                "content-type": "application/json",
                "x-kagent-host": f"{request.target_agent}.ewallet-agents",
            },
            payload=a2a_message_send(request),
            timeout_seconds=self.timeout_seconds,
        )


def _terminal_evidence(response: Mapping[str, object], request: G3BusinessRequest) -> SpecialistEvidence:
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
            request = business_request_from_a2a(inbound_a2a)
        except (ValueError, ValidationError) as exc:
            raise CoordinatorFailure("invalid_business_request", str(exc)) from exc
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
        owner_token = uuid4().hex
        ownership = self._claim_or_wait(
            scope,
            authority_id,
            request.request_id,
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

        response = self._transport(self._gateway.call_for(request))
        if cancelled and cancelled.is_set():
            raise CoordinatorFailure("cancelled", "request was cancelled during delegation")
        evidence = _terminal_evidence(response, request)
        decision_support = self._synthesizer(request, evidence).strip()
        if not decision_support or len(decision_support) > MAX_SYNTHESIS_CHARS:
            raise CoordinatorFailure("invalid_synthesis", "response synthesis is empty or too large")
        if "<tool_call>" in decision_support or "REQUEST_JSON=" in decision_support:
            raise CoordinatorFailure("invalid_synthesis", "response synthesis contains executable-looking text")
        evidence_json = json.dumps(evidence.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        for numeric_claim in re.findall(r"(?<![A-Za-z])[+-]?(?:\d+(?:\.\d+)?|\.\d+)%?", decision_support):
            if numeric_claim.rstrip("%") not in evidence_json:
                raise CoordinatorFailure("invalid_synthesis", "response synthesis introduced a new numeric claim")

        # Business values are copied from validated evidence, never reconstructed by the LLM.
        result: dict[str, object] = {
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
