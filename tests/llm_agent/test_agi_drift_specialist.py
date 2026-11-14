"""Agent integration: deterministic BYO Drift Specialist (A2A server + MCP client).

The MCP session is a fake that returns real drift results computed by the
drift tool over the synthetic fixture; the A2A server, request parsing,
validation and evidence conversion are real.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest

from a2a.server.tasks import InMemoryTaskStore
from fastapi.testclient import TestClient

from ai_platform.llm_agent.business_request import G3DriftRequest, a2a_message_send
from ai_platform.llm_agent.byo_drift_specialist import (
    APPROVED_MCP_GATEWAY,
    DriftMCPClient,
    DriftSpecialistExecutor,
    DriftSpecialistFailure,
    build_app,
    evidence_from_envelope,
)
from ai_platform.llm_agent.contracts import RequestContext
from ai_platform.llm_agent.tools.drift import DriftDetectionInput, DriftDetectionTool
from ai_platform.llm_agent.tools.drift_sources import drift_backends_from_env
from tests.llm_agent.agi_support import ACCOUNT_BASELINE, ACCOUNT_FEATURE, CURRENT_WINDOW, VERSIONS, DriftFixture
from tests.llm_agent.test_agi_contracts import fraud

VERSION_CONTEXT = {
    "git_sha": VERSIONS["EWALLET_GIT_SHA"], "image_digest": VERSIONS["EWALLET_IMAGE_DIGEST"],
    "config_revision": "agi-test", "prompt_revision": "no-prompt", "model_revision": "fraud-detector/1",
    "feature_revision": "agi-fixture-synthetic-v1", "corpus_revision": "fraud-policy-v1-20261030",
}


def request(request_id: str = "drift-spec-1") -> G3DriftRequest:
    return G3DriftRequest(request_id=request_id, feature_name=ACCOUNT_FEATURE, baseline_id=ACCOUNT_BASELINE,
                          current_window=CURRENT_WINDOW)


class FakeSession:
    def __init__(self, envelope: dict, tools=("drift_detection",)):
        self.envelope = envelope
        self.tools = tools
        self.calls = []

    async def initialize(self):
        return SimpleNamespace(protocol_version="2025-06-18")

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self.tools])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=self.envelope["status"] != "ok", structured_content=self.envelope)


def factory(session, capture):
    @asynccontextmanager
    async def open_session(endpoint, headers, timeout):
        capture.update(endpoint=endpoint, headers=dict(headers))
        yield session
    return open_session


class DriftSpecialistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = DriftFixture()
        tool = DriftDetectionTool(*drift_backends_from_env(cls.fixture.drift_env))
        context = RequestContext(request_id="x", traceparent="00-" + "5" * 32 + "-" + "6" * 16 + "-01",
                                 actor_id="drift-specialist", timeout_ms=5_000, tool_call_budget=1)
        cls.result = dict(asyncio.run(tool.run(DriftDetectionInput(**request().tool_arguments()), context)).result)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def envelope(self, request_id="drift-spec-1", **changes) -> dict:
        value = {"request_id": request_id, "tool_name": "drift_detection", "status": "ok",
                 "result": self.result, "versions": VERSION_CONTEXT, "citations": [], "fallback_used": False}
        value.update(changes)
        return value

    def test_valid_result_becomes_one_drift_finding_with_tool_provenance(self):
        evidence = evidence_from_envelope(self.envelope(), request())
        self.assertEqual((evidence.status, len(evidence.drift)), ("ok", 1))
        self.assertEqual(evidence.drift[0]["tool_versions"]["git_sha"], "c" * 40)
        self.assertEqual(evidence.prompt_revision, "drift-specialist/byo-v1")

    def test_tool_rejection_is_structured_unavailability(self):
        envelope = self.envelope(status="rejected", result={"error": {
            "code": "unknown_baseline", "message": "No baseline with this id exists for the feature", "retryable": False}})
        evidence = evidence_from_envelope(envelope, request())
        self.assertEqual((evidence.status, evidence.drift), ("unavailable", []))
        self.assertEqual(evidence.unavailable[0]["code"], "unknown_baseline")

    def test_mismatched_or_malformed_results_fail(self):
        cases = {
            "other request": self.envelope(request_id="other"),
            "other tool": self.envelope(tool_name="feature_lookup"),
            "fallback": self.envelope(fallback_used=True),
            "other baseline": self.envelope(result=dict(self.result, baseline_id="bl-other")),
            "extra field": self.envelope(result=dict(self.result, injected="x")),
            "bad versions": self.envelope(versions={"git_sha": "x"}),
            "error without code": self.envelope(status="unavailable", result={}),
        }
        for label, envelope in cases.items():
            with self.subTest(label), self.assertRaises(DriftSpecialistFailure):
                evidence_from_envelope(envelope, request())

    def test_client_uses_only_the_drift_gateway_route_and_its_token(self):
        capture: dict = {}
        session = FakeSession(self.envelope())
        client = DriftMCPClient(token_reader=lambda: "t" * 40, session_factory=factory(session, capture))
        asyncio.run(client.run(request()))
        self.assertEqual(capture["endpoint"], APPROVED_MCP_GATEWAY)
        self.assertEqual(capture["headers"]["X-Request-Id"], "drift-spec-1")
        self.assertEqual(session.calls, [("drift_detection", request().tool_arguments())])
        for endpoint in ("http://drift-mcp.ewallet-mcp.svc.cluster.local:8080/mcp",
                         "http://mcp-gateway.ewallet-agents.svc:8080/feature-rag/mcp"):
            with self.subTest(endpoint), self.assertRaises(ValueError):
                DriftMCPClient(endpoint=endpoint)

    def test_missing_token_or_unexpected_inventory_fail_before_any_call(self):
        session = FakeSession(self.envelope(), tools=("drift_detection", "feature_lookup"))
        with self.assertRaises(DriftSpecialistFailure) as missing:
            asyncio.run(DriftMCPClient(token_reader=lambda: "", session_factory=factory(session, {})).run(request()))
        self.assertEqual(missing.exception.code, "mcp_credential_unavailable")
        with self.assertRaises(DriftSpecialistFailure) as inventory:
            asyncio.run(DriftMCPClient(token_reader=lambda: "t" * 40, session_factory=factory(session, {})).run(request()))
        self.assertEqual(inventory.exception.code, "mcp_tool_missing")
        self.assertEqual(session.calls, [])

    def app(self, session):
        client = DriftMCPClient(token_reader=lambda: "t" * 40, session_factory=factory(session, {}))
        return build_app(task_store=InMemoryTaskStore(), executor=DriftSpecialistExecutor(mcp_client=client))

    def test_a2a_request_returns_one_validated_terminal_artifact(self):
        session = FakeSession(self.envelope())
        with TestClient(self.app(session)) as client:
            task = client.post("/", json=a2a_message_send(request())).json()["result"]
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(len(task["artifacts"]), 1)
        evidence = json.loads(task["artifacts"][0]["parts"][0]["text"])
        self.assertEqual((evidence["request_id"], evidence["drift"][0]["psi"]), ("drift-spec-1", self.result["psi"]))

    def test_fraud_and_natural_language_requests_are_rejected_without_tool_calls(self):
        session = FakeSession(self.envelope())
        natural = a2a_message_send(request())
        natural["params"]["message"]["parts"][0]["text"] = "Phân tích drift giúp tôi"
        with TestClient(self.app(session)) as client:
            for payload in (a2a_message_send(fraud()), natural):
                with self.subTest(payload["params"]["message"]["parts"][0]["text"][:20]):
                    state = client.post("/", json=payload).json()["result"]["status"]["state"]
                    self.assertEqual(state, "rejected")
        self.assertEqual(session.calls, [])

    def test_agent_card_keeps_the_registered_skill_id(self):
        with TestClient(self.app(FakeSession(self.envelope()))) as client:
            card = client.get("/.well-known/agent-card.json").json()
        self.assertEqual(card["name"], "drift_specialist")
        self.assertEqual([skill["id"] for skill in card["skills"]], ["feature-risk-analysis"])


if __name__ == "__main__":
    unittest.main()
