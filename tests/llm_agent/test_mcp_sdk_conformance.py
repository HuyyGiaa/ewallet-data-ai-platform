"""Interoperability with the official MCP Python SDK (mcp==2.3.0, dev group).

The SDK's own ``ClientSession`` and Streamable HTTP client talk to the Phase D
app in-process through ``httpx2.ASGITransport``: no port, no network, no
backend. Run with the locked project environment:

    cd infra/docker/llm-agent-tooling && uv sync --frozen
    cd ../../.. && infra/docker/llm-agent-tooling/.venv/bin/python -m unittest tests.llm_agent.test_mcp_sdk_conformance

Credentials are the TEST FIXTURE tokens from test_phase_d_tools; passing here
says nothing about a deployed authentication mechanism.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest

from tests.llm_agent.test_phase_d_tools import (
    ACTOR,
    FASTAPI,
    MANIFEST,
    feature_rag,
    run,
    tool_app,
)

MCP_SDK = FASTAPI and importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("httpx2") is not None

if MCP_SDK:
    if sys.version_info < (3, 11):  # builtin from 3.11; the lock installs the backport only below it
        from exceptiongroup import BaseExceptionGroup
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import MCPError
    import mcp.types as mcp_types

    from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex
    from ai_platform.llm_agent.tools.knowledge_search import UnavailableKnowledgeRetriever
    from ai_platform.llm_agent.tools.security import HttpSecuritySettings

URL = "http://testserver/mcp"


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for sub in error.exceptions for leaf in leaves(sub)]
    return [error]


async def session_run(app, headers, action, *, statuses, rewrite_accept=None):
    """Open an SDK session against ``app``; every HTTP exchange is recorded in ``statuses``."""

    async def on_request(request):
        if rewrite_accept is not None:
            request.headers["accept"] = rewrite_accept

    async def on_response(response):
        statuses.append((response.request.method, response.status_code))

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
                                  headers=headers, timeout=10,
                                  event_hooks={"request": [on_request], "response": [on_response]}) as client:
        async with streamable_http_client(URL, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                return await asyncio.wait_for(action(session), 20)


@unittest.skipUnless(MCP_SDK, "official MCP SDK unavailable: uv sync --frozen in infra/docker/llm-agent-tooling")
class OfficialSdkInteropTests(unittest.TestCase):
    def test_initialize_discover_and_call(self):
        async def flow(session):
            init = await session.initialize()
            tools = await session.list_tools()
            call = await session.call_tool("knowledge_search", {"query": "account takeover", "top_k": 2})
            return init, tools, call

        statuses = []
        init, tools, call = run(session_run(tool_app(feature_rag()), ACTOR, flow, statuses=statuses))
        self.assertEqual(init.protocol_version, "2025-06-18")  # server keeps its version
        self.assertEqual(init.server_info.name, "feature-rag-mcp")
        self.assertIsNotNone(init.capabilities.tools)
        by_name = {tool.name: tool for tool in tools.tools}
        self.assertEqual(set(by_name), {"feature_lookup", "fraud_scoring", "knowledge_search"})
        self.assertEqual(by_name["knowledge_search"].input_schema["properties"]["top_k"]["maximum"], 5)
        self.assertFalse(call.is_error)
        self.assertEqual(json.loads(call.content[0].text), call.structured_content)
        self.assertTrue(call.structured_content["citations"])
        self.assertIn(("POST", 202), statuses)  # notifications/initialized acknowledged

    def test_tool_execution_failure_is_in_band(self):
        app = tool_app(feature_rag(retriever=UnavailableKnowledgeRetriever(TrustedChunkIndex.from_manifest(MANIFEST))))

        async def flow(session):
            await session.initialize()
            return await session.call_tool("knowledge_search", {"query": "account takeover"})

        call = run(session_run(app, ACTOR, flow, statuses=[]))
        self.assertTrue(call.is_error)
        self.assertEqual(call.structured_content["status"], "unavailable")
        self.assertEqual(call.structured_content["result"]["error"]["code"], "vector_search_unavailable")

    def test_rejections_surface_as_sdk_errors(self):
        small = HttpSecuritySettings(max_body_bytes=2048)

        async def initialize(session):
            return await session.initialize()

        async def call(session):
            await session.initialize()
            return await session.call_tool("knowledge_search", {"query": "takeover"})

        async def oversized(session):
            await session.initialize()
            return await session.call_tool("knowledge_search", {"query": "x" * 4096})

        cases = {
            "missing authentication": (tool_app(feature_rag()), {}, initialize, None, 401),
            "unknown credentials": (tool_app(feature_rag()), {"authorization": "Bearer guessed"}, initialize, None, 401),
            "forged actor header": (tool_app(feature_rag()), {**ACTOR, "x-ewallet-actor": "spiffe-test/fraud-coordinator"},
                                    call, None, 403),
            "invalid origin": (tool_app(feature_rag()), {**ACTOR, "origin": "https://evil.example"}, initialize, None, 403),
            "unacceptable media type": (tool_app(feature_rag()), ACTOR, initialize, "text/html", 406),
            "oversized request": (tool_app(feature_rag(), http_settings=small), ACTOR, oversized, None, 413),
        }
        for label, (app, headers, action, accept, status) in cases.items():
            with self.subTest(label):
                statuses = []
                with self.assertRaises(BaseException) as caught:
                    run(session_run(app, headers, action, statuses=statuses, rewrite_accept=accept))
                self.assertTrue(any(isinstance(leaf, MCPError) for leaf in leaves(caught.exception)),
                                [type(leaf).__name__ for leaf in leaves(caught.exception)])
                self.assertIn(("POST", status), statuses)

    def test_malformed_messages_return_sdk_valid_errors(self):
        app = tool_app(feature_rag())
        base = {**ACTOR, "content-type": "application/json", "accept": "application/json, text/event-stream"}
        cases = (
            (b"{not json", 400, -32700),
            (b'{"jsonrpc": "1.0", "method": "ping", "id": 1}', 400, -32600),
            (b'[{"jsonrpc": "2.0", "method": "ping", "id": 1}]', 400, -32600),
            (b'{"jsonrpc": "2.0", "method": "resources/list", "id": 2}', 200, -32601),
            (b'{"jsonrpc": "2.0", "method": "tools/call", "id": 3, "params": {"name": "shell"}}', 200, -32602),
        )

        async def post(body, headers):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver") as client:
                return await client.post("/mcp", content=body, headers=headers)

        for body, status, code in cases:
            with self.subTest(body=body):
                response = run(post(body, base))
                self.assertEqual(response.status_code, status)
                error = mcp_types.JSONRPCError.model_validate(response.json())
                self.assertEqual(error.error.code, code)
        response = run(post(b'{"jsonrpc": "2.0", "method": "ping", "id": 9}',
                            {**base, "mcp-protocol-version": "2099-01-01"}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(mcp_types.JSONRPCError.model_validate(response.json()).error.code, -32600)


if __name__ == "__main__":
    unittest.main()
