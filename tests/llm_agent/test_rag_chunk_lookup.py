"""chunk_lookup (rubric edai-2 row 16) through the authenticated tool boundary.

The row source is an in-memory stand-in for Feast Online built from the
versioned corpus; Feast itself is exercised in test_rag_airflow_ingestion.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from tests.llm_agent.test_phase_d_tools import ENV, FASTAPI, SUBJECT, asgi, rpc, run

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data/llm_agent/rag/fraud-policy-v1/manifest.json"
TOKEN = {"authorization": "Bearer fixture-token-specialist"}

if FASTAPI:
    from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex
    from ai_platform.llm_agent.tools.chunk_lookup import ChunkLookupInput, ChunkLookupTool
    from ai_platform.llm_agent.tools.runtime import ToolExecutor
    from ai_platform.llm_agent.tools.security import ToolGrants
    from ai_platform.llm_agent.tools.service import create_tool_app, versions_from_env
    from tests.llm_agent.test_phase_d_tools import FixtureAuthenticator


class MemoryRows:
    """Rows shaped like FeastChunkRowSource output; ``overrides`` tamper per chunk."""

    def __init__(self, index, **overrides):
        manifest = index.manifest
        self.rows = {
            chunk_id: {
                "chunk_id": chunk_id, "document_id": chunk.document_id, "chunk_text": chunk.text,
                "source_uri": chunk.source_uri, "source_checksum": chunk.source_checksum,
                "corpus_revision": manifest.corpus_revision,
                "embedding_model_revision": manifest.embedding_model_revision,
                "feature_index_revision": manifest.feature_index_revision,
                "embedding": [1.0] + [0.0] * (manifest.embedding_dimensions - 1),
            }
            for chunk_id, chunk in index.chunks.items()
        }
        for chunk_id, change in overrides.items():
            self.rows[chunk_id.replace("__", ":")].update(change)
        self.requests = []

    async def fetch(self, chunk_ids):
        self.requests.append(list(chunk_ids))
        return [self.rows.get(chunk_id, {"chunk_id": chunk_id}) for chunk_id in chunk_ids]


@unittest.skipUnless(FASTAPI, "FastAPI/Pydantic unavailable")
class ChunkLookupTests(unittest.TestCase):
    def setUp(self):
        self.index = TrustedChunkIndex.from_manifest(MANIFEST)

    def app(self, rows=None, tools=("chunk_lookup",)):
        tool = ChunkLookupTool(self.index, rows or MemoryRows(self.index))
        executor = ToolExecutor((tool,), versions=versions_from_env(ENV))
        return create_tool_app(service_name="feature-rag-mcp", executor=executor,
                               authenticator=FixtureAuthenticator(), grants=ToolGrants({SUBJECT: frozenset(tools)}))

    def call(self, app, chunk_id, headers=TOKEN):
        return run(asgi(app, "POST", "/v1/tools/chunk_lookup", {"chunk_id": chunk_id}, headers=headers))

    def test_granted_caller_gets_the_published_chunk_with_a_manifest_citation(self):
        rows = MemoryRows(self.index)
        status, body = self.call(self.app(rows), "policy-payment-anomaly:0001")
        self.assertEqual((status, body["status"]), (200, "ok"))
        chunk = self.index.chunks["policy-payment-anomaly:0001"]
        result = body["result"]
        self.assertEqual((result["chunk_id"], result["document_id"], result["chunk_text"]),
                         (chunk.chunk_id, chunk.document_id, chunk.text))
        self.assertEqual(result["embedding_dimensions"], 384)
        self.assertNotIn("embedding", result)
        (citation,) = body["citations"]
        self.assertEqual(citation["chunk_id"], chunk.chunk_id)
        self.assertEqual(citation["source_checksum"], f"sha256:{chunk.source_checksum}")
        self.assertEqual(citation["corpus_revision"], self.index.manifest.corpus_revision)
        self.assertEqual(rows.requests, [["policy-payment-anomaly:0001"]])

    def test_identity_is_required_and_grants_are_default_deny(self):
        app = self.app()
        self.assertEqual(self.call(app, "policy-payment-anomaly:0000", headers={})[0], 401)
        # A caller-supplied actor header is not an identity.
        spoofed = self.call(app, "policy-payment-anomaly:0000", headers={"x-ewallet-actor": SUBJECT})
        self.assertEqual(spoofed[0], 401)
        self.assertEqual(self.call(app, "policy-payment-anomaly:0000",
                                   headers={"authorization": "Bearer fixture-token-other"})[0], 403)
        status, body = run(asgi(app, "POST", "/mcp", rpc("tools/list"),
                                headers={"authorization": "Bearer fixture-token-other"}))
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["tools"], [])

    def test_listed_only_to_granted_subjects(self):
        status, body = run(asgi(self.app(), "POST", "/mcp", rpc("tools/list"), headers=TOKEN))
        self.assertEqual(status, 200)
        (tool,) = body["result"]["tools"]
        self.assertEqual(tool["name"], "chunk_lookup")
        self.assertEqual(tool["inputSchema"]["required"], ["chunk_id"])

    def test_unknown_and_malformed_ids_are_rejected_without_a_backend_call(self):
        rows = MemoryRows(self.index)
        app = self.app(rows)
        status, body = self.call(app, "policy-payment-anomaly:0999")
        self.assertEqual((status, body["result"]["error"]["code"]), (422, "unknown_chunk"))
        for bad in ("policy-payment-anomaly", "../etc:0000", "POLICY:0000", "a:00001", "x" * 200 + ":0000"):
            with self.subTest(bad=bad):
                self.assertEqual(self.call(app, bad)[0], 422)
        self.assertEqual(rows.requests, [])
        with self.assertRaises(Exception):
            ChunkLookupInput(chunk_id="policy-payment-anomaly:0000", extra=1)

    def test_tampered_or_unmaterialized_rows_fail_closed(self):
        tampered = MemoryRows(self.index, **{"policy-account-takeover__0000": {"chunk_text": "injected text"}})
        status, body = self.call(self.app(tampered), "policy-account-takeover:0000")
        self.assertEqual((status, body["result"]["error"]["code"]), (502, "rag_index_provenance_mismatch"))
        relabelled = MemoryRows(self.index, **{"policy-account-takeover__0000": {"corpus_revision": "other"}})
        self.assertEqual(self.call(self.app(relabelled), "policy-account-takeover:0000")[0], 502)
        missing = MemoryRows(self.index, **{"policy-account-takeover__0000": {"embedding": None}})
        status, body = self.call(self.app(missing), "policy-account-takeover:0000")
        self.assertEqual((status, body["result"]["error"]["code"]), (503, "rag_index_not_materialized"))
        short = MemoryRows(self.index, **{"policy-account-takeover__0000": {"embedding": [1.0, 0.0]}})
        self.assertEqual(self.call(self.app(short), "policy-account-takeover:0000")[0], 502)

    def test_readiness_requires_a_materialized_matching_chunk(self):
        run(ChunkLookupTool(self.index, MemoryRows(self.index)).ready())
        first = min(self.index.chunks).replace(":", "__")
        with self.assertRaises(Exception):
            run(ChunkLookupTool(self.index, MemoryRows(self.index, **{first: {"embedding": None}})).ready())


if __name__ == "__main__":
    unittest.main()
