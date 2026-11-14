"""Agent integration: Coordinator routing of drift-only and combined workflows.

The Coordinator core is real. The A2A transport is a recorder that returns
completed-task payloads; the drift findings in those payloads are produced by
the real ``DriftDetectionTool`` over the synthetic fixture, so their PSI and
classification are internally consistent.
"""

from __future__ import annotations

import asyncio
from threading import Lock
import time
import unittest

from opentelemetry import propagate, trace

from ai_platform.llm_agent.business_request import (
    G3BusinessRequest,
    G3DriftRequest,
    G3WorkflowRequest,
    a2a_message_send,
    member_request_id,
)
from ai_platform.llm_agent.contracts import RequestContext
from ai_platform.llm_agent.idempotency import IdempotencyScope, InMemoryIdempotencyStore
from ai_platform.llm_agent.programmatic_coordinator import (
    CoordinatorFailure,
    ProgrammaticCoordinator,
    encode_specialist_task,
)
from ai_platform.llm_agent.tools.drift import DriftDetectionInput, DriftDetectionTool
from ai_platform.llm_agent.tools.drift_sources import drift_backends_from_env
from tests.llm_agent.agi_support import ACCOUNT_BASELINE, ACCOUNT_FEATURE, CURRENT_WINDOW, DriftFixture
from tests.llm_agent.test_agi_contracts import TRANSACTION

SCOPE = IdempotencyScope("ewallet-agents", "kagent-controller")
FEATURE = "account_behavior__account_tx_count_1h"


def fraud_request(request_id: str) -> G3BusinessRequest:
    return G3BusinessRequest(
        request_id=request_id, target_agent="feature-rag-specialist", capabilities=("fraud-evidence",),
        requested_tools=("feature_lookup", "fraud_scoring"), transaction=TRANSACTION, feature_names=(FEATURE,),
    )


def drift_request(request_id: str, **changes) -> G3DriftRequest:
    value = {"request_id": request_id, "feature_name": ACCOUNT_FEATURE, "baseline_id": ACCOUNT_BASELINE,
             "current_window": CURRENT_WINDOW}
    value.update(changes)
    return G3DriftRequest.model_validate(value)


def workflow_request(request_id: str = "wf-agi-1") -> G3WorkflowRequest:
    return G3WorkflowRequest(request_id=request_id, fraud=fraud_request(member_request_id(request_id, "fraud")),
                             drift=drift_request(member_request_id(request_id, "drift")))


def fraud_evidence(request_id: str, **changes) -> dict:
    value = {
        "status": "ok", "request_id": request_id, "features": {FEATURE: 3},
        "fraud_score": {"fraud_probability": 0.81, "model_version": "1"}, "drift": [], "citations": [],
        "unavailable": [], "contract_version": "g3-orchestration/v1",
        "prompt_revision": "feature-rag-specialist/byo-v1",
    }
    value.update(changes)
    return value


class Specialists:
    """Records each Gateway call and answers per target agent (x-kagent-host)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.trace_ids = []
        self._lock = Lock()

    def __call__(self, call):
        headers = dict(call.headers)
        propagate.inject(headers)
        with self._lock:
            self.calls.append(call)
            self.trace_ids.append(headers.get("traceparent", "").split("-")[1] if "traceparent" in headers else "")
        target = call.headers["x-kagent-host"].split(".")[0]
        answer = self.answers[target]
        if isinstance(answer, Exception):
            raise answer
        return answer(call.payload) if callable(answer) else answer


class CoordinatorWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = DriftFixture()
        tool = DriftDetectionTool(*drift_backends_from_env(cls.fixture.drift_env))
        arguments = DriftDetectionInput(feature_name=ACCOUNT_FEATURE, baseline_id=ACCOUNT_BASELINE,
                                        current_window=CURRENT_WINDOW)
        context = RequestContext(request_id="x", traceparent="00-" + "3" * 32 + "-" + "4" * 16 + "-01",
                                 actor_id="drift-specialist", timeout_ms=5_000, tool_call_budget=1)
        cls.finding = dict(asyncio.run(tool.run(arguments, context)).result)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def drift_evidence(self, request_id: str, **changes) -> dict:
        value = {"status": "ok", "request_id": request_id, "drift": [self.finding], "unavailable": [],
                 "contract_version": "g3-orchestration/v1", "prompt_revision": "drift-specialist/byo-v1"}
        value.update(changes)
        return value

    def coordinator(self, transport, store=None):
        return ProgrammaticCoordinator(transport=transport, synthesizer=lambda request, evidence: "Đã xác thực.",
                                       idempotency_store=store or InMemoryIdempotencyStore())

    def test_drift_request_is_routed_only_to_the_drift_specialist(self):
        request = drift_request("drift-agi-1")
        specialists = Specialists({"drift-specialist": encode_specialist_task(
            request.request_id, self.drift_evidence(request.request_id))})
        result = self.coordinator(specialists).handle(a2a_message_send(request), scope=SCOPE, session_id="s")
        self.assertEqual([call.headers["x-kagent-host"] for call in specialists.calls], ["drift-specialist.ewallet-agents"])
        self.assertEqual(specialists.calls[0].url, "http://a2a-gateway.ewallet-agents.svc.cluster.local:8083/")
        self.assertEqual((result["contract_version"], result["workflow"], result["status"]),
                         ("g3-orchestration/v2", "drift", "complete"))
        self.assertEqual(result["specialists"]["drift-specialist"]["drift"][0]["psi"], self.finding["psi"])
        self.assertNotIn("feature-rag-specialist", result["specialists"])

    def test_combined_request_reaches_both_specialists_and_labels_each_result(self):
        request = workflow_request()
        specialists = Specialists({
            "feature-rag-specialist": encode_specialist_task(request.fraud.request_id, fraud_evidence(request.fraud.request_id)),
            "drift-specialist": encode_specialist_task(request.drift.request_id, self.drift_evidence(request.drift.request_id)),
        })
        result = self.coordinator(specialists).handle(a2a_message_send(request), scope=SCOPE, session_id="s")
        self.assertEqual(sorted(call.headers["x-kagent-host"] for call in specialists.calls),
                         ["drift-specialist.ewallet-agents", "feature-rag-specialist.ewallet-agents"])
        sections = result["specialists"]
        self.assertEqual((result["status"], result["workflow"]), ("complete", "combined"))
        self.assertEqual(sections["feature-rag-specialist"]["fraud_score"]["fraud_probability"], 0.81)
        self.assertNotIn("drift", sections["feature-rag-specialist"])
        self.assertNotIn("fraud_score", sections["drift-specialist"])
        self.assertEqual(sections["drift-specialist"]["request_id"], "wf-agi-1.drift")

    def test_combined_delegations_run_concurrently(self):
        request = workflow_request("wf-agi-concurrent")

        def slow(evidence):
            def answer(_payload):
                time.sleep(0.4)
                return evidence
            return answer

        specialists = Specialists({
            "feature-rag-specialist": slow(encode_specialist_task(request.fraud.request_id, fraud_evidence(request.fraud.request_id))),
            "drift-specialist": slow(encode_specialist_task(request.drift.request_id, self.drift_evidence(request.drift.request_id))),
        })
        started = time.monotonic()
        self.coordinator(specialists).handle(a2a_message_send(request), scope=SCOPE, session_id="s")
        self.assertLess(time.monotonic() - started, 0.75)

    def test_partial_failure_is_explicit_and_never_invented(self):
        request = workflow_request("wf-agi-partial")
        specialists = Specialists({
            "feature-rag-specialist": encode_specialist_task(request.fraud.request_id, fraud_evidence(request.fraud.request_id)),
            "drift-specialist": CoordinatorFailure("a2a_transport_error", "A2A Gateway request failed"),
        })
        result = self.coordinator(specialists).handle(a2a_message_send(request), scope=SCOPE, session_id="s")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["specialists"]["drift-specialist"]["status"], "failed")
        self.assertNotIn("drift", result["specialists"]["drift-specialist"])
        self.assertEqual(result["unavailable"], [{"agent": "drift-specialist", "code": "a2a_transport_error",
                                                  "message": "A2A Gateway request failed"}])

    def test_tool_level_unavailability_is_carried_with_its_specialist(self):
        request = drift_request("drift-agi-unavailable")
        evidence = self.drift_evidence(request.request_id, status="unavailable", drift=[], unavailable=[
            {"tool": "drift_detection", "status": "rejected", "code": "unknown_baseline",
             "message": "No baseline with this id exists for the feature", "retryable": False}])
        result = self.coordinator(Specialists({"drift-specialist": encode_specialist_task(request.request_id, evidence)})
                                  ).handle(a2a_message_send(request), scope=SCOPE, session_id="s")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["unavailable"][0]["detail"]["code"], "unknown_baseline")

    def test_all_specialists_failing_fails_the_task(self):
        request = workflow_request("wf-agi-down")
        failure = CoordinatorFailure("a2a_transport_error", "A2A Gateway request failed")
        with self.assertRaises(CoordinatorFailure) as caught:
            self.coordinator(Specialists({"feature-rag-specialist": failure, "drift-specialist": failure})).handle(
                a2a_message_send(request), scope=SCOPE, session_id="s")
        self.assertEqual(caught.exception.code, "specialists_unavailable")
        with self.assertRaises(CoordinatorFailure) as single:
            self.coordinator(Specialists({"drift-specialist": failure})).handle(
                a2a_message_send(drift_request("drift-agi-down")), scope=SCOPE, session_id="s")
        self.assertEqual(single.exception.code, "a2a_transport_error")

    def test_malformed_or_mismatched_drift_evidence_is_rejected(self):
        request = drift_request("drift-agi-bad")
        tampered = dict(self.finding, psi=self.finding["psi"] + 1.0)
        reclassified = dict(self.finding, drift_status="no_drift")
        other_window = dict(self.finding, current_window={"start": "2026-05-02T00:00:00+00:00",
                                                          "end": "2026-05-31T00:00:00+00:00"})
        cases = {
            "psi not equal to bins": self.drift_evidence(request.request_id, drift=[tampered]),
            "classification disagrees with thresholds": self.drift_evidence(request.request_id, drift=[reclassified]),
            "another window": self.drift_evidence(request.request_id, drift=[other_window]),
            "fraud data from drift agent": self.drift_evidence(request.request_id, features={FEATURE: 1}),
            "two findings": self.drift_evidence(request.request_id, drift=[self.finding, self.finding]),
            "unavailable without reason": self.drift_evidence(request.request_id, status="unavailable", drift=[]),
        }
        for label, evidence in cases.items():
            with self.subTest(label), self.assertRaises(CoordinatorFailure) as caught:
                self.coordinator(Specialists({"drift-specialist": encode_specialist_task(request.request_id, evidence)})
                                 ).handle(a2a_message_send(request), scope=SCOPE, session_id="s")
            self.assertEqual(caught.exception.code, "invalid_specialist_evidence")

    def test_duplicate_requests_are_idempotent_and_conflicts_are_rejected(self):
        request = workflow_request("wf-agi-idem")
        specialists = Specialists({
            "feature-rag-specialist": encode_specialist_task(request.fraud.request_id, fraud_evidence(request.fraud.request_id)),
            "drift-specialist": encode_specialist_task(request.drift.request_id, self.drift_evidence(request.drift.request_id)),
        })
        coordinator = self.coordinator(specialists)
        first = coordinator.handle(a2a_message_send(request), scope=SCOPE, session_id="s")
        second = coordinator.handle(a2a_message_send(request), scope=SCOPE, session_id="s2")
        self.assertEqual(first, second)
        self.assertEqual(len(specialists.calls), 2)
        changed = G3WorkflowRequest(request_id="wf-agi-idem", fraud=request.fraud,
                                    drift=drift_request("wf-agi-idem.drift",
                                                        current_window={"start": "2026-05-02T00:00:00+00:00",
                                                                        "end": "2026-05-31T00:00:00+00:00"}))
        with self.assertRaises(CoordinatorFailure) as caught:
            coordinator.handle(a2a_message_send(changed), scope=SCOPE, session_id="s3")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_fraud_only_path_keeps_the_v1_contract(self):
        request = fraud_request("g3-agi-fraud")
        specialists = Specialists({"feature-rag-specialist": encode_specialist_task(
            request.request_id, fraud_evidence(request.request_id))})
        result = self.coordinator(specialists).handle(a2a_message_send(request), scope=SCOPE, session_id="s")
        self.assertEqual(result["contract_version"], "g3-orchestration/v1")
        self.assertNotIn("specialists", result)

    def test_trace_context_reaches_every_delegation(self):
        from opentelemetry.sdk.trace import TracerProvider

        if not isinstance(trace.get_tracer_provider(), TracerProvider):
            trace.set_tracer_provider(TracerProvider())
        request = workflow_request("wf-agi-trace")
        specialists = Specialists({
            "feature-rag-specialist": encode_specialist_task(request.fraud.request_id, fraud_evidence(request.fraud.request_id)),
            "drift-specialist": encode_specialist_task(request.drift.request_id, self.drift_evidence(request.drift.request_id)),
        })
        with trace.get_tracer("agi-test").start_as_current_span("root") as root:
            self.coordinator(specialists).handle(a2a_message_send(request), scope=SCOPE, session_id="s")
            expected = f"{root.get_span_context().trace_id:032x}"
        self.assertEqual(specialists.trace_ids, [expected, expected])


if __name__ == "__main__":
    unittest.main()
