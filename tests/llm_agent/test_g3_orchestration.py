from __future__ import annotations

import unittest

from ai_platform.llm_agent.contracts import Citation, ErrorResponse, RequestContext, VersionContext
from ai_platform.llm_agent.orchestration import (
    CoordinatorOrchestrator,
    DRIFT_SPECIALIST,
    FEATURE_RAG_SPECIALIST,
    SpecialistResult,
)


CONTEXT = RequestContext(
    request_id="req-g3",
    traceparent="00-" + "1" * 32 + "-" + "2" * 16 + "-01",
    actor_id="fraud-coordinator",
    timeout_ms=20_000,
    tool_call_budget=3,
)
VERSIONS = VersionContext(
    git_sha="a" * 40,
    image_digest="sha256:" + "b" * 64,
    config_revision="g3-test",
    prompt_revision="fraud-coordinator/v2",
    model_revision="qwen3-0.6b-test",
    feature_revision="feature-test",
    corpus_revision="corpus-test",
)
CITATION = Citation(
    document_id="policy-1",
    chunk_id="policy-1:0001",
    source_uri="gs://example/policy-1.md",
    source_checksum="sha256:" + "c" * 64,
    corpus_revision="corpus-test",
    embedding_revision="minilm-test",
    feature_revision="feature-test",
    score=0.9,
)


class CoordinatorOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = CoordinatorOrchestrator()

    def test_selects_only_requested_specialists_and_preserves_tool_grants(self):
        fraud = self.orchestrator.plan(["fraud-evidence"])
        self.assertEqual(fraud.target_agents, (FEATURE_RAG_SPECIALIST,))
        request = self.orchestrator.request(
            target_agent=FEATURE_RAG_SPECIALIST,
            task="Investigate the supplied canonical transaction",
            context=CONTEXT,
            versions=VERSIONS,
        )
        self.assertEqual(request.allowed_tools, ("feature_lookup", "fraud_scoring", "knowledge_search"))

        both = self.orchestrator.plan(["fraud-evidence", "drift"])
        self.assertEqual(both.target_agents, (FEATURE_RAG_SPECIALIST, DRIFT_SPECIALIST))
        drift = self.orchestrator.request(
            target_agent=DRIFT_SPECIALIST,
            task="Evaluate the approved drift baseline",
            context=CONTEXT,
            versions=VERSIONS,
        )
        self.assertEqual(drift.allowed_tools, ("feature_lookup", "drift_detection"))

    def test_aggregates_business_evidence_without_inventing_values(self):
        plan = self.orchestrator.plan(["fraud-evidence", "drift"])
        result = self.orchestrator.aggregate(plan, (
            SpecialistResult(
                agent=FEATURE_RAG_SPECIALIST,
                status="ok",
                payload={"features": {"velocity_1h": 3}, "fraud_score": {
                    "probability": 0.81, "model_revision": "fraud/v7", "status": "ok",
                }},
                citations=(CITATION,),
            ),
            SpecialistResult(
                agent=DRIFT_SPECIALIST,
                status="ok",
                payload={"features": {"amount_24h": 500}, "drift": [{
                    "feature_name": "amount_24h", "status": "ok", "metric": 0.03,
                }]},
            ),
        ))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["fraud_score"]["probability"], 0.81)
        self.assertEqual(result["citations"][0]["chunk_id"], "policy-1:0001")
        self.assertEqual(result["drift"][0]["metric"], 0.03)

    def test_partial_specialist_failure_is_explicit_and_bounded(self):
        plan = self.orchestrator.plan(["fraud-evidence", "drift"])
        error = ErrorResponse("req-g3", "drift_backend_timeout", "Drift backend timed out", True, 500)
        result = self.orchestrator.aggregate(plan, (
            SpecialistResult(
                agent=FEATURE_RAG_SPECIALIST,
                status="ok",
                payload={"fraud_score": {"probability": 0.2, "status": "ok"}},
            ),
            SpecialistResult(
                agent=DRIFT_SPECIALIST,
                status="unavailable",
                errors=(error,),
            ),
        ))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unavailable"], [{
            "agent": DRIFT_SPECIALIST,
            "code": "drift_backend_timeout",
            "retryable": True,
        }])
        self.assertEqual(result["drift"], [])

    def test_rejects_unknown_capability_unauthorized_result_and_duplicate_result(self):
        with self.assertRaises(ValueError):
            self.orchestrator.plan(["shell"])
        plan = self.orchestrator.plan(["fraud-evidence"])
        drift = SpecialistResult(agent=DRIFT_SPECIALIST, status="ok")
        with self.assertRaises(ValueError):
            self.orchestrator.aggregate(plan, (drift,))
        feature = SpecialistResult(agent=FEATURE_RAG_SPECIALIST, status="ok")
        with self.assertRaises(ValueError):
            self.orchestrator.aggregate(plan, (feature, feature))


if __name__ == "__main__":
    unittest.main()
