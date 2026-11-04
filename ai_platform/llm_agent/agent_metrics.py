"""Bounded OpenTelemetry counters for the BYO agents (rubric edai-2 rows 50/51).

Exported through the shared otel-collector Prometheus exporter (namespace
``ewallet``):

``ewallet_agent_invocations_total{agent, mode, outcome}``
    Each terminal A2A task of a BYO agent.
``ewallet_llm_prompts_screened_total{agent, result}``
    Each prompt checked by the PII screen before an LLM call.
``ewallet_llm_prompt_pii_detections_total{agent, pii_type}``
    Detected PII items, by type; the values themselves are never exported.

Every label value comes from a closed set, so cardinality is fixed (at most 2
agents x 2 modes x 4 outcomes, etc.). Without a configured MeterProvider the
OpenTelemetry API is a no-op.
"""

from __future__ import annotations

from typing import Literal

from opentelemetry import metrics

from ai_platform.llm_agent.prompt_safety import PII_TYPES, ScreenResult

Agent = Literal["fraud-coordinator", "feature-rag-specialist"]
Mode = Literal["typed", "natural-language"]
Outcome = Literal["completed", "rejected", "failed", "cancelled"]

AGENTS = frozenset({"fraud-coordinator", "feature-rag-specialist"})
MODES = frozenset({"typed", "natural-language"})
OUTCOMES = frozenset({"completed", "rejected", "failed", "cancelled"})

_meter = metrics.get_meter("ewallet.agent")
_invocations = _meter.create_counter(
    "agent_invocations_total", unit="1", description="Terminal A2A tasks per BYO agent",
)
_screened = _meter.create_counter(
    "llm_prompts_screened_total", unit="1", description="Prompts screened for PII before an LLM call",
)
_pii = _meter.create_counter(
    "llm_prompt_pii_detections_total", unit="1", description="PII items masked before an LLM call",
)


def _check(value: str, allowed: frozenset[str], name: str) -> None:
    if value not in allowed:
        raise ValueError(f"{name} {value!r} is not in the bounded label set")


def record_invocation(agent: str, mode: str, outcome: str) -> None:
    _check(agent, AGENTS, "agent")
    _check(mode, MODES, "mode")
    _check(outcome, OUTCOMES, "outcome")
    _invocations.add(1, {"agent": agent, "mode": mode, "outcome": outcome})


def initialize(agent: str) -> None:
    """Export every bounded series of this agent at 0 when the process starts.

    OTLP counters otherwise appear in Prometheus already holding their first
    increment, so increase()/rate() cannot see it. Starting from 0 lets
    restart-safe range queries count every event and handle the reset when a
    pod is replaced (N12 F2 preflight).
    """

    _check(agent, AGENTS, "agent")
    for mode in sorted(MODES):
        for outcome in sorted(OUTCOMES):
            _invocations.add(0, {"agent": agent, "mode": mode, "outcome": outcome})
    for result in ("clean", "pii_masked"):
        _screened.add(0, {"agent": agent, "result": result})
    for pii_type in PII_TYPES:
        _pii.add(0, {"agent": agent, "pii_type": pii_type})


def record_prompt_screen(agent: str, result: ScreenResult) -> None:
    _check(agent, AGENTS, "agent")
    _screened.add(1, {"agent": agent, "result": "pii_masked" if result.detected else "clean"})
    for pii_type in PII_TYPES:
        count = result.counts.get(pii_type, 0)
        if count:
            _pii.add(count, {"agent": agent, "pii_type": pii_type})
