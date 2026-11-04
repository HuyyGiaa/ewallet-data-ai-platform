"""N11 P0: kagent Chat compatibility (A2A message/stream) for the BYO agents.

The kagent 0.10.3 UI always sends JSON-RPC ``message/stream``.  These tests
drive the real A2A FastAPI application over SSE and assert that every request
ends in exactly one terminal state (completed, rejected or failed), so the UI
can never stay on "Thinking".
"""

from __future__ import annotations

import json
import unittest
from uuid import uuid4

from a2a.server.tasks import InMemoryTaskStore
from fastapi.testclient import TestClient

from ai_platform.llm_agent.business_request import G3BusinessRequest, a2a_message_send, business_request_from_a2a
from ai_platform.llm_agent.byo_coordinator import CoordinatorExecutor, build_app
from ai_platform.llm_agent.byo_feature_rag_specialist import (
    FeatureRagSpecialistExecutor,
    build_app as build_specialist_app,
)
from ai_platform.llm_agent.idempotency import IdempotencyScope, InMemoryIdempotencyStore
from ai_platform.llm_agent.programmatic_coordinator import (
    ProgrammaticCoordinator,
    SpecialistEvidence,
    encode_specialist_task,
)
from tests.llm_agent.test_g3_programmatic_coordinator import evidence, request
from tests.llm_agent.test_n11_nl_intake import PROMPT, RecordingLLM, completion


SCOPE = IdempotencyScope("ewallet-agents", "kagent-controller")
TERMINAL = {"completed", "rejected", "failed", "canceled"}


class EchoSpecialist:
    """Answers each delegated request with evidence for exactly that request."""

    def __init__(self):
        self.calls = []

    def __call__(self, call):
        self.calls.append(call)
        req = business_request_from_a2a(dict(call.payload))
        value = evidence(
            req,
            features={name: {"value": 3, "event_timestamp": "2026-05-31T23:59:56+00:00"} for name in req.feature_names},
            fraud_score={"transaction_id": req.transaction.transaction_id, "fraud_probability": 0.0639895560144887,
                         "fraud_prediction": 0, "model_name": "fraud-detector", "model_version": "1",
                         "feature_timestamp": "2026-05-31T23:59:56+00:00",
                         "model_input_schema_sha256": "0" * 64},
        )
        if "knowledge_search" not in req.requested_tools:
            value["citations"] = []
        return encode_specialist_task(req.request_id, value)


def coordinator_app(*, planner=None, specialist=None):
    specialist = specialist or EchoSpecialist()
    store = InMemoryIdempotencyStore()
    executor = CoordinatorExecutor(
        lambda: ProgrammaticCoordinator(
            transport=specialist,
            synthesizer=lambda _r, _e: "Bằng chứng đã được xác thực.",
            idempotency_store=store,
        ),
        timeout_seconds=5.0,
        idempotency_scope=SCOPE,
        planner=planner,
    )
    return build_app(task_store=InMemoryTaskStore(), executor=executor), specialist


def stream(client: TestClient, text: str, *, message_id: str | None = None) -> list[dict]:
    message_id = message_id or str(uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "message/stream",
        "params": {"message": {"role": "user", "messageId": message_id,
                               "parts": [{"kind": "text", "text": text}]}},
    }
    events = []
    with client.stream("POST", "/", json=payload) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return events


def terminal_states(events: list[dict]) -> list[str]:
    return [event["result"]["status"]["state"] for event in events
            if event.get("result", {}).get("kind") == "status-update" and event["result"].get("final")]


def artifacts(events: list[dict]) -> dict[str, str]:
    return {event["result"]["artifact"]["name"]: event["result"]["artifact"]["parts"][0]["text"]
            for event in events if event.get("result", {}).get("kind") == "artifact-update"}


def final_message(events: list[dict]) -> str:
    for event in reversed(events):
        status = event.get("result", {}).get("status") or {}
        if event.get("result", {}).get("final") and status.get("message"):
            return status["message"]["parts"][0]["text"]
    return ""


class CoordinatorStreamingTests(unittest.TestCase):
    def test_agent_card_advertises_streaming(self):
        app, _ = coordinator_app()
        with TestClient(app) as client:
            self.assertTrue(client.get("/.well-known/agent-card.json").json()["capabilities"]["streaming"])

    def test_typed_protocol_over_stream_completes_with_the_unchanged_artifact(self):
        app, specialist = coordinator_app()
        req = request()
        with TestClient(app) as client:
            events = stream(client, req.to_wire_text(), message_id=req.request_id)
        self.assertEqual(terminal_states(events), ["completed"])
        self.assertEqual(set(artifacts(events)), {"validated-fraud-investigation"})
        result = json.loads(artifacts(events)["validated-fraud-investigation"])
        self.assertEqual(result["request_id"], req.request_id)
        self.assertEqual(len(specialist.calls), 1)

    def test_typed_message_send_is_unchanged(self):
        app, _ = coordinator_app()
        req = request()
        with TestClient(app) as client:
            response = client.post("/", json=a2a_message_send(req))
        task = response.json()["result"]
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(len(task["artifacts"]), 1)

    def test_natural_language_question_runs_llm_tool_call_then_deterministic_delegation(self):
        llm = RecordingLLM()
        app, specialist = coordinator_app(planner=llm.planner())
        with TestClient(app) as client:
            events = stream(client, PROMPT)
        self.assertEqual(terminal_states(events), ["completed"])
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(len(specialist.calls), 1)
        delegated = business_request_from_a2a(dict(specialist.calls[0].payload))
        self.assertEqual(delegated.transaction.transaction_id, "dc58ff4e-b5f7-4486-b1f8-4c31383741a9")
        self.assertIn("knowledge_search", delegated.requested_tools)
        produced = artifacts(events)
        self.assertEqual(set(produced), {"analyst-answer", "validated-fraud-investigation"})
        result = json.loads(produced["validated-fraud-investigation"])
        self.assertEqual(result["request_id"], delegated.request_id)
        self.assertIn("fraud_probability = 0.0639895560144887", produced["analyst-answer"])
        self.assertEqual(result["fraud_score"]["fraud_probability"], 0.0639895560144887)
        self.assertIn("investigate_transaction", produced["analyst-answer"])
        working = [event["result"]["status"]["message"]["parts"][0]["text"] for event in events
                   if event.get("result", {}).get("kind") == "status-update"
                   and event["result"]["status"]["state"] == "working" and event["result"]["status"].get("message")]
        self.assertTrue(any("LLM đã chọn công cụ" in text for text in working))

    def test_natural_language_rejections_terminate_the_stream(self):
        cases = {
            "no identifiers": (RecordingLLM(), "Thời tiết hôm nay thế nào?", "missing_transaction_identifiers"),
            "no tool selected": (RecordingLLM(completion(calls=0)), PROMPT, "unsupported_request"),
            "unauthorized tool": (RecordingLLM(completion(name="drop_tables")), PROMPT, "unauthorized_tool"),
            "missing field": (RecordingLLM(), PROMPT.replace(", event_timestamp 2026-06-01T00:00:00Z", ""),
                              "missing_or_ambiguous_field"),
        }
        for label, (llm, text, code) in cases.items():
            with self.subTest(label):
                app, specialist = coordinator_app(planner=llm.planner())
                with TestClient(app) as client:
                    events = stream(client, text)
                self.assertEqual(terminal_states(events), ["rejected"])
                self.assertIn(code, final_message(events))
                self.assertEqual(specialist.calls, [], "no delegation and no prediction on rejection")
                self.assertEqual(artifacts(events), {})

    def test_free_text_llm_answer_is_never_shown_to_the_analyst(self):
        # Gate A on GKE: with a verbose prompt Qwen3-0.6B answered "Mức rủi ro cao"
        # in free text without calling the tool; that text must not reach the UI.
        invented = completion(calls=0)
        invented["choices"][0]["message"]["content"] = "Mức rủi ro cao theo chính sách gian lận."
        app, specialist = coordinator_app(planner=RecordingLLM(invented).planner())
        with TestClient(app) as client:
            events = stream(client, PROMPT)
        self.assertEqual(terminal_states(events), ["rejected"])
        self.assertNotIn("Mức rủi ro cao", json.dumps(events, ensure_ascii=False))
        self.assertEqual(specialist.calls, [])

    def test_llm_outage_is_a_failure_not_a_hang_or_a_fabricated_score(self):
        app, specialist = coordinator_app(planner=RecordingLLM(status=503, body={"e": 1}).planner())
        with TestClient(app) as client:
            events = stream(client, PROMPT)
        self.assertEqual(terminal_states(events), ["failed"])
        self.assertIn("llm_unavailable", final_message(events))
        self.assertEqual(specialist.calls, [])

    def test_natural_language_disabled_rejects_explicitly(self):
        app, specialist = coordinator_app(planner=None)
        with TestClient(app) as client:
            events = stream(client, PROMPT)
        self.assertEqual(terminal_states(events), ["rejected"])
        self.assertIn("nl_intake_disabled", final_message(events))
        self.assertEqual(specialist.calls, [])

    def test_specialist_failure_after_tool_selection_fails_closed(self):
        def failing(call):
            raise RuntimeError("specialist down")
        app, _ = coordinator_app(planner=RecordingLLM().planner(), specialist=failing)
        with TestClient(app) as client:
            events = stream(client, PROMPT)
        self.assertEqual(terminal_states(events), ["failed"])
        self.assertNotIn("analyst-answer", artifacts(events))


class FakeMCPClient:
    def __init__(self):
        self.requests: list[G3BusinessRequest] = []

    async def run(self, req: G3BusinessRequest) -> SpecialistEvidence:
        self.requests.append(req)
        return SpecialistEvidence.model_validate(evidence(req))


class SpecialistStreamingTests(unittest.TestCase):
    def app(self, planner=None):
        client = FakeMCPClient()
        executor = FeatureRagSpecialistExecutor(mcp_client=client, timeout_seconds=5.0, planner=planner)
        return build_specialist_app(task_store=InMemoryTaskStore(), executor=executor), client

    def test_natural_language_question_runs_tool_call_and_real_mcp_client_path(self):
        llm = RecordingLLM()
        app, mcp = self.app(planner=llm.planner())
        with TestClient(app) as client:
            events = stream(client, PROMPT)
        self.assertEqual(terminal_states(events), ["completed"])
        self.assertEqual(len(llm.requests), 1)
        [req] = mcp.requests
        self.assertEqual(req.transaction.transaction_id, "dc58ff4e-b5f7-4486-b1f8-4c31383741a9")
        self.assertEqual(req.requested_tools, ("feature_lookup", "fraud_scoring", "knowledge_search"))
        self.assertEqual(set(artifacts(events)), {"analyst-answer", "validated-feature-rag-evidence"})
        self.assertIn("policy-1", artifacts(events)["analyst-answer"])

    def test_policy_only_question_is_rejected_with_guidance_not_answered_by_the_llm(self):
        llm = RecordingLLM()
        app, mcp = self.app(planner=llm.planner())
        with TestClient(app) as client:
            events = stream(client, "Theo chính sách gian lận nội bộ, khi nào cần step-up authentication?")
        self.assertEqual(terminal_states(events), ["rejected"])
        self.assertIn("missing_transaction_identifiers", final_message(events))
        self.assertEqual((llm.requests, mcp.requests), ([], []))

    def test_cancel_during_llm_intake_stops_before_any_mcp_call(self):
        import asyncio
        from a2a.server.agent_execution import RequestContext
        from a2a.server.events import EventQueue
        from a2a.types import Message, MessageSendParams, Part, Role, TextPart

        class SlowPlanner:
            _model = "ewallet-fraud-llm"
            async def select(self, text):
                await asyncio.sleep(30)

        async def scenario():
            mcp = FakeMCPClient()
            executor = FeatureRagSpecialistExecutor(mcp_client=mcp, timeout_seconds=5.0, planner=SlowPlanner())
            message = Message(role=Role.user, message_id="m-1", parts=[Part(root=TextPart(text=PROMPT))])
            context = RequestContext(request=MessageSendParams(message=message), task_id="t-1", context_id="c-1")
            queue = EventQueue()
            running = asyncio.create_task(executor.execute(context, queue))
            await asyncio.sleep(0.2)
            await executor.cancel(context, queue)
            await asyncio.wait_for(running, timeout=5)
            return mcp

        mcp = asyncio.run(scenario())
        self.assertEqual(mcp.requests, [])

    def test_card_streaming_and_typed_request_completes_over_stream(self):
        app, mcp = self.app()
        req = request()
        with TestClient(app) as client:
            self.assertTrue(client.get("/.well-known/agent-card.json").json()["capabilities"]["streaming"])
            events = stream(client, req.to_wire_text(), message_id=req.request_id)
        self.assertEqual(terminal_states(events), ["completed"])
        self.assertEqual(set(artifacts(events)), {"validated-feature-rag-evidence"})
        self.assertEqual(len(mcp.requests), 1)

    def test_free_text_is_rejected_explicitly_without_tool_calls(self):
        app, mcp = self.app()
        with TestClient(app) as client:
            events = stream(client, "Theo chính sách gian lận nội bộ, khi nào cần step-up authentication?")
        self.assertEqual(terminal_states(events), ["rejected"])
        self.assertIn("invalid_business_request", final_message(events))
        self.assertEqual(mcp.requests, [])


if __name__ == "__main__":
    unittest.main()
