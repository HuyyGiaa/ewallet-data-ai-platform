"""Bounded OpenTelemetry counters for the BYO agents (rubric edai-2 rows 50/51).

Exported through the shared otel-collector Prometheus exporter (namespace
``ewallet``):

``ewallet_agent_invocations_total{agent, mode, outcome}``
    Each terminal A2A task of a BYO agent.
``ewallet_llm_prompts_screened_total{agent, result}``
    Each prompt checked by the PII screen before an LLM call.
``ewallet_llm_prompt_pii_detections_total{agent, pii_type}``
    Detected PII items, by type; the values themselves are never exported.
``ewallet_agent_inflight_tasks{agent}``
    A2A tasks being executed. Agents mostly wait on the LLM and tools, so CPU hardly
    moves under load; this gauge is the signal KEDA scales on.
``ewallet_mcp_client_retries_total{agent}``
    Read-only MCP calls repeated after a transport failure.
``ewallet_agent_ab_requests_total{variant, outcome}``
    Coordinator LLM intakes per experiment variant; only an active experiment writes it.
``ewallet_agent_ab_llm_tokens_total{variant, kind}``
    Prompt and completion tokens per variant.
``ewallet_agent_ab_llm_latency_seconds{variant}``
    Time from the assigned planner call to a grounded request or a failure.

Every label value comes from a closed set, so cardinality is fixed (at most 2
agents x 2 modes x 4 outcomes, etc.). Without a configured MeterProvider the
OpenTelemetry API is a no-op.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Literal

from opentelemetry import metrics

from ai_platform.llm_agent.agent_ab import VARIANTS
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
_inflight = _meter.create_up_down_counter(
    "agent_inflight_tasks", unit="1", description="A2A tasks being executed per BYO agent",
)
_mcp_retries = _meter.create_counter(
    "mcp_client_retries_total", unit="1", description="Read-only MCP calls repeated after a transport failure",
)


_ab_requests = _meter.create_counter(
    "agent_ab_requests_total", unit="1", description="Coordinator LLM intakes per experiment variant",
)
_ab_tokens = _meter.create_counter(
    "agent_ab_llm_tokens_total", unit="1", description="LLM tokens per experiment variant",
)
_ab_latency = _meter.create_histogram(
    "agent_ab_llm_latency_seconds", unit="s", description="LLM intake latency per experiment variant",
)

AB_OUTCOMES = frozenset({"selected", "no_tool", "invalid_tool", "invalid_arguments", "llm_error"})
_AB_FAILURES = {
    "unsupported_request": "no_tool",
    "unauthorized_tool": "invalid_tool",
    "multiple_tool_calls": "invalid_tool",
    "llm_invalid_response": "invalid_tool",
    "invalid_tool_arguments": "invalid_arguments",
    "ungrounded_argument": "invalid_arguments",
    "ambiguous_identifier": "invalid_arguments",
    "missing_or_ambiguous_field": "invalid_arguments",
}


def ab_outcome(failure_code: str | None) -> str:
    """Map an intake failure code onto the bounded outcome set."""
    if failure_code is None:
        return "selected"
    return _AB_FAILURES.get(failure_code, "llm_error")


def record_agent_ab(
    variant: str,
    outcome: str,
    *,
    latency_seconds: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """Count one intake of an experiment variant."""
    _check(variant, frozenset(VARIANTS), "variant")
    _check(outcome, AB_OUTCOMES, "outcome")
    _ab_requests.add(1, {"variant": variant, "outcome": outcome})
    _ab_latency.record(latency_seconds, {"variant": variant})
    _ab_tokens.add(prompt_tokens, {"variant": variant, "kind": "prompt"})
    _ab_tokens.add(completion_tokens, {"variant": variant, "kind": "completion"})


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
    _inflight.add(0, {"agent": agent})
    _mcp_retries.add(0, {"agent": agent})


@contextmanager
def track_inflight(agent: str) -> Iterator[None]:
    """Count one A2A task as in flight for the duration of the block."""

    _check(agent, AGENTS, "agent")
    _inflight.add(1, {"agent": agent})
    try:
        yield
    finally:
        _inflight.add(-1, {"agent": agent})


def record_mcp_retry(agent: str) -> None:
    """Count one repeated MCP call."""

    _check(agent, AGENTS, "agent")
    _mcp_retries.add(1, {"agent": agent})


class InflightTrackingExecutor:
    """Wrap an A2A executor so every task shows up in the in-flight gauge."""

    def __init__(self, agent: str, delegate) -> None:
        _check(agent, AGENTS, "agent")
        self._agent = agent
        self._delegate = delegate

    async def execute(self, context, event_queue) -> None:
        """Run the wrapped task while counting it."""

        with track_inflight(self._agent):
            await self._delegate.execute(context, event_queue)

    async def cancel(self, context, event_queue) -> None:
        """Forward cancellation unchanged."""

        await self._delegate.cancel(context, event_queue)


def record_prompt_screen(agent: str, result: ScreenResult) -> None:
    _check(agent, AGENTS, "agent")
    _screened.add(1, {"agent": agent, "result": "pii_masked" if result.detected else "clean"})
    for pii_type in PII_TYPES:
        count = result.counts.get(pii_type, 0)
        if count:
            _pii.add(count, {"agent": agent, "pii_type": pii_type})
