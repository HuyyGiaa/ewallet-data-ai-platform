"""Bounded client-side failover for read-only MCP tool calls.

agentgateway 1.7 does not retry MCP backends: with a ``retry`` policy on an MCP route, a
backend answering 503 was called once and the 503 reached the caller. Failover therefore
lives in the client, which also knows what is safe to repeat.

A call may be repeated only when every tool in it is declared read-only here. Repeating
such a call can reach the server twice, which is harmless because it changes nothing. A
future tool with side effects stays outside the set and is never retried, whatever the
failure.

Only failures a later attempt could heal are retried: a lost connection, a 502, 503 or 504
from the gateway, or a 429 with which the gateway's rate limit shed the request before
forwarding it (gateway_backoff). Anything else is an answer, including a rejected credential, a
tool error, an invalid result and a spent deadline. An unrecognized exception is not retried, so
the default is the safe side. The caller's single overall deadline still bounds the run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Collection, Iterator, Mapping, TypeVar

import anyio
import httpx
import httpx2

from ai_platform.llm_agent.gateway_backoff import RATE_LIMITED, rate_limited_delay, retry_after_seconds


Result = TypeVar("Result")

READ_ONLY_TOOLS = frozenset(
    {"feature_lookup", "fraud_scoring", "knowledge_search", "chunk_lookup", "drift_detection"}
)


RECOVERABLE_STATUS = frozenset({RATE_LIMITED, 502, 503, 504})
# The official MCP client (mcp 2.x, byo_mcp) sends through httpx2, whose exceptions are not httpx's:
# matching httpx alone classified every real gateway 429/503 and lost connection as an answer (GKE AGR-06).
_STATUS_ERRORS = (httpx.HTTPStatusError, httpx2.HTTPStatusError)
_LOST_CONNECTION = (
    httpx.TransportError,
    httpx2.TransportError,
    ConnectionError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
)


class GatewayStatusError(Exception):
    """An MCP request that the MCP Gateway answered with an HTTP error status.

    The MCP SDK (2.x) reports every non-2xx answer to a request as a JSON-RPC INTERNAL_ERROR
    without the status, so a 429, a 503 and a 401 looked alike. ``byo_mcp`` records the status
    from the HTTP response and raises this from the SDK's error.
    """

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(f"MCP Gateway answered HTTP {status}")
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class FailoverPolicy:
    """How often and how fast to repeat a failed read-only call.

    Attributes:
        max_attempts: Total attempts, including the first.
        base_delay_seconds: Wait before the second attempt; doubles after each attempt.
        max_delay_seconds: Longest single wait after a 5xx or a lost connection.
        rate_limited_max_delay_seconds: Longest single wait after a 429; these waits are
            jittered and honour Retry-After.
        rate_limited_max_attempts: Total session attempts after a 429 that survived the
            session's own repeats of the shed request (byo_mcp.SheddingAwareSession), when retry
            is on at all (``max_attempts`` > 1).
    """

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 1.0
    rate_limited_max_delay_seconds: float = 2.0
    rate_limited_max_attempts: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("retry delays are invalid")
        if self.rate_limited_max_delay_seconds < self.base_delay_seconds:
            raise ValueError("retry delays are invalid")
        if not 1 <= self.rate_limited_max_attempts <= 8:
            raise ValueError("rate_limited_max_attempts must be between 1 and 8")

    def delay(self, attempt: int) -> float:
        """Return the wait after failed attempt number ``attempt`` (one-based)."""

        return min(self.base_delay_seconds * 2 ** (attempt - 1), self.max_delay_seconds)


def policy_from_env(env: Mapping[str, str]) -> FailoverPolicy:
    """Read the attempt budget from ``EWALLET_MCP_MAX_ATTEMPTS``.

    Setting it to 1 turns retry off without changing the image, so the failover experiment
    can compare both arms on one image.

    Raises:
        ValueError: The value is not an integer from 1 to 5.
    """

    value = env.get("EWALLET_MCP_MAX_ATTEMPTS")
    return FailoverPolicy() if value is None else FailoverPolicy(max_attempts=int(value))


def _causes(error: BaseException) -> Iterator[BaseException]:
    """Walk an exception, its causes and the members of exception groups."""

    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)


def is_recoverable(error: BaseException) -> bool:
    """Return whether a failure could heal on another replica.

    The MCP client wraps transport errors in task groups and in the caller's own error, so
    the whole chain is searched. A definite rejection anywhere in it wins: a 401 is not
    healed by asking again, even if a connection also closed while it was reported.
    """

    chain = list(_causes(error))
    statuses = [item.response.status_code for item in chain if isinstance(item, _STATUS_ERRORS)]
    statuses += [item.status for item in chain if isinstance(item, GatewayStatusError)]
    if any(status not in RECOVERABLE_STATUS for status in statuses):
        return False
    return bool(statuses) or any(isinstance(item, _LOST_CONNECTION) for item in chain)


def _rate_limited(error: BaseException) -> tuple[bool, float | None]:
    """Return whether the gateway shed this request with 429, and its Retry-After."""

    for item in _causes(error):
        if isinstance(item, GatewayStatusError) and item.status == RATE_LIMITED:
            return True, item.retry_after
        if isinstance(item, _STATUS_ERRORS) and item.response.status_code == RATE_LIMITED:
            return True, retry_after_seconds(item.response.headers)
    return False, None


def retry_delay(error: BaseException, attempt: int, policy: FailoverPolicy) -> float:
    """Return the wait before repeating after ``error``: jittered for a gateway 429."""

    shed, retry_after = _rate_limited(error)
    if shed:
        return rate_limited_delay(attempt, base_seconds=policy.base_delay_seconds,
                                  max_seconds=policy.rate_limited_max_delay_seconds, retry_after=retry_after)
    return policy.delay(attempt)


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
    attempt = 1
    while True:
        try:
            return await call()
        except Exception as error:
            budget = attempts
            if attempts > 1 and _rate_limited(error)[0]:
                budget = max(attempts, policy.rate_limited_max_attempts)
            if attempt >= budget or not is_transient(error):
                raise
            on_retry(attempt)
            await sleep(retry_delay(error, attempt, policy))
            attempt += 1
