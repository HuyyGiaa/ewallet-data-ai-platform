"""Shared MCP client pieces for the deterministic BYO Specialists.

Both BYO Specialists reach their MCP server only through the MCP Gateway, with
the official MCP Streamable HTTP client, a service token from a Kubernetes
Secret and W3C trace propagation. Each Specialist validates the shared
``ToolCallResponse`` envelope returned in MCP ``structuredContent``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
import os
from typing import Protocol
from urllib.parse import urlparse

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field

MCP_GATEWAY_HOST = "mcp-gateway.ewallet-agents.svc"
MCP_GATEWAY_PORT = 8080


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
    async with httpx2.AsyncClient(headers=dict(headers), timeout=timeout) as client:
        async with streamable_http_client(endpoint, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                yield session


def environment_token() -> str:
    return os.getenv("EWALLET_MCP_SERVICE_TOKEN", "").strip()


def validate_gateway_route(value: str, path: str) -> None:
    """Accept only the approved in-cluster MCP Gateway route for one server."""

    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != MCP_GATEWAY_HOST
        or parsed.port != MCP_GATEWAY_PORT
        or parsed.path != path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"MCP endpoint must be the approved Gateway route {path}")
