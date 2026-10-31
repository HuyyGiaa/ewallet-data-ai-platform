from __future__ import annotations

import unittest

from ai_platform.llm_agent.contracts import (
    AgentDelegationRequest,
    Citation,
    RequestContext,
    ToolCallRequest,
    VersionContext,
)


class ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = RequestContext(
            request_id="req-1",
            traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            actor_id="analyst-1",
            timeout_ms=5000,
            tool_call_budget=4,
        )
        self.versions = VersionContext(
            git_sha="a" * 40,
            image_digest="sha256:" + "b" * 64,
            config_revision="config-v1",
            prompt_revision="prompt-v1",
            model_revision="model-v1",
            feature_revision="features-v1",
            corpus_revision="corpus-v1",
        )

    def test_valid_tool_and_native_a2a_contracts(self) -> None:
        request = ToolCallRequest(
            tool_name="knowledge_search",
            arguments={"query": "fraud policy"},
            context=self.context,
            versions=self.versions,
        )
        delegation = AgentDelegationRequest(
            target_agent="feature-rag-specialist",
            task="Find applicable policy evidence",
            context=self.context,
            versions=self.versions,
            allowed_tools=("feature_lookup", "knowledge_search"),
        )
        self.assertEqual(request.tool_name, "knowledge_search")
        self.assertNotIn("gateway", delegation.target_agent)

    def test_unknown_tool_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "tool_name"):
            ToolCallRequest(
                tool_name="delete_account",
                arguments={},
                context=self.context,
                versions=self.versions,
            )

    def test_w3c_traceparent_and_budgets_are_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "traceparent"):
            RequestContext("req", "invalid", "actor", 5000, 4)
        with self.assertRaisesRegex(ValueError, "tool_call_budget"):
            RequestContext(
                "req",
                "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
                "actor",
                5000,
                33,
            )

    def test_citation_preserves_all_required_revisions(self) -> None:
        citation = Citation(
            document_id="policy-1",
            chunk_id="policy-1:0001",
            source_uri="gs://corpus/policy-1.txt",
            source_checksum="sha256:" + "c" * 64,
            corpus_revision="corpus-v1",
            embedding_revision="embedding-v1",
            feature_revision="features-v1",
            score=0.91,
        )
        self.assertEqual(citation.chunk_id, "policy-1:0001")


if __name__ == "__main__":
    unittest.main()
