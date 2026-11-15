"""Backoff after an Agent Gateway sheds a request with HTTP 429.

The A2A Gateway and the MCP Gateway (agentgateway ``localRateLimit``) protect the Specialists
and the MCP servers with a token bucket. When the bucket is empty the gateway answers 429
itself, before forwarding, so the upstream never saw the request and repeating it cannot
repeat any work. The rate limit is the protective control; callers wait and try again rather
than fail the business task on the first refusal.

Waits are jittered so that callers refused in the same instant do not come back in the same
instant, and a ``Retry-After`` given in seconds is honoured up to the policy's longest wait.
Every caller keeps its own overall deadline; this module only computes the waits.
On GKE (agi-20261115T145608Z, AGR-06) a burst of 100 typed tasks at concurrency 8 lost 95
tasks to 429s while the gateways themselves were healthy.

Standard library only: both the Coordinator and the Specialist images import it.
"""

from __future__ import annotations

import random
from typing import Callable, Mapping

RATE_LIMITED = 429
MAX_RETRY_AFTER_SECONDS = 60.0


def retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """Return the ``Retry-After`` delay in seconds, or None when absent or not delta-seconds.

    The HTTP-date form is ignored: the jittered backoff is used instead, which is never longer.
    """

    if not headers:
        return None
    value = None
    for key, item in headers.items():
        if key.lower() == "retry-after":
            value = item
            break
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    if seconds != seconds or seconds < 0:  # NaN or negative
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def rate_limited_delay(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    retry_after: float | None = None,
    rand: Callable[[], float] = random.random,
) -> float:
    """Return the wait after refused attempt number ``attempt`` (one-based).

    Exponential from ``base_seconds``, capped at ``max_seconds``, with equal jitter (between
    half and all of the step). A server ``Retry-After`` replaces the step, still capped.
    """

    if attempt < 1:
        raise ValueError("attempt is one-based")
    if base_seconds < 0 or max_seconds < base_seconds:
        raise ValueError("backoff delays are invalid")
    if retry_after is not None:
        return min(max(retry_after, 0.0), max_seconds)
    step = min(base_seconds * 2 ** (attempt - 1), max_seconds)
    return step * (0.5 + 0.5 * rand())
