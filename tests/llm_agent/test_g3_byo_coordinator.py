from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Message, MessageSendParams, Part, Role, TextPart
from fastapi.testclient import TestClient

from ai_platform.llm_agent.business_request import a2a_message_send
from ai_platform.llm_agent.idempotency import IdempotencyScope, InMemoryIdempotencyStore
from ai_platform.llm_agent.byo_coordinator import (
    CoordinatorExecutor,
    _runtime_scope,
    build_app,
)
from ai_platform.llm_agent.byo_runtime import ControllerTokenHook
from ai_platform.llm_agent.programmatic_coordinator import (
    GatewayPolicy,
    ProgrammaticCoordinator,
    encode_specialist_task,
)
from tests.llm_agent.test_g3_programmatic_coordinator import evidence, request


ROOT = Path(__file__).resolve().parents[2]
SCOPE = IdempotencyScope("ewallet-agents", "kagent-controller")


class Recorder:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, call):
        self.calls.append(call)
        return self.response


def coordinator_factory(recorder: Recorder, idempotency_store=None):
    store = idempotency_store or InMemoryIdempotencyStore()

    def build():
        return ProgrammaticCoordinator(
            transport=recorder,
            synthesizer=lambda _request, _evidence: "Bằng chứng đã được xác thực.",
            idempotency_store=store,
        )

    return build


def wire_payload(*, task_id: str | None = None, context_id: str | None = None):
    req = request()
    payload = a2a_message_send(req)
    if task_id:
        payload["params"]["message"]["taskId"] = task_id
    if context_id:
        payload["params"]["message"]["contextId"] = context_id
    return req, payload


class BYOCoordinatorProtocolTests(unittest.TestCase):
    def app(self, *, store=None, timeout=5.0):
        req = request()
        recorder = Recorder(encode_specialist_task(req.request_id, evidence(req)))
        executor = CoordinatorExecutor(
            coordinator_factory(recorder), timeout_seconds=timeout, idempotency_scope=SCOPE,
        )
        return build_app(task_store=store or InMemoryTaskStore(), executor=executor), recorder

    def test_agent_card_and_health_endpoints_match_kagent_contract(self):
        app, _ = self.app()
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").text, "OK")
            self.assertEqual(client.get("/ready").text, "READY")
            card = client.get("/.well-known/agent-card.json")
        self.assertEqual(card.status_code, 200)
        value = card.json()
        self.assertEqual(value["name"], "fraud_coordinator")
        self.assertEqual(value["url"], "http://fraud-coordinator.ewallet-agents:8080")
        self.assertTrue(value["capabilities"]["streaming"])

    def test_runtime_scope_uses_only_deployment_identity_not_forwarded_headers(self):
        with patch.dict(
            "os.environ",
            {
                "EWALLET_IDEMPOTENCY_TENANT": "tenant-a",
                "EWALLET_IDEMPOTENCY_PRINCIPAL": "cluster.local/ns/default/sa/kagent-controller",
                "HTTP_X_USER_ID": "forged-user",
                "HTTP_X_FORWARDED_USER": "forged-forwarded-user",
            },
            clear=False,
        ):
            scope = _runtime_scope()
        self.assertEqual(
            (scope.tenant, scope.principal),
            ("tenant-a", "cluster.local/ns/default/sa/kagent-controller"),
        )

    def test_readiness_fails_closed_when_shared_store_is_unavailable(self):
        class UnreadyMemoryStore(InMemoryIdempotencyStore):
            def ready(self):
                raise RuntimeError("shared store unavailable")

        app, _ = self.app()
        req = request()
        recorder = Recorder(encode_specialist_task(req.request_id, evidence(req)))
        executor = CoordinatorExecutor(
            coordinator_factory(recorder, UnreadyMemoryStore()),
            timeout_seconds=5.0,
            idempotency_scope=SCOPE,
        )
        app = build_app(task_store=InMemoryTaskStore(), executor=executor)
        with TestClient(app) as client:
            response = client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.text, "shared idempotency authority unavailable")

    def test_actual_a2a_jsonrpc_request_returns_completed_task_and_artifact(self):
        app, recorder = self.app()
        req, payload = wire_payload(context_id="session-byo-1")
        with TestClient(app) as client:
            response = client.post("/", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["result"]
        self.assertTrue(task["id"])
        self.assertEqual(task["contextId"], "session-byo-1")
        self.assertEqual(task["status"]["state"], "completed")
        result = json.loads(task["artifacts"][0]["parts"][0]["text"])
        self.assertEqual(result["request_id"], req.request_id)
        self.assertEqual(result["fraud_score"], evidence(req)["fraud_score"])
        self.assertEqual(result["citations"], evidence(req)["citations"])
        self.assertEqual(len(recorder.calls), 1)

    def test_invalid_request_reaches_terminal_rejected_state_without_gateway_call(self):
        app, recorder = self.app()
        _, payload = wire_payload(context_id="session-invalid")
        payload["params"]["message"]["parts"][0]["text"] = "not the typed contract"
        with TestClient(app) as client:
            response = client.post("/", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["status"]["state"], "rejected")
        self.assertEqual(recorder.calls, [])

    def test_repeated_request_is_idempotent_within_worker(self):
        app, recorder = self.app()
        _, payload = wire_payload(context_id="session-duplicate")
        with TestClient(app) as client:
            first = client.post("/", json=payload)
            second = client.post("/", json=payload)
        self.assertEqual(first.json()["result"]["status"]["state"], "completed")
        self.assertEqual(second.json()["result"]["status"]["state"], "completed")
        self.assertEqual(len(recorder.calls), 1)

    def test_shared_task_store_recovers_completed_task_after_app_restart(self):
        store = InMemoryTaskStore()
        app1, recorder1 = self.app(store=store)
        _, payload = wire_payload(context_id="session-restart")
        with TestClient(app1) as client:
            first = client.post("/", json=payload)
        self.assertEqual(first.json()["result"]["status"]["state"], "completed")
        task_id = first.json()["result"]["id"]

        # A new app/executor instance models a worker restart while kagent's
        # controller-backed TaskStore remains durable.
        app2, recorder2 = self.app(store=store)
        poll = {"jsonrpc": "2.0", "id": "poll-restart", "method": "tasks/get", "params": {"id": task_id}}
        with TestClient(app2) as client:
            recovered = client.post("/", json=poll)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["result"]["status"]["state"], "completed")
        self.assertEqual(len(recorder1.calls), 1)
        self.assertEqual(recorder2.calls, [])

    def test_oversized_jsonrpc_body_is_rejected_before_execution(self):
        app, recorder = self.app()
        _, payload = wire_payload(context_id="session-large")
        payload["params"]["metadata"] = {"padding": "x" * 9_000}
        with TestClient(app) as client:
            response = client.post("/", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error"]["code"], -32600)
        self.assertEqual(response.json()["error"]["message"], "Payload too large")
        self.assertEqual(recorder.calls, [])


class BYOCoordinatorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cpu_specialist_deadline_is_bounded_but_allows_observed_runtime(self):
        recorder = Recorder({})
        executor = CoordinatorExecutor(
            coordinator_factory(recorder), timeout_seconds=480.0, idempotency_scope=SCOPE,
        )
        self.assertEqual(executor._timeout_seconds, 480.0)
        self.assertEqual(GatewayPolicy(timeout_seconds=480.0).timeout_seconds, 480.0)
        with self.assertRaises(ValueError):
            CoordinatorExecutor(
                coordinator_factory(recorder), timeout_seconds=600.1, idempotency_scope=SCOPE,
            )
        with self.assertRaises(ValueError):
            GatewayPolicy(timeout_seconds=600.1)

    async def test_cancel_marks_live_execution_and_emits_terminal_cancel(self):
        req, payload = wire_payload(task_id="task-cancel", context_id="context-cancel")
        message = Message.model_validate(payload["params"]["message"])
        params = MessageSendParams(message=message)
        context = RequestContext(params, task_id="task-cancel", context_id="context-cancel")
        recorder = Recorder(encode_specialist_task(req.request_id, evidence(req)))
        executor = CoordinatorExecutor(
            coordinator_factory(recorder), timeout_seconds=5.0, idempotency_scope=SCOPE,
        )
        event_queue = EventQueue()
        await executor.cancel(context, event_queue)
        event = await event_queue.dequeue_event(no_wait=True)
        self.assertEqual(event.status.state.value, "canceled")
        self.assertTrue(event.final)


class ControllerTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_projected_token_is_injected_but_never_part_of_url_or_body(self):
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "kagent-token"
            token_path.write_text("secret-projected-token\n", encoding="utf-8")
            hook = ControllerTokenHook(token_path=token_path)
            request_value = httpx.Request("POST", "http://kagent-controller/api/tasks", json={"id": "safe"})
            await hook.add_headers(request_value)
        self.assertEqual(request_value.headers["authorization"], "Bearer secret-projected-token")
        self.assertEqual(request_value.headers["x-agent-name"], "fraud-coordinator")
        self.assertNotIn("secret-projected-token", str(request_value.url))
        self.assertNotIn(b"secret-projected-token", request_value.content)

    async def test_hook_uses_explicit_agent_identity_and_reads_rotated_token(self):
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "kagent-token"
            token_path.write_text("first-token\n", encoding="utf-8")
            hook = ControllerTokenHook(
                app_name="feature-rag-specialist",
                token_path=token_path,
            )
            first = httpx.Request("POST", "http://kagent-controller/api/tasks")
            await hook.add_headers(first)
            token_path.write_text("rotated-token\n", encoding="utf-8")
            second = httpx.Request("POST", "http://kagent-controller/api/tasks")
            await hook.add_headers(second)

        self.assertEqual(first.headers["x-agent-name"], "feature-rag-specialist")
        self.assertEqual(first.headers["authorization"], "Bearer first-token")
        self.assertEqual(second.headers["authorization"], "Bearer rotated-token")


class BYOCoordinatorImageTests(unittest.TestCase):
    def test_git_is_available_only_in_dependency_build_stage(self):
        dockerfile = (ROOT / "infra/docker/g3-byo-coordinator/Dockerfile").read_text(encoding="utf-8")
        dependency_stage, runtime_stage = dockerfile.split("FROM python:", 2)[1:]
        self.assertIn("apt-get install -y --no-install-recommends git ca-certificates", dependency_stage)
        self.assertNotIn("apt-get install -y --no-install-recommends git", runtime_stage)

    def test_runtime_image_includes_transitive_feature_contract(self):
        dockerfile = (ROOT / "infra/docker/g3-byo-coordinator/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY ai_platform/features/__init__.py ai_platform/features/__init__.py", dockerfile)
        self.assertIn("COPY ai_platform/features/feast/schema.py ai_platform/features/feast/schema.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
