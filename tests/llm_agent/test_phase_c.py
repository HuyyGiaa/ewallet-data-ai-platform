from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_platform.llm_agent.rag.feast_contract import assess_vector_compatibility, datahub_lineage_plan
from ai_platform.llm_agent.rag.evaluation import run_evaluation
from ai_platform.llm_agent.rag.pipeline import (
    chunk_documents,
    citations,
    embed_chunks,
    evaluation_metrics,
    feast_publication_rows,
    ingest_documents,
    retrieve_exact,
)
from ops.llm_agent.rag_pipeline import validate_manifest
from ops.llm_agent.verify_rag_artifacts import verify


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data/llm_agent/rag/fraud-policy-v1/manifest.json"


class PhaseCRagTest(unittest.TestCase):
    def test_manifest_ingestion_checksums_and_chunk_identity(self):
        manifest, documents = ingest_documents(MANIFEST)
        chunks = chunk_documents(
            documents,
            size_words=manifest.chunk_size_words,
            overlap_words=manifest.chunk_overlap_words,
        )
        self.assertEqual(len(documents), 2)
        self.assertEqual(len({item.chunk_id for item in chunks}), len(chunks))
        self.assertTrue(all(item.corpus_revision == manifest.corpus_revision for item in chunks))

    def test_checksum_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "policy.md").write_text("tampered", encoding="utf-8")
            payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
            payload["documents"] = [{
                "document_id": "tampered",
                "path": "policy.md",
                "source_uri": "repo://tampered",
                "sha256": "0" * 64,
                "event_timestamp": "2026-10-30T00:00:00Z",
            }]
            path = root / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                ingest_documents(path)

    def test_embedding_dimension_and_cardinality_fail_closed(self):
        manifest, documents = ingest_documents(MANIFEST)
        chunks = chunk_documents(documents, size_words=80, overlap_words=16)
        with self.assertRaisesRegex(ValueError, "cardinality"):
            embed_chunks(
                chunks, embed=lambda _: [], dimensions=384,
                model_revision=manifest.embedding_model_revision,
                index_revision=manifest.feature_index_revision,
                published_at=datetime.now(timezone.utc),
            )
        with self.assertRaisesRegex(ValueError, "dimension"):
            embed_chunks(
                chunks, embed=lambda texts: [[1.0] for _ in texts], dimensions=384,
                model_revision=manifest.embedding_model_revision,
                index_revision=manifest.feature_index_revision,
                published_at=datetime.now(timezone.utc),
            )

    def test_exact_retrieval_citations_and_metrics(self):
        manifest, documents = ingest_documents(MANIFEST)
        chunks = chunk_documents(documents, size_words=80, overlap_words=16)
        vectors = {
            "policy-account-takeover": (1.0, 0.0),
            "policy-payment-anomaly": (0.0, 1.0),
        }
        embedded = embed_chunks(
            chunks,
            embed=lambda texts: [vectors[chunks[index].document_id] for index, _ in enumerate(texts)],
            dimensions=2,
            model_revision=manifest.embedding_model_revision,
            index_revision=manifest.feature_index_revision,
            published_at=datetime.now(timezone.utc),
        )
        hits = retrieve_exact((1.0, 0.0), embedded, top_k=2, threshold=0.5)
        self.assertEqual(hits[0].embedded_chunk.chunk.document_id, "policy-account-takeover")
        citation = citations(hits)[0]
        self.assertEqual(citation.source_checksum, hits[0].embedded_chunk.chunk.source_checksum)
        self.assertTrue(citation.quote)
        metrics = evaluation_metrics([hits], [{"policy-account-takeover"}])
        self.assertEqual(metrics["recall_at_k"], 1.0)
        self.assertEqual(metrics["mean_reciprocal_rank"], 1.0)

    def test_feast_rows_preserve_all_version_and_time_fields(self):
        manifest, documents = ingest_documents(MANIFEST)
        chunks = chunk_documents(documents, size_words=80, overlap_words=16)
        embedded = embed_chunks(
            chunks,
            embed=lambda texts: [[1.0, 0.5] for _ in texts],
            dimensions=2,
            model_revision=manifest.embedding_model_revision,
            index_revision=manifest.feature_index_revision,
            published_at=datetime.now(timezone.utc),
        )
        row = feast_publication_rows(embedded)[0]
        required = {
            "document_id", "chunk_id", "source_uri", "source_checksum",
            "corpus_revision", "embedding_model_revision", "feature_index_revision",
            "event_timestamp", "publication_timestamp", "embedding",
        }
        self.assertTrue(required <= row.keys())

    def test_redis_is_publication_only_not_vector_equivalent(self):
        report = assess_vector_compatibility(feast_version="0.66.0", online_store="redis")
        self.assertTrue(report.array_float32_supported)
        self.assertFalse(report.vector_search_supported)
        self.assertEqual(report.status, "PUBLICATION_ONLY_RUNTIME_BLOCKED")
        self.assertTrue(report.blocker)

    def test_datahub_plan_is_not_runtime_verified(self):
        plan = datahub_lineage_plan(corpus_revision="c1", feature_index_revision="i1")
        self.assertFalse(plan["runtime_verified"])
        self.assertIn("upstreamLineage", plan["required_readback"])

    def test_manifest_validate_only_is_source_evidence(self):
        result = validate_manifest(MANIFEST)
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["runtime_verified"])

    def test_evaluation_emits_real_citations_and_fail_closed_status(self):
        def embed(texts):
            vectors = []
            for text in texts:
                lowered = text.lower()
                vector = [0.0] * 384
                vector[0 if "account" in lowered or "takeover" in lowered else 1] = 1.0
                vectors.append(vector)
            return vectors

        result = run_evaluation(
            manifest_path=MANIFEST,
            evaluation_path=MANIFEST.with_name("evaluation.json"),
            embed=embed,
        )
        self.assertFalse(result["runtime_verified"])
        self.assertEqual(result["metrics"]["query_count"], 2.0)
        self.assertTrue(all(item["citations"] for item in result["results"]))

    def test_airflow_dag_preserves_manual_source_only_gate(self):
        source = (ROOT / "data_platform/orchestration/airflow/dags/rag_fraud_policy.py").read_text()
        self.assertIn('dag_id="rag_fraud_policy"', source)
        self.assertIn('schedule=None', source)
        self.assertIn('max_active_runs=1', source)
        self.assertIn('build_feast_publication_artifact', source)
        self.assertIn('evaluate_retrieval_with_citations', source)
        self.assertIn('verify_source_artifacts_and_runtime_gate', source)
        self.assertNotIn('python_module_command("feast', source)
        self.assertNotIn('DatahubRestEmitter', source)

    def test_artifact_verifier_rejects_runtime_claim_and_accepts_provenance(self):
        identity = {
            "corpus_revision": "c1",
            "embedding_model_revision": "m1",
            "feature_index_revision": "i1",
        }
        row = {
            "document_id": "d", "chunk_id": "c", "source_uri": "repo://d",
            "source_checksum": "0" * 64, "event_timestamp": "2026-10-30T00:00:00+00:00",
            "publication_timestamp": "2026-10-30T00:00:01+00:00", "embedding": [1.0],
            **identity,
        }
        build = {
            **identity, "runtime_verified": False, "chunk_count": 1,
            "feast_publication_rows": [row],
            "datahub_lineage_plan": {"runtime_verified": False, "required_readback": ["upstreamLineage"]},
        }
        evaluation = {
            **identity, "runtime_verified": False,
            "metrics": {"recall_at_k": 1.0, "mean_reciprocal_rank": 1.0, "empty_retrieval_rate": 0.0},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_path, eval_path = root / "build.json", root / "eval.json"
            build_path.write_text(json.dumps(build), encoding="utf-8")
            eval_path.write_text(json.dumps(evaluation), encoding="utf-8")
            self.assertEqual(verify(build_path, eval_path)["status"], "SOURCE_ONLY_PASS")
            build["runtime_verified"] = True
            build_path.write_text(json.dumps(build), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not claim"):
                verify(build_path, eval_path)


if __name__ == "__main__":
    unittest.main()
