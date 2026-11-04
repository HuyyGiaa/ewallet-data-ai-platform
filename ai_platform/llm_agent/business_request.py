"""Validated, bounded ingress contract for one G3 fraud investigation.

The live Coordinator remains a kagent declarative Agent and delegates with the
native Agent/A2A tool.  This module owns the deterministic input serialization
used at that boundary; it is not an alternative orchestration runtime.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ai_platform.inference.schemas import FraudPredictionRequest
from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS


BUSINESS_REQUEST_VERSION = "g3-business-request/v1"
MAX_BUSINESS_REQUEST_BYTES = 2_048
WIRE_ROUTE_PREFIX = "CALL_AGENT="
WIRE_REQUEST_PREFIX = "REQUEST_JSON="

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


def a2a_message_send(request: G3BusinessRequest) -> dict[str, object]:
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


def business_request_from_a2a(payload: object) -> G3BusinessRequest:
    """Fail closed unless an A2A payload carries exactly one typed text part."""

    if not isinstance(payload, dict):
        raise ValueError("A2A payload must be an object")
    try:
        message = payload["params"]["message"]
        parts = message["parts"]
    except (KeyError, TypeError) as exc:
        raise ValueError("A2A payload has no message parts") from exc
    if (
        message.get("role") != "user"
        or not isinstance(parts, list)
        or len(parts) != 1
        or parts[0].get("kind") != "text"
        or not isinstance(parts[0].get("text"), str)
    ):
        raise ValueError("A2A payload must contain exactly one user text part")
    text = parts[0]["text"]
    lines = text.splitlines()
    if len(lines) != 2 or not lines[0].startswith(WIRE_ROUTE_PREFIX) or not lines[1].startswith(WIRE_REQUEST_PREFIX):
        raise ValueError("A2A text part must use the canonical two-line route protocol")
    try:
        decoded = json.loads(lines[1][len(WIRE_REQUEST_PREFIX):])
    except json.JSONDecodeError as exc:
        raise ValueError("A2A text part must contain canonical JSON") from exc
    request = G3BusinessRequest.model_validate(decoded)
    if text != request.to_wire_text():
        raise ValueError("A2A business request is not the canonical wire representation")
    if payload.get("id") != request.request_id or message.get("messageId") != request.request_id:
        raise ValueError("A2A identifiers must match the business request_id")
    return request
