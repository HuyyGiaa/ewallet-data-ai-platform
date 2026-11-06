"""Agent configuration A/B at the point where a business agent calls its model.

The Coordinator's tool-selection call is the only LLM decision on the BYO path, so a
variant is applied there: each conversation is assigned once by ``context_id`` and
keeps one model configuration. Variants may differ only in model alias, temperature
and token budget; the system prompt, tool schema, grounding and authorization are
shared code, which keeps the comparison about the model configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Protocol

from ai_platform.inference.ab_assignment import AssignmentError, TrafficSplit, WeightedArm

BASELINE = "baseline"
CANDIDATE = "candidate"
VARIANTS = (BASELINE, CANDIDATE)


class AgentExperimentError(ValueError):
    """An agent experiment configuration is invalid."""


@dataclass(frozen=True)
class AgentVariant:
    """One model configuration of the Coordinator."""

    name: str
    weight: int
    model: str
    temperature: float
    max_tokens: int


@dataclass(frozen=True)
class AgentExperiment:
    """A validated two-variant experiment; the baseline comes first."""

    experiment_id: str
    salt: str
    variants: tuple[AgentVariant, ...]

    @classmethod
    def from_json(cls, raw: str) -> "AgentExperiment":
        """Parse and validate the ``EWALLET_AGENT_AB_CONFIG`` document."""
        try:
            data = json.loads(raw)
            variants = tuple(
                AgentVariant(
                    name=str(item["name"]),
                    weight=int(item["weight"]),
                    model=str(item["model"]),
                    temperature=float(item["temperature"]),
                    max_tokens=int(item["max_tokens"]),
                )
                for item in data["variants"]
            )
            experiment = cls(str(data["experiment_id"]), str(data["salt"]), variants)
            experiment.split()
        except (KeyError, TypeError, ValueError, AssignmentError) as exc:
            raise AgentExperimentError("agent experiment configuration is invalid") from exc
        if tuple(variant.name for variant in variants) != VARIANTS:
            raise AgentExperimentError("variants must be baseline then candidate")
        first, second = variants
        if (first.model, first.temperature, first.max_tokens) == (
            second.model, second.temperature, second.max_tokens,
        ):
            raise AgentExperimentError("variants must differ in model configuration")
        for variant in variants:
            if not 0 <= variant.temperature <= 2 or not 1 <= variant.max_tokens <= 2048:
                raise AgentExperimentError("variant sampling settings are out of range")
        return experiment

    def split(self) -> TrafficSplit:
        """Return the traffic split of this experiment."""
        return TrafficSplit(
            self.experiment_id,
            self.salt,
            tuple(WeightedArm(variant.name, variant.weight) for variant in self.variants),
        )


class AssignablePlanner(Protocol):
    """The planner surface the Coordinator needs."""

    def available(self) -> bool: ...


class ExperimentPlanner:
    """Hold one configured planner per variant and pick one per conversation."""

    def __init__(
        self, experiment: AgentExperiment, planners: Mapping[str, AssignablePlanner]
    ) -> None:
        if set(planners) != set(VARIANTS):
            raise AgentExperimentError("every variant needs exactly one planner")
        self._split = experiment.split()
        self._planners = dict(planners)

    def available(self) -> bool:
        """Report whether the planners can authenticate; they share one key."""
        return all(planner.available() for planner in self._planners.values())

    def for_context(self, context_id: str) -> AssignablePlanner:
        """Return the planner of a conversation; no context means the baseline."""
        if not context_id:
            return self._planners[BASELINE]
        return self._planners[self._split.assign(context_id).name]
