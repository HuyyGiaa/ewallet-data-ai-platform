"""Validated, bounded ingress contracts for the G3 Coordinator and its Specialists.

Three typed requests share one deterministic two-line wire protocol:

- ``G3BusinessRequest`` (``g3-business-request/v1``): one fraud investigation for
  ``feature-rag-specialist``. Unchanged since G3.
- ``G3DriftRequest`` (``g3-drift-request/v1``): one PSI drift analysis for
  ``drift-specialist``. Every business argument (feature, baseline revision and
  current window) is explicit; nothing is defaulted.
- ``G3WorkflowRequest`` (``g3-workflow-request/v1``): a bounded composite that
  carries exactly one fraud and one drift request for a combined investigation.
  It is sent to the Coordinator only; Specialists never receive it.

This module owns the serialization used at the A2A boundary; it is not an
orchestration runtime.
"""

from __future__ import annotations

from datetime import timedelta
import json
import re
from typing import Annotated, Literal, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS


BUSINESS_REQUEST_VERSION = "g3-business-request/v1"
DRIFT_REQUEST_VERSION = "g3-drift-request/v1"
WORKFLOW_REQUEST_VERSION = "g3-workflow-request/v1"
MAX_BUSINESS_REQUEST_BYTES = 2_048
MAX_WORKFLOW_REQUEST_BYTES = 4_096
WIRE_ROUTE_PREFIX = "CALL_AGENT="
WIRE_WORKFLOW_PREFIX = "CALL_WORKFLOW="
WIRE_REQUEST_PREFIX = "REQUEST_JSON="
# Same immutable-revision rule as the drift tool and the shared contracts.
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_MUTABLE_REVISIONS = frozenset({"latest", "current", "active"})
# A resource bound on one drift call, identical to tools.drift.MAX_WINDOW.
MAX_DRIFT_WINDOW = timedelta(days=31)
# Model inputs that a drift baseline may monitor (tools.drift.MONITORED_FEATURES).
MONITORED_DRIFT_FEATURES = frozenset((*HISTORICAL_FEATURE_COLUMNS, "amount"))

Capability = Literal["fraud-evidence", "drift"]
TargetAgent = Literal["feature-rag-specialist", "drift-specialist"]
BusinessTool = Literal[
    "feature_lookup", "fraud_scoring", "knowledge_search", "drift_detection"
]
RequestId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]

_FEATURES = frozenset(HISTORICAL_FEATURE_COLUMNS)
_CAPABILITY_TOOLS = {
    "fraud-evidence": frozenset({"feature_lookup", "fraud_scoring", "knowledge_search"}),
    "drift": frozenset({"feature_lookup", "drift_detection"}),
}
_CAPABILITY_AGENT = {
    "fraud-evidence": "feature-rag-specialist",
    "drift": "drift-specialist",
}


class G3BusinessRequest(BaseModel):
    """Complete request copied unchanged from Coordinator to Specialist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[BUSINESS_REQUEST_VERSION] = BUSINESS_REQUEST_VERSION
    request_id: RequestId
    target_agent: TargetAgent
    capabilities: Annotated[tuple[Capability, ...], Field(min_length=1, max_length=1)]
    requested_tools: Annotated[tuple[BusinessTool, ...], Field(min_length=1, max_length=4)]
    transaction: FraudPredictionRequest
    feature_names: Annotated[tuple[str, ...], Field(max_length=8)] = ()
    knowledge_query: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
        | None
    ) = None

    @model_validator(mode="after")
    def validate_routing_and_budget(self) -> "G3BusinessRequest":
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        if self.target_agent != _CAPABILITY_AGENT[self.capabilities[0]]:
            raise ValueError("target_agent does not match the requested capability")
        if len(set(self.requested_tools)) != len(self.requested_tools):
            raise ValueError("requested_tools must be unique")

        allowed = set().union(*(_CAPABILITY_TOOLS[item] for item in self.capabilities))
        requested = set(self.requested_tools)
        if not requested <= allowed:
            raise ValueError("requested_tools are not granted by the requested capabilities")
        if "drift_detection" in requested and "drift" not in self.capabilities:
            raise ValueError("drift_detection requires the explicit drift capability")

        if "feature_lookup" in requested:
            if not self.feature_names:
                raise ValueError("feature_lookup requires a bounded feature_names projection")
            unknown = sorted(set(self.feature_names) - _FEATURES)
            if unknown:
                raise ValueError(f"feature_names are not canonical: {unknown}")
            if len(set(self.feature_names)) != len(self.feature_names):
                raise ValueError("feature_names must be unique")
        elif self.feature_names:
            raise ValueError("feature_names are only valid with feature_lookup")

        if ("knowledge_search" in requested) != (self.knowledge_query is not None):
            raise ValueError("knowledge_query is required only for knowledge_search")
        if len(self.to_compact_json().encode("utf-8")) > MAX_BUSINESS_REQUEST_BYTES:
            raise ValueError("business request exceeds the 2048-byte context budget")
        return self

    def to_compact_json(self) -> str:
        """Stable JSON copied verbatim through the native kagent Agent tool."""

        return json.dumps(
            self.model_dump(mode="json", exclude_computed_fields=True),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    def to_wire_text(self) -> str:
        """Small deterministic protocol that makes the native route explicit."""

        return (
            f"{WIRE_ROUTE_PREFIX}{self.target_agent}\n"
            f"{WIRE_REQUEST_PREFIX}{self.to_compact_json()}"
        )


class DriftWindow(BaseModel):
    """Half-open current window [start, end) compared with the baseline reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _ordered_and_bounded(self) -> "DriftWindow":
        if self.end <= self.start:
            raise ValueError("current_window end must be after start")
        if self.end - self.start > MAX_DRIFT_WINDOW:
            raise ValueError("current_window must not exceed 31 days")
        return self


class G3DriftRequest(BaseModel):
    """One explicit drift analysis copied unchanged from Coordinator to Drift Specialist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[DRIFT_REQUEST_VERSION] = DRIFT_REQUEST_VERSION
    request_id: RequestId
    target_agent: Literal["drift-specialist"] = "drift-specialist"
    capabilities: tuple[Literal["drift"]] = ("drift",)
    requested_tools: tuple[Literal["drift_detection"]] = ("drift_detection",)
    feature_name: str
    baseline_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    current_window: DriftWindow

    @field_validator("feature_name")
    @classmethod
    def _monitored(cls, value: str) -> str:
        if value not in MONITORED_DRIFT_FEATURES:
            raise ValueError("feature_name is not a monitored model input")
        return value

    @field_validator("baseline_id")
    @classmethod
    def _immutable_baseline(cls, value: str) -> str:
        if not _REVISION.fullmatch(value) or value.lower() in _MUTABLE_REVISIONS:
            raise ValueError("baseline_id must be an immutable revision identifier")
        return value

    @model_validator(mode="after")
    def _budget(self) -> "G3DriftRequest":
        if len(self.to_compact_json().encode("utf-8")) > MAX_BUSINESS_REQUEST_BYTES:
            raise ValueError("drift request exceeds the 2048-byte context budget")
        return self

    def tool_arguments(self) -> dict[str, object]:
        """Exact ``drift_detection`` arguments; the Specialist adds nothing."""

        return {
            "feature_name": self.feature_name,
            "baseline_id": self.baseline_id,
            "current_window": self.current_window.model_dump(mode="json"),
        }

    def to_compact_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_computed_fields=True),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    def to_wire_text(self) -> str:
        return (
            f"{WIRE_ROUTE_PREFIX}{self.target_agent}\n"
            f"{WIRE_REQUEST_PREFIX}{self.to_compact_json()}"
        )


def member_request_id(workflow_request_id: str, member: Literal["fraud", "drift"]) -> str:
    """Deterministic, distinct request identity for one member of a combined workflow."""

    return f"{workflow_request_id}.{member}"


class G3WorkflowRequest(BaseModel):
    """Bounded composite for a combined investigation: one fraud and one drift member."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[WORKFLOW_REQUEST_VERSION] = WORKFLOW_REQUEST_VERSION
    request_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ]
    workflow: Literal["combined"] = "combined"
    fraud: G3BusinessRequest
    drift: G3DriftRequest

    @model_validator(mode="after")
    def _members(self) -> "G3WorkflowRequest":
        if self.fraud.capabilities != ("fraud-evidence",):
            raise ValueError("the fraud member must request the fraud-evidence capability")
        if self.fraud.request_id != member_request_id(self.request_id, "fraud"):
            raise ValueError("the fraud member request_id must be <request_id>.fraud")
        if self.drift.request_id != member_request_id(self.request_id, "drift"):
            raise ValueError("the drift member request_id must be <request_id>.drift")
        if len(self.to_compact_json().encode("utf-8")) > MAX_WORKFLOW_REQUEST_BYTES:
            raise ValueError("workflow request exceeds the 4096-byte context budget")
        return self

    @property
    def members(self) -> tuple[G3BusinessRequest, G3DriftRequest]:
        return (self.fraud, self.drift)

    def to_compact_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_computed_fields=True),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    def to_wire_text(self) -> str:
        return (
            f"{WIRE_WORKFLOW_PREFIX}{self.workflow}\n"
            f"{WIRE_REQUEST_PREFIX}{self.to_compact_json()}"
        )


SpecialistRequest = Union[G3BusinessRequest, G3DriftRequest]
TypedRequest = Union[G3BusinessRequest, G3DriftRequest, G3WorkflowRequest]
_REQUEST_MODELS: dict[str, type[BaseModel]] = {
    BUSINESS_REQUEST_VERSION: G3BusinessRequest,
    DRIFT_REQUEST_VERSION: G3DriftRequest,
    WORKFLOW_REQUEST_VERSION: G3WorkflowRequest,
}


def is_typed_wire_text(text: str) -> bool:
    """The deterministic protocol; anything else is a natural-language question."""

    return text.startswith((WIRE_ROUTE_PREFIX, WIRE_WORKFLOW_PREFIX))


def a2a_message_send(request: TypedRequest) -> dict[str, object]:
    """Build a reproducible A2A message/send payload from the typed request."""

    text = request.to_wire_text()
    return {
        "jsonrpc": "2.0",
        "id": request.request_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": request.request_id,
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


def _a2a_text(payload: object) -> tuple[str, dict, dict]:
    if not isinstance(payload, dict):
        raise ValueError("A2A payload must be an object")
    try:
        message = payload["params"]["message"]
        parts = message["parts"]
    except (KeyError, TypeError) as exc:
        raise ValueError("A2A payload has no message parts") from exc
    if (
        not isinstance(message, dict)
        or message.get("role") != "user"
        or not isinstance(parts, list)
        or len(parts) != 1
        or not isinstance(parts[0], dict)
        or parts[0].get("kind") != "text"
        or not isinstance(parts[0].get("text"), str)
    ):
        raise ValueError("A2A payload must contain exactly one user text part")
    return parts[0]["text"], payload, message


def typed_request_from_a2a(
    payload: object,
    *,
    accepted: tuple[type[BaseModel], ...] = (G3BusinessRequest, G3DriftRequest, G3WorkflowRequest),
) -> TypedRequest:
    """Fail closed unless an A2A payload carries one canonical typed request of an accepted kind."""

    text, envelope, message = _a2a_text(payload)
    lines = text.splitlines()
    if (
        len(lines) != 2
        or not lines[0].startswith((WIRE_ROUTE_PREFIX, WIRE_WORKFLOW_PREFIX))
        or not lines[1].startswith(WIRE_REQUEST_PREFIX)
    ):
        raise ValueError("A2A text part must use the canonical two-line route protocol")
    try:
        decoded = json.loads(lines[1][len(WIRE_REQUEST_PREFIX):])
    except json.JSONDecodeError as exc:
        raise ValueError("A2A text part must contain canonical JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("A2A request must be a JSON object")
    # The G3 contract predates the explicit version field on the wire default.
    version = decoded.get("contract_version", BUSINESS_REQUEST_VERSION)
    model = _REQUEST_MODELS.get(version) if isinstance(version, str) else None
    if model is None or model not in accepted:
        raise ValueError("A2A request contract is not accepted by this agent")
    request = model.model_validate(decoded)
    if text != request.to_wire_text():
        raise ValueError("A2A request is not the canonical wire representation")
    if envelope.get("id") != request.request_id or message.get("messageId") != request.request_id:
        raise ValueError("A2A identifiers must match the request_id")
    return request  # type: ignore[return-value]


def business_request_from_a2a(payload: object) -> G3BusinessRequest:
    """Fail closed unless an A2A payload carries exactly one typed fraud request."""

    request = typed_request_from_a2a(payload, accepted=(G3BusinessRequest,))
    assert isinstance(request, G3BusinessRequest)
    return request


def drift_request_from_a2a(payload: object) -> G3DriftRequest:
    """Fail closed unless an A2A payload carries exactly one typed drift request."""

    request = typed_request_from_a2a(payload, accepted=(G3DriftRequest,))
    assert isinstance(request, G3DriftRequest)
    return request
