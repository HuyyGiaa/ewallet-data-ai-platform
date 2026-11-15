"""A real BYO Specialist rides out MCP Gateway 429s through the official MCP client (AGR-06).

On GKE the Specialists failed every task the MCP Gateway's rate limit shed. There were two causes:

- the MCP SDK (2.x) reports a non-2xx answer as a status-less JSON-RPC INTERNAL_ERROR;
- the SDK sends through httpx2, whose exceptions are not httpx's.

So ``mcp_failover`` never saw a 429, and never saw a 503 either.

Real here: the Drift Specialist's MCP client, ``byo_mcp.official_session`` (MCP SDK over
httpx2) and the production drift MCP app. Simulated: the MCP Gateway is an httpx2 transport
that maps ``/drift/mcp`` to the app's ``/mcp``, as the gateway route does, and answers the
first POSTs itself with the configured status, as ``localRateLimit`` does.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import httpx2

from ai_platform.llm_agent import byo_drift_specialist
from ai_platform.llm_agent.mcp_failover import FailoverPolicy, GatewayStatusError, is_recoverable
from ai_platform.llm_agent.tools.service import create_drift_app
from tests.llm_agent.agi_support import DRIFT_TOKEN, DriftFixture
from tests.llm_agent.test_agi_coordinator_workflows import drift_request

REAL_CLIENT = httpx2.AsyncClient


class Gateway(httpx2.AsyncBaseTransport):
    """MCP Gateway stand-in: sheds the first ``shed`` POSTs with ``status``, then forwards."""

    def __init__(self, app, *, shed: int, status: int = 429, retry_after: str = "0") -> None:
        self.inner = httpx2.ASGITransport(app=app)
        self.shed, self.status, self.retry_after = shed, status, retry_after
        self.posts = 0
        self.refused = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            self.posts += 1
            if self.refused < self.shed:
                self.refused += 1
                return httpx2.Response(self.status, headers={"Retry-After": self.retry_after}, request=request)
        if not request.url.path.endswith("/drift/mcp"):
            return httpx2.Response(404, request=request)
        forwarded = httpx2.Request(request.method, request.url.copy_with(path="/mcp"),
                                   headers=request.headers, stream=request.stream)
        return await self.inner.handle_async_request(forwarded)


class RealClientGatewayStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = DriftFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def run_drift(self, gateway: Gateway, policy: FailoverPolicy = FailoverPolicy()):
        """Run one typed drift request through the real client; the gateway is the only stand-in."""

        def client_factory(*args, **kwargs):
            return REAL_CLIENT(*args, transport=gateway, **kwargs)

        retries: list[int] = []
        client = byo_drift_specialist.DriftMCPClient(token_reader=lambda: DRIFT_TOKEN, failover=policy)
        with mock.patch.object(httpx2, "AsyncClient", client_factory), \
                mock.patch.object(byo_drift_specialist, "record_mcp_retry", lambda agent: retries.append(1)):
            try:
                return asyncio.run(client.run(drift_request("status-1"))), retries, None
            except Exception as error:  # the outcome under test
                return None, retries, error

    def gateway(self, **options) -> Gateway:
        return Gateway(create_drift_app(self.fixture.server_env()), **options)

    def test_a_healthy_gateway_completes(self):
        evidence, retries, error = self.run_drift(self.gateway(shed=0))
        self.assertIsNone(error)
        self.assertEqual((evidence.status, retries), ("ok", []))

    def test_shed_requests_are_repeated_until_the_gateway_admits_them(self):
        gateway = self.gateway(shed=3)
        evidence, retries, error = self.run_drift(gateway)
        self.assertIsNone(error, repr(error))
        self.assertEqual(evidence.status, "ok")
        self.assertEqual((gateway.refused, len(retries)), (3, 3))

    def test_a_shed_request_is_classified_with_its_status(self):
        _, _, error = self.run_drift(self.gateway(shed=99), FailoverPolicy(max_attempts=1))
        self.assertIsNotNone(error)
        statuses = []
        pending = [error]
        while pending:
            item = pending.pop()
            if isinstance(item, GatewayStatusError):
                statuses.append((item.status, item.retry_after))
            if item.__cause__ is not None:
                pending.append(item.__cause__)
        self.assertEqual(statuses, [(429, 0.0)])
        self.assertTrue(is_recoverable(error))

    def test_one_attempt_still_turns_retry_off(self):
        gateway = self.gateway(shed=1)
        _, retries, error = self.run_drift(gateway, FailoverPolicy(max_attempts=1))
        self.assertIsNotNone(error)
        self.assertEqual((gateway.refused, retries), (1, []))

    def test_the_rate_limited_budget_ends_a_persistent_shed(self):
        gateway = self.gateway(shed=99)
        _, retries, error = self.run_drift(gateway)
        self.assertIsNotNone(error)
        self.assertEqual(gateway.refused, FailoverPolicy().rate_limited_max_attempts)

    def test_a_lost_replica_is_now_seen_through_the_real_client(self):
        gateway = self.gateway(shed=1, status=503)
        evidence, retries, error = self.run_drift(gateway)
        self.assertIsNone(error, repr(error))
        self.assertEqual((evidence.status, len(retries)), ("ok", 1))

    def test_a_rejection_is_not_repeated(self):
        gateway = self.gateway(shed=99, status=401)
        _, retries, error = self.run_drift(gateway)
        self.assertIsNotNone(error)
        self.assertFalse(is_recoverable(error))
        self.assertEqual((gateway.refused, retries), (1, []))


if __name__ == "__main__":
    unittest.main()
