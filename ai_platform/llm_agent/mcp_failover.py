"""Bounded client-side failover for read-only MCP tool calls.

agentgateway 1.7 does not retry MCP backends: with a ``retry`` policy on an MCP route, a
backend answering 503 was called once and the 503 reached the caller. Failover therefore
lives in the client, which also knows what is safe to repeat.

A call may be repeated only when every tool in it is declared read-only here. Repeating
such a call can reach the server twice, which is harmless because it changes nothing. A
future tool with side effects stays outside the set and is never retried, whatever the
failure. Only transport failures are retried: a tool error, an invalid result and a spent
deadline are real answers, and the caller's single overall deadline still bounds the run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Collection, TypeVar


Result = TypeVar("Result")

READ_ONLY_TOOLS = frozenset(
    {"feature_lookup", "fraud_scoring", "knowledge_search", "chunk_lookup", "drift_detection"}
)


@dataclass(frozen=True)
class FailoverPolicy:
    """How often and how fast to repeat a failed read-only call.

    Attributes:
        max_attempts: Total attempts, including the first.
        base_delay_seconds: Wait before the second attempt; doubles after each attempt.
        max_delay_seconds: Longest single wait.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("retry delays are invalid")

    def delay(self, attempt: int) -> float:
        """Return the wait after failed attempt number ``attempt`` (one-based)."""

        return min(self.base_delay_seconds * 2 ** (attempt - 1), self.max_delay_seconds)


def may_repeat(tools: Collection[str]) -> bool:
    """Return whether a call using ``tools`` is safe to repeat."""

    return bool(tools) and set(tools) <= READ_ONLY_TOOLS


async def with_failover(
    call: Callable[[], Awaitable[Result]],
    *,
    tools: Collection[str],
    is_transient: Callable[[Exception], bool],
    policy: FailoverPolicy = FailoverPolicy(),
    on_retry: Callable[[int], None] = lambda attempt: None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Result:
    """Run ``call``, repeating it after transient failures when that is safe.

    Args:
        call: One complete attempt, opening its own session.
        tools: Tools the attempt may execute.
        is_transient: Whether a failure may heal on another replica.
        policy: Attempt budget and backoff.
        on_retry: Called with the failed attempt number before each retry.
        sleep: Wait function, replaced in tests.

    Returns:
        The first successful result.

    Raises:
        Exception: The last failure, unchanged, when it is not retryable or the
            budget is spent.
    """

    attempts = policy.max_attempts if may_repeat(tools) else 1
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as error:
            if attempt == attempts or not is_transient(error):
                raise
            on_retry(attempt)
            await sleep(policy.delay(attempt))
    raise AssertionError("the loop always returns or raises")
