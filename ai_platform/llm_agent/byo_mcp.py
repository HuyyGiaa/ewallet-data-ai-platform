"""Shared MCP client pieces for the deterministic BYO Specialists.

Both BYO Specialists reach their MCP server only through the MCP Gateway, with
the official MCP Streamable HTTP client, a service token from a Kubernetes
Secret and W3C trace propagation. Each Specialist validates the shared
``ToolCallResponse`` envelope returned in MCP ``structuredContent``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
import os
import re
from typing import Protocol

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from ai_platform.llm_agent.gateway_backoff import RATE_LIMITED, rate_limited_delay, retry_after_seconds
from ai_platform.llm_agent.mcp_failover import READ_ONLY_TOOLS, GatewayStatusError
from ai_platform.llm_agent.gateway_environment import (
    MCP_GATEWAY_PORT as _MCP_GATEWAY_PORT,
    PRODUCTION_GATEWAY,
    GatewayEnvironment,
    resolve_gateway_environment,
    validate_service_url,
)
from pydantic import BaseModel, ConfigDict, Field

# Production MCP Gateway (the default). Other approved environments: gateway_environment.py.
MCP_GATEWAY_HOST = PRODUCTION_GATEWAY.mcp_host
MCP_GATEWAY_PORT = _MCP_GATEWAY_PORT

# agentgateway's MCP rate limit answers inside the protocol as well as with HTTP 429 (observed on GKE,
# AGR-06): initialize/list get JSON-RPC error -32003 "rate limit exceeded" with data.retryAfterSeconds in
# an HTTP 200; tools/call gets a CallToolResult with isError and exactly this text. In both the gateway
# answered and no MCP server ran anything.
GATEWAY_RATE_LIMIT_CODE = -32003
_GATEWAY_SHED_TEXT = re.compile(r"rate limit exceeded \(retry after (\d+)s; limit \d+, remaining \d+\)")
# Repeats of one shed request inside its session; a repeat reaches only the gateway's token bucket.
SHED_ATTEMPTS = 6
SHED_BASE_DELAY_SECONDS = 0.25
SHED_MAX_DELAY_SECONDS = 2.0


class MCPEnvelope(BaseModel):
    """Exact shared tool response carried by MCP structuredContent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    tool_name: str
    status: str
    result: dict[str, object]
    versions: dict[str, str]
    citations: list[dict[str, object]] = Field(default_factory=list, max_length=8)
    fallback_used: bool = False


class MCPSession(Protocol):
    """The part of an MCP client session that the Specialists use."""

    async def initialize(self): ...
    async def list_tools(self): ...
    async def call_tool(self, name: str, arguments: dict[str, object]): ...


SessionFactory = Callable[[str, Mapping[str, str], float], object]
TokenReader = Callable[[], str]


@asynccontextmanager
async def official_session(
    endpoint: str,
    headers: Mapping[str, str],
    timeout: float,
) -> AsyncIterator[MCPSession]:
    # The SDK reports a non-2xx answer to a request as a status-less INTERNAL_ERROR; the first failed
    # POST's status is kept so that mcp_failover can tell a shed (429) or lost replica (5xx) from a
    # rejection. The listener GET and the closing DELETE never fail a call, so they are not recorded.
    failed: list[tuple[int, float | None]] = []

    async def record(response: httpx2.Response) -> None:
        if response.status_code >= 400 and response.request.method == "POST":
            failed.append((response.status_code, retry_after_seconds(response.headers)))

    async with httpx2.AsyncClient(headers=dict(headers), timeout=timeout, event_hooks={"response": [record]}) as client:
        try:
            async with streamable_http_client(endpoint, http_client=client) as (read, write):
                async with ClientSession(read, write) as session:
                    yield SheddingAwareSession(session, failed)
        except Exception as error:
            shed = _gateway_rate_limit_error(error)
            if shed is not None:
                raise GatewayStatusError(429, shed) from error
            if failed and _sdk_status_error(error):
                raise GatewayStatusError(*failed[0]) from error
            raise


class SheddingAwareSession:
    """An MCP session that repeats a request the MCP Gateway shed, without reopening the session.

    Only the shed request is sent again: the gateway answered it and no server ran it. Reopening the
    whole session for one refusal (initialize, notification, list, every tool, DELETE) spent about
    eight gateway tokens per retry and kept the bucket empty (GKE AGR-06). Only read-only tools are
    repeated. The caller's overall deadline still bounds every wait.
    """

    def __init__(self, session: ClientSession, failed: list[tuple[int, float | None]],
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._session, self._failed, self._sleep = session, failed, sleep
        self.shed_repeats = 0

    async def initialize(self):
        return await self._repeat(self._session.initialize)

    async def list_tools(self):
        return await self._repeat(self._session.list_tools)

    async def call_tool(self, name: str, arguments: dict[str, object]):
        if name not in READ_ONLY_TOOLS:
            return await self._session.call_tool(name, arguments)
        return await self._repeat(lambda: self._session.call_tool(name, arguments), tool=True)

    async def _repeat(self, send: Callable[[], Awaitable[object]], *, tool: bool = False):
        for attempt in range(1, SHED_ATTEMPTS + 1):
            before = len(self._failed)
            try:
                result = await send()
            except MCPError as error:
                retry_after = _request_shed(error, self._failed[before:])
                if retry_after is None or attempt == SHED_ATTEMPTS:
                    raise
            else:
                retry_after = _tool_shed(result) if tool else None
                if retry_after is None:
                    return result
                if attempt == SHED_ATTEMPTS:
                    raise GatewayStatusError(RATE_LIMITED, retry_after)
            self.shed_repeats += 1
            await self._sleep(rate_limited_delay(attempt, base_seconds=SHED_BASE_DELAY_SECONDS,
                                                 max_seconds=SHED_MAX_DELAY_SECONDS, retry_after=retry_after))
        raise AssertionError("the loop always returns or raises")


def _request_shed(error: MCPError, statuses: list[tuple[int, float | None]]) -> float | None:
    """Retry-After of a shed request: the in-protocol -32003, or an HTTP 429 seen for this request."""

    in_protocol = _gateway_rate_limit_error(error)
    if in_protocol is not None:
        return in_protocol
    for status, retry_after in statuses:
        if status == RATE_LIMITED:
            return retry_after if retry_after is not None else 0.0
    return None


def _tool_shed(result: object) -> float | None:
    try:
        gateway_shed(result)
    except GatewayStatusError as shed:
        return shed.retry_after
    return None


def gateway_shed(result: object) -> None:
    """Raise GatewayStatusError(429) when a tools/call result is the MCP Gateway's rate limit.

    Called before a Specialist treats ``isError`` as the tool's own failure: the gateway answered,
    the tool never ran, so the call may be repeated like any other shed request.
    """

    if not getattr(result, "is_error", False) or getattr(result, "structured_content", None) is not None:
        return
    texts = [getattr(item, "text", "") for item in getattr(result, "content", None) or []]
    if len(texts) == 1:
        match = _GATEWAY_SHED_TEXT.fullmatch(texts[0].strip())
        if match:
            raise GatewayStatusError(429, float(match.group(1)))


def _gateway_rate_limit_error(error: BaseException) -> float | None:
    """Return Retry-After seconds when ``error`` holds the gateway's JSON-RPC rate-limit error."""

    for item in _walk(error):
        if isinstance(item, MCPError) and item.error.code == GATEWAY_RATE_LIMIT_CODE \
                and item.error.message.startswith("rate limit exceeded"):
            data = item.error.data if isinstance(item.error.data, dict) else {}
            seconds = data.get("retryAfterSeconds", 0)
            return float(seconds) if isinstance(seconds, (int, float)) and 0 <= seconds <= 60 else 0.0
    return None


def _sdk_status_error(error: BaseException) -> bool:
    """Whether the SDK's stand-in for a non-2xx HTTP answer is anywhere in ``error``."""

    return any(isinstance(item, MCPError) for item in _walk(error))


def _walk(error: BaseException):
    """Yield ``error``, the members of exception groups and linked causes, once each."""

    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        for linked in (item.__cause__, item.__context__):
            if linked is not None:
                pending.append(linked)


def environment_token() -> str:
    return os.getenv("EWALLET_MCP_SERVICE_TOKEN", "").strip()


def validate_gateway_route(value: str, path: str, *, gateway: GatewayEnvironment | None = None) -> None:
    """Accept only the approved in-cluster MCP Gateway route for one server.

    ``gateway`` is the approved Gateway environment; None resolves it from the process environment
    (production unless EWALLET_GATEWAY_NAMESPACE selects staging), so staging never accepts the
    production Gateway and production never accepts the staging one.
    """

    environment = resolve_gateway_environment() if gateway is None else gateway
    try:
        validate_service_url(value, host=environment.mcp_host, port=MCP_GATEWAY_PORT, path=path, what="MCP endpoint")
    except ValueError as exc:
        raise ValueError(f"MCP endpoint must be the approved Gateway route {path}") from exc


def gateway_route(path: str, *, gateway: GatewayEnvironment | None = None) -> str:
    """The approved MCP Gateway route of one server in this process's Gateway environment."""

    environment = resolve_gateway_environment() if gateway is None else gateway
    return environment.mcp_route(path)
