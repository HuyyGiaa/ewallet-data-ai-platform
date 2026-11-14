"""Agent integration: in-process end-to-end workflows with real components.

Real: ``ProgrammaticCoordinator`` (routing, idempotency, validation), the BYO
Drift and Feature-RAG Specialist A2A servers (official a2a-sdk), the official
MCP Streamable HTTP client, the production ``create_drift_app`` (service-token
authentication, per-tool grants), ``ArtifactBaselineStore``/``CsvWindowSampler``
and the PSI implementation.

Simulated (labelled): the A2A and MCP Gateways are replaced by in-process
routing on the same identifiers they use (``x-kagent-host`` and the
``/drift/mcp`` route); the Feature-RAG MCP backend (Feast, KServe, RAG) is the
existing fake session from ``test_g3_byo_feature_rag_specialist``. Drift data is
the synthetic fixture. None of this is GKE evidence.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import unittest

from a2a.server.tasks import InMemoryTaskStore
from fastapi.testclient import TestClient
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ai_platform.llm_agent import byo_drift_specialist, byo_feature_rag_specialist
from ai_platform.llm_agent.business_request import G3WorkflowRequest, a2a_message_send, member_request_id
from ai_platform.llm_agent.idempotency import IdempotencyScope, InMemoryIdempotencyStore
from ai_platform.llm_agent.programmatic_coordinator import CoordinatorFailure, ProgrammaticCoordinator
from ai_platform.llm_agent.tools.service import create_drift_app
from tests.llm_agent.agi_support import DRIFT_TOKEN, FEATURE_RAG_TOKEN, DriftFixture
from tests.llm_agent.test_agi_coordinator_workflows import drift_request, fraud_request
from tests.llm_agent.test_g3_byo_feature_rag_specialist import FakeSession as FeatureRagSession

SCOPE = IdempotencyScope("ewallet-agents", "kagent-controller")


def mcp_route(app, *, route: str):
    """MCP Gateway stand-in: only ``route`` reaches the server's ``/mcp``, as the gateway route does."""

    @asynccontextmanager
    async def open_session(endpoint, headers, timeout):
        if not endpoint.endswith(route):
            raise ConnectionError("no MCP Gateway route")
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
                                      headers=dict(headers), timeout=timeout) as client:
            async with streamable_http_client("http://testserver/mcp", http_client=client) as (read, write):
                async with ClientSession(read, write) as session:
                    yield session
    return open_session


class A2AGatewaySim:
    """A2A Gateway stand-in: exact ``x-kagent-host`` routes only, like policy-gateways.yaml."""

    def __init__(self, routes):
        self.routes = routes
        self.hosts = []

    def __call__(self, call):
        host = call.headers.get("x-kagent-host")
        self.hosts.append(host)
        client = self.routes.get(host)
        if client is None:
            raise CoordinatorFailure("a2a_transport_error", "A2A Gateway request failed")
        response = client.post("/", json=call.payload)
        response.raise_for_status()
        return response.json()


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DriftFixture()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.__exit__(None, None, None)
        self.fixture.close()

    def client(self, app) -> TestClient:
        client = TestClient(app)
        client.__enter__()
        self.clients.append(client)
        return client

    def drift_agent(self, *, token=DRIFT_TOKEN, server_env=None):
        mcp_app = create_drift_app(server_env or self.fixture.server_env())
        client = byo_drift_specialist.DriftMCPClient(token_reader=lambda: token,
                                                     session_factory=mcp_route(mcp_app, route="/drift/mcp"))
        return self.client(byo_drift_specialist.build_app(
            task_store=InMemoryTaskStore(),
            executor=byo_drift_specialist.DriftSpecialistExecutor(mcp_client=client)))

    def feature_rag_agent(self, fraud):
        session = FeatureRagSession(fraud)

        @asynccontextmanager
        async def open_session(endpoint, headers, timeout):
            session.request_id = headers["X-Request-Id"]
            yield session

        client = byo_feature_rag_specialist.FeatureRagMCPClient(token_reader=lambda: FEATURE_RAG_TOKEN,
                                                                 session_factory=open_session)
        return self.client(byo_feature_rag_specialist.build_app(
            task_store=InMemoryTaskStore(),
            executor=byo_feature_rag_specialist.FeatureRagSpecialistExecutor(mcp_client=client, planner=None)))

    def coordinator(self, gateway):
        return ProgrammaticCoordinator(transport=gateway, synthesizer=lambda request, evidence: "Đã xác thực.",
                                       idempotency_store=InMemoryIdempotencyStore())

    def test_drift_request_flows_through_real_specialist_mcp_and_artifacts(self):
        request = drift_request("e2e-drift-1")
        gateway = A2AGatewaySim({"drift-specialist.ewallet-agents": self.drift_agent()})
        result = self.coordinator(gateway).handle(a2a_message_send(request), scope=SCOPE, session_id="s")
        finding = result["specialists"]["drift-specialist"]["drift"][0]
        self.assertEqual((result["status"], gateway.hosts), ("complete", ["drift-specialist.ewallet-agents"]))
        self.assertEqual((finding["drift_status"], finding["dataset_revisions"]["current"]),
                         ("drift", "agi-fixture-synthetic-v1"))
        self.assertEqual(finding["tool_versions"]["config_revision"], "agi-test")

    def test_combined_request_reaches_both_real_specialists(self):
        workflow = G3WorkflowRequest(
            request_id="e2e-wf-1",
            fraud=fraud_request(member_request_id("e2e-wf-1", "fraud")),
            drift=drift_request(member_request_id("e2e-wf-1", "drift")))
        gateway = A2AGatewaySim({
            "drift-specialist.ewallet-agents": self.drift_agent(),
            "feature-rag-specialist.ewallet-agents": self.feature_rag_agent(workflow.fraud),
        })
        result = self.coordinator(gateway).handle(a2a_message_send(workflow), scope=SCOPE, session_id="s")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(sorted(gateway.hosts), ["drift-specialist.ewallet-agents",
                                                 "feature-rag-specialist.ewallet-agents"])
        self.assertIn("fraud_score", result["specialists"]["feature-rag-specialist"])
        self.assertIn("drift", result["specialists"]["drift-specialist"])

    def test_unauthorized_mcp_principal_is_denied_by_the_real_server(self):
        gateway = A2AGatewaySim({"drift-specialist.ewallet-agents": self.drift_agent(token=FEATURE_RAG_TOKEN)})
        with self.assertRaises(CoordinatorFailure) as caught:
            self.coordinator(gateway).handle(a2a_message_send(drift_request("e2e-deny-1")), scope=SCOPE, session_id="s")
        # The Specialist sees an inventory without drift_detection and fails the task.
        self.assertEqual(caught.exception.code, "specialist_not_completed")

    def test_unrouted_specialist_is_unreachable(self):
        gateway = A2AGatewaySim({})
        with self.assertRaises(CoordinatorFailure) as caught:
            self.coordinator(gateway).handle(a2a_message_send(drift_request("e2e-noroute")), scope=SCOPE, session_id="s")
        self.assertEqual(caught.exception.code, "a2a_transport_error")

    def test_missing_source_data_is_reported_never_invented(self):
        broken = self.fixture.server_env(EWALLET_DRIFT_WINDOW_MANIFEST_SHA256="0" * 64)
        workflow = G3WorkflowRequest(
            request_id="e2e-wf-missing",
            fraud=fraud_request(member_request_id("e2e-wf-missing", "fraud")),
            drift=drift_request(member_request_id("e2e-wf-missing", "drift")))
        gateway = A2AGatewaySim({
            "drift-specialist.ewallet-agents": self.drift_agent(server_env=broken),
            "feature-rag-specialist.ewallet-agents": self.feature_rag_agent(workflow.fraud),
        })
        result = self.coordinator(gateway).handle(a2a_message_send(workflow), scope=SCOPE, session_id="s")
        drift = result["specialists"]["drift-specialist"]
        self.assertEqual((result["status"], drift["status"], drift["drift"]), ("partial", "unavailable", []))
        self.assertEqual(drift["unavailable"][0]["code"], "drift_window_checksum_mismatch")
        self.assertNotIn("psi", json.dumps(drift))


if __name__ == "__main__":
    unittest.main()
