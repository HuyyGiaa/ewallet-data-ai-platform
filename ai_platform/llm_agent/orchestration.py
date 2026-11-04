"""Transport-neutral contracts for native kagent A2A orchestration.

The production runtime remains kagent 0.10.3.  This module does not implement
another agent framework; it defines and tests the deterministic boundary around
delegation and result aggregation used by the versioned agent prompts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ai_platform.llm_agent.contracts import (
    AgentDelegationRequest,
    Citation,
    ErrorResponse,
    RequestContext,
    TOOL_NAMES,
    VersionContext,
)


ORCHESTRATION_CONTRACT_VERSION = "g3-orchestration/v1"
FEATURE_RAG_SPECIALIST = "feature-rag-specialist"
DRIFT_SPECIALIST = "drift-specialist"

SPECIALIST_TOOLS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    FEATURE_RAG_SPECIALIST: ("feature_lookup", "fraud_scoring", "knowledge_search"),
    DRIFT_SPECIALIST: ("feature_lookup", "drift_detection"),
})

CAPABILITY_SPECIALIST: Mapping[str, str] = MappingProxyType({
    "fraud-evidence": FEATURE_RAG_SPECIALIST,
    "drift": DRIFT_SPECIALIST,
})


@dataclass(frozen=True)
class DelegationPlan:
    """Ordered native-A2A targets selected from explicit capabilities."""

    capabilities: tuple[str, ...]
    target_agents: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.capabilities or len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be non-empty and unique")
        unknown = set(self.capabilities) - set(CAPABILITY_SPECIALIST)
        if unknown:
            raise ValueError(f"unknown capabilities: {sorted(unknown)}")
        expected = tuple(dict.fromkeys(CAPABILITY_SPECIALIST[item] for item in self.capabilities))
        if self.target_agents != expected:
            raise ValueError("target_agents do not match capabilities")


@dataclass(frozen=True)
class SpecialistResult:
    """Structured response returned by one native kagent A2A delegation."""

    agent: str
    status: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    errors: tuple[ErrorResponse, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.agent not in SPECIALIST_TOOLS:
            raise ValueError("unknown specialist")
        if self.status not in {"ok", "partial", "unavailable"}:
            raise ValueError("unsupported specialist status")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        if self.status == "ok" and self.errors:
            raise ValueError("successful specialist result cannot contain errors")
        if self.status == "unavailable" and not self.errors:
            raise ValueError("unavailable specialist result requires an error")


class CoordinatorOrchestrator:
    """Plan bounded A2A calls and combine only validated specialist evidence."""

    def plan(self, capabilities: Iterable[str]) -> DelegationPlan:
        ordered = tuple(dict.fromkeys(capabilities))
        targets = tuple(dict.fromkeys(CAPABILITY_SPECIALIST[item] for item in ordered
                                      if item in CAPABILITY_SPECIALIST))
        return DelegationPlan(ordered, targets)

    def request(
        self,
        *,
        target_agent: str,
        task: str,
        context: RequestContext,
        versions: VersionContext,
    ) -> AgentDelegationRequest:
        tools = SPECIALIST_TOOLS.get(target_agent)
        if tools is None or not set(tools) <= TOOL_NAMES:
            raise ValueError("target agent has no approved tool grant")
        return AgentDelegationRequest(
            target_agent=target_agent,
            task=task,
            context=context,
            versions=versions,
            allowed_tools=tools,
        )

    def aggregate(
        self,
        plan: DelegationPlan,
        results: Iterable[SpecialistResult],
    ) -> dict[str, Any]:
        by_agent: dict[str, SpecialistResult] = {}
        for result in results:
            if result.agent not in plan.target_agents:
                raise ValueError("result came from a specialist that was not delegated")
            if result.agent in by_agent:
                raise ValueError("duplicate specialist result")
            by_agent[result.agent] = result

        unavailable: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        features: dict[str, Any] = {}
        fraud_score: Mapping[str, Any] | None = None
        drift: list[Any] = []
        statuses: list[str] = []

        for agent in plan.target_agents:
            result = by_agent.get(agent)
            if result is None:
                statuses.append("unavailable")
                unavailable.append({
                    "agent": agent,
                    "code": "specialist_unavailable",
                    "retryable": False,
                })
                continue
            statuses.append(result.status)
            if isinstance(result.payload.get("features"), Mapping):
                features[agent] = dict(result.payload["features"])
            if agent == FEATURE_RAG_SPECIALIST:
                supplied_score = result.payload.get("fraud_score")
                if isinstance(supplied_score, Mapping):
                    fraud_score = dict(supplied_score)
            if agent == DRIFT_SPECIALIST and isinstance(result.payload.get("drift"), list):
                drift.extend(result.payload["drift"])
            citations.extend(asdict(item) for item in result.citations)
            unavailable.extend({
                "agent": agent,
                "code": error.code,
                "retryable": error.retryable,
            } for error in result.errors)
            supplied_unavailable = result.payload.get("unavailable", [])
            if isinstance(supplied_unavailable, list):
                unavailable.extend({"agent": agent, "detail": item} for item in supplied_unavailable)

        overall = (
            "complete" if statuses and all(item == "ok" for item in statuses)
            else "unavailable" if statuses and all(item == "unavailable" for item in statuses)
            else "partial"
        )
        return {
            "contract_version": ORCHESTRATION_CONTRACT_VERSION,
            "status": overall,
            "features": features,
            "fraud_score": fraud_score,
            "drift": drift,
            "citations": citations,
            "unavailable": unavailable,
        }
