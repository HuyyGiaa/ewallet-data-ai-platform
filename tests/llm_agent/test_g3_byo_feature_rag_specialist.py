from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
from types import SimpleNamespace
import unittest

from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Message, MessageSendParams
from fastapi.testclient import TestClient

from ai_platform.llm_agent.business_request import a2a_message_send
from ai_platform.llm_agent.byo_feature_rag_specialist import (
    APPROVED_MCP_GATEWAY,
    FeatureRagFailure,
    FeatureRagMCPClient,
    FeatureRagSpecialistExecutor,
    build_app,
)
from tests.llm_agent.test_g3_programmatic_coordinator import FEATURE, request


VERSIONS = {
    "git_sha": "a" * 40,
    "image_digest": "sha256:" + "b" * 64,
    "config_revision": "g3-gke-v1",
    "prompt_revision": "v2",
    "model_revision": "fraud-detector/1",
    "feature_revision": "canonical-online-v1",
    "corpus_revision": "fraud-policy-v1",
}
CITATION = {
    "document_id": "policy-payment-anomaly",
    "chunk_id": "policy-payment-anomaly:0000",
    "source_uri": "repo://policy-payment-anomaly.md",
    "source_checksum": "sha256:" + "c" * 64,
    "corpus_revision": "fraud-policy-v1",
    "embedding_revision": "minilm-v1",
    "feature_revision": "fraud-policy-feast-v1",
    "score": 0.9,
}


def structured(tool: str, req=None):
    req = req or request()
    if tool == "feature_lookup":
        result = {
            "transaction_id": req.transaction.transaction_id,
            "historical_features": {
                FEATURE: {"value": 3, "event_timestamp": "2026-05-31T23:59:56+00:00"},
            },
            "request_time_features": req.transaction.request_time_features(),
            "feature_contract": {
                "historical_returned": 1,
                "historical": 31,
                "request_time": 4,
                "derived_in_scoring_only": 6,
            },
            "materialization_verified": False,
        }
        citations = []
    elif tool == "fraud_scoring":
        result = {
            "transaction_id": req.transaction.transaction_id,
            "fraud_probability": 0.81,
            "fraud_prediction": 1,
            "model_name": "fraud-detector",
            "model_version": "1",
            "feature_timestamp": "2026-05-31T23:59:56+00:00",
            "model_input_schema_sha256": "d" * 64,
        }
        citations = []
    else:
        result = {
            "query_sha256": hashlib.sha256(req.knowledge_query.encode()).hexdigest(),
            "results": [{
                "rank": 1,
                "chunk_id": CITATION["chunk_id"],
                "document_id": CITATION["document_id"],
                "score": CITATION["score"],
                "quote": "Validated policy quote.",
            }],
            "retrieval_backend": "feast-online-redis+exact-cosine",
            "trust_anchor": "source_corpus",
        }
        citations = [CITATION]
    return {
        "request_id": req.request_id,
        "tool_name": tool,
        "status": "ok",
        "result": result,
        "versions": VERSIONS,
        "citations": citations,
        "fallback_used": False,
    }


class FakeSession:
    def __init__(self, req=None, *, tools=None, overrides=None, is_error=None):
        self.req = req or request()
        self.tools = tools or ["feature_lookup", "fraud_scoring", "knowledge_search"]
        self.overrides = overrides or {}
        self.is_error = is_error or {}
        self.calls = []
        self.initialized = False
        self.request_id = self.req.request_id

    async def initialize(self):
        self.initialized = True
        return SimpleNamespace(protocol_version="2025-06-18")

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self.tools])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        value = structured(name, self.req)
        value["request_id"] = self.request_id
        value.update(self.overrides.get(name, {}))
        return SimpleNamespace(
            is_error=self.is_error.get(name, False),
            structured_content=value,
        )


def session_factory(session, capture):
    @asynccontextmanager
    async def factory(endpoint, headers, timeout):
        capture.update(endpoint=endpoint, headers=dict(headers), timeout=timeout)
        session.request_id = headers["X-Request-Id"]
        yield session

    return factory


class FeatureRagMCPClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_request_initializes_lists_and_calls_in_exact_order(self):
        req = request()
        session, capture = FakeSession(req), {}
        client = FeatureRagMCPClient(
            token_reader=lambda: "secret-token",
            session_factory=session_factory(session, capture),
        )

        evidence = await client.run(req)

        self.assertTrue(session.initialized)
        self.assertEqual([name for name, _ in session.calls], list(req.requested_tools))
        self.assertEqual(capture["endpoint"], APPROVED_MCP_GATEWAY)
        self.assertEqual(capture["headers"]["X-Ewallet-MCP-Service-Token"], "secret-token")
        self.assertEqual(capture["headers"]["X-Request-Id"], req.request_id)
        transaction = req.transaction.model_dump(mode="json", exclude_computed_fields=True)
        self.assertNotIn("merchant_present", transaction)
        self.assertEqual(session.calls[0][1], {
            "transaction": transaction,
            "feature_names": [FEATURE],
        })
        self.assertEqual(session.calls[1][1], {
            "transaction": transaction,
        })
        self.assertEqual(session.calls[2][1], {"query": req.knowledge_query, "top_k": 3})
        self.assertEqual(evidence.features[FEATURE]["value"], 3)
        self.assertEqual(evidence.fraud_score["fraud_probability"], 0.81)
        self.assertEqual(evidence.citations[0]["chunk_id"], CITATION["chunk_id"])

    async def test_missing_discovered_tool_fails_before_any_tool_call(self):
        session = FakeSession(tools=["feature_lookup", "fraud_scoring"])
        client = FeatureRagMCPClient(
            token_reader=lambda: "token",
            session_factory=session_factory(session, {}),
        )
        with self.assertRaises(FeatureRagFailure) as seen:
            await client.run(request())
        self.assertEqual(seen.exception.code, "mcp_tool_missing")
        self.assertEqual(session.calls, [])

    async def test_missing_credential_and_unapproved_endpoint_fail_closed(self):
        session = FakeSession()
        client = FeatureRagMCPClient(
            token_reader=lambda: "",
            session_factory=session_factory(session, {}),
        )
        with self.assertRaises(FeatureRagFailure) as seen:
            await client.run(request())
        self.assertEqual(seen.exception.code, "mcp_credential_unavailable")
        with self.assertRaises(ValueError):
            FeatureRagMCPClient(endpoint="http://feature-rag-mcp:8080/mcp")

    async def test_error_or_malformed_structured_result_is_rejected(self):
        cases = (
            FakeSession(is_error={"feature_lookup": True}),
            FakeSession(overrides={"feature_lookup": {"status": "error"}}),
            FakeSession(overrides={"feature_lookup": {"tool_name": "fraud_scoring"}}),
            FakeSession(overrides={"feature_lookup": {"request_id": "other-request"}}),
        )
        for session in cases:
            with self.subTest(session=session):
                client = FeatureRagMCPClient(
                    token_reader=lambda: "token",
                    session_factory=session_factory(session, {}),
                )
                with self.assertRaises(FeatureRagFailure):
                    await client.run(request())

    async def test_transaction_prediction_and_citation_mismatches_are_rejected(self):
        req = request()
        feature_bad = structured("feature_lookup", req)
        feature_bad["result"]["transaction_id"] = "other-transaction"
        score_bad = structured("fraud_scoring", req)
        score_bad["result"]["fraud_probability"] = 1.5
        rag_bad = structured("knowledge_search", req)
        rag_bad["citations"] = []
        cases = (
            ("feature_lookup", feature_bad),
            ("fraud_scoring", score_bad),
            ("knowledge_search", rag_bad),
        )
        for tool, value in cases:
            with self.subTest(tool=tool):
                session = FakeSession(req, overrides={tool: value})
                client = FeatureRagMCPClient(
                    token_reader=lambda: "token",
                    session_factory=session_factory(session, {}),
                )
                with self.assertRaises(FeatureRagFailure) as seen:
                    await client.run(req)
                self.assertEqual(seen.exception.code, "invalid_mcp_result")


class FeatureRagSpecialistA2ATests(unittest.TestCase):
    def app(self, *, session=None, store=None, timeout=5.0):
        req = request()
        session = session or FakeSession(req)
        client = FeatureRagMCPClient(
            token_reader=lambda: "token",
            session_factory=session_factory(session, {}),
        )
        executor = FeatureRagSpecialistExecutor(mcp_client=client, timeout_seconds=timeout)
        return build_app(task_store=store or InMemoryTaskStore(), executor=executor), session

    def test_actual_a2a_jsonrpc_returns_one_valid_terminal_artifact(self):
        app, session = self.app()
        req = request()
        payload = a2a_message_send(req)
        payload["params"]["message"]["contextId"] = "specialist-session-1"
        with TestClient(app) as client:
            response = client.post("/", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["result"]
        self.assertEqual(task["status"]["state"], "completed")
        evidence = json.loads(task["artifacts"][0]["parts"][0]["text"])
        self.assertEqual(evidence["request_id"], req.request_id)
        self.assertEqual(evidence["contract_version"], "g3-orchestration/v1")
        self.assertEqual([name for name, _ in session.calls], list(req.requested_tools))

    def test_invalid_request_is_rejected_without_mcp_call(self):
        app, session = self.app()
        payload = a2a_message_send(request())
        payload["params"]["message"]["parts"][0]["text"] = "pseudo tool text"
        with TestClient(app) as client:
            response = client.post("/", json=payload)
        self.assertEqual(response.json()["result"]["status"]["state"], "rejected")
        self.assertEqual(session.calls, [])

    def test_task_store_is_idempotent_per_context_but_sessions_are_isolated(self):
        app, session = self.app()
        first = a2a_message_send(request())
        first["params"]["message"]["contextId"] = "session-a"
        second = a2a_message_send(request(request_id="g3-byo-002"))
        second["params"]["message"]["contextId"] = "session-b"
        with TestClient(app) as client:
            one = client.post("/", json=first)
            duplicate = client.post("/", json=first)
            other = client.post("/", json=second)
        self.assertEqual(one.json()["result"]["status"]["state"], "completed")
        self.assertEqual(duplicate.json()["result"]["status"]["state"], "completed")
        self.assertEqual(other.json()["result"]["status"]["state"], "completed")
        self.assertEqual(len(session.calls), 6)


class FeatureRagSpecialistLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_and_cancel_are_terminal_and_bounded(self):
        class SlowClient:
            async def run(self, _request):
                await __import__("asyncio").sleep(10)

        executor = FeatureRagSpecialistExecutor(mcp_client=SlowClient(), timeout_seconds=0.01)
        req = request()
        payload = a2a_message_send(req)
        payload["params"]["message"].update(taskId="task-timeout", contextId="ctx-timeout")
        message = Message.model_validate(payload["params"]["message"])
        context = RequestContext(
            MessageSendParams(message=message),
            task_id="task-timeout",
            context_id="ctx-timeout",
        )
        events = EventQueue()
        await executor.execute(context, events)
        states = []
        while event := await events.dequeue_event(no_wait=True):
            if hasattr(event, "status"):
                states.append(event.status.state.value)
            if getattr(event, "final", False):
                break
        self.assertEqual(states[-1], "failed")

        cancel_events = EventQueue()
        await executor.cancel(context, cancel_events)
        canceled = await cancel_events.dequeue_event(no_wait=True)
        self.assertEqual(canceled.status.state.value, "canceled")


if __name__ == "__main__":
    unittest.main()
