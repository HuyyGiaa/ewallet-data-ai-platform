"""Airflow RAG ingestion stages: parse, chunk, embed, Feast publish, retrieval, DataHub.

Runs in the uv-locked RAG runtime (``uv run --locked --project
infra/docker/rag-ingestion``). Feast runs for real against a temporary
local registry and sqlite online store; embeddings are a deterministic
fixture unless EWALLET_RAG_MODEL_TESTS=1 enables the pinned ONNX model.
None of this is Airflow, Redis or DataHub runtime evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "data/llm_agent/rag/fraud-policy-v1"
HAS_RUNTIME = all(importlib.util.find_spec(name) for name in ("feast", "numpy", "fsspec"))

if HAS_RUNTIME:
    import numpy as np

    from ai_platform.llm_agent.rag import feast_publication as fp
    from ai_platform.llm_agent.rag import governance
    from ai_platform.llm_agent.rag.embedding import (
        LocalQueryEmbedder, OnnxSentenceEmbedder, check_sentence_transformer_layout, mean_pool_normalize,
    )
    from ai_platform.llm_agent.rag.online_retriever import FeastChunkRowSource, FeastOnlineExactRetriever
    from ai_platform.llm_agent.rag.pipeline import TrustedChunkIndex
    from ops.llm_agent import rag_ingest

DIMENSIONS = 384
VOCABULARY = ("account", "takeover", "manual", "review", "payment", "anomaly", "citation", "revision")


def fake_vector(text: str) -> list[float]:
    """Deterministic bag-of-keywords vector with a hashed tail; unit norm."""
    words = text.lower().split()
    vector = [float(sum(word.startswith(term) for word in words)) for term in VOCABULARY]
    digest = hashlib.sha256(text.encode()).digest()
    vector += [(digest[i % 32] / 255.0) * 0.01 for i in range(DIMENSIONS - len(VOCABULARY))]
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


class FakeEmbedder:
    model_sha256 = "fixture"

    def __init__(self, manifest=None):
        self.model_revision = manifest.embedding_model_revision if manifest else "0" * 40
        self.dimensions = DIMENSIONS

    def __call__(self, texts):
        return [fake_vector(text) for text in texts]


class FakeQueryEmbedder:
    def __init__(self, manifest):
        self.model_revision = manifest.embedding_model_revision
        self.dimensions = manifest.embedding_dimensions

    async def embed(self, text):
        return fake_vector(text)


def make_repo(folder: Path) -> Path:
    """A temporary repository root holding a copy of the versioned corpus."""
    target = folder / "data/llm_agent/rag/fraud-policy-v1"
    shutil.copytree(CORPUS, target)
    (folder / "infra/docker/rag-ingestion").mkdir(parents=True)
    return folder


@unittest.skipUnless(HAS_RUNTIME, "requires the uv-locked RAG ingestion runtime")
class RagCdContractTests(unittest.TestCase):
    """jenkins/cd/rag-pipeline.contract.json must track the versioned corpus it verifies."""

    def test_cd_contract_matches_the_corpus_manifest(self):
        contract = json.loads((ROOT / "jenkins/cd/rag-pipeline.contract.json").read_text())
        manifest = json.loads((CORPUS / "manifest.json").read_text())
        self.assertEqual(contract["corpus_revision"], manifest["corpus_revision"])
        self.assertEqual(contract["embedding_model"], manifest["embedding_model_id"])
        self.assertEqual(contract["embedding_revision"], manifest["embedding_model_revision"])
        self.assertEqual(contract["embedding_dimensions"], manifest["embedding_dimensions"])
        from ai_platform.llm_agent.rag.pipeline import chunk_documents, ingest_documents

        _, documents = ingest_documents(CORPUS / "manifest.json")
        chunks = chunk_documents(documents, size_words=manifest["chunk_size_words"],
                                 overlap_words=manifest["chunk_overlap_words"])
        self.assertEqual(contract["chunk_count"], len(chunks))
        self.assertEqual(contract["published_rows"], len(chunks))
        self.assertEqual(contract["min_recall_at_k"], 1.0)
        self.assertIn("feature-store", contract["forbidden_redis_targets"])


class RagIngestStageTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="rag-ingest-test."))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.repo = make_repo(self.folder)
        patches = [
            mock.patch.object(rag_ingest, "REPOSITORY_ROOT", self.repo),
            mock.patch.object(rag_ingest, "LOCKFILE", ROOT / "infra/docker/rag-ingestion/uv.lock"),
            mock.patch.object(rag_ingest, "_embedder", lambda manifest: FakeEmbedder(manifest)),
            mock.patch.dict(os.environ, {"EWALLET_RAG_PUBLICATION_BASE": str(self.folder / "feast")}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # validate() embeds queries through LocalQueryEmbedder(OnnxSentenceEmbedder); swap the model.
        query = mock.patch("ai_platform.llm_agent.rag.embedding.LocalQueryEmbedder",
                           side_effect=lambda embedder: FakeQueryEmbedder(
                               TrustedChunkIndex.from_manifest(CORPUS / "manifest.json").manifest))
        query.start()
        self.addCleanup(query.stop)

    def run_stage(self, stage, run_label="run-001", **flags):
        argv = [stage, "--run-label", run_label, "--online-store", "sqlite"]
        for name, value in flags.items():
            argv += [f"--{name.replace('_', '-')}", value]
        self.assertEqual(rag_ingest.main(argv), 0)
        return json.loads((rag_ingest.run_dir(run_label, root=self.repo) / rag_ingest.STAGES[stage][0]).read_text())

    def run_all(self, run_label="run-001"):
        return {stage: self.run_stage(stage, run_label, **({"datahub_mode": "plan_only"} if stage == "datahub" else {}))
                for stage in rag_ingest.STAGES}

    def test_full_pipeline_publishes_reads_back_and_cites_sources(self):
        out = self.run_all()
        documents, chunks, embeddings = out["discover"], out["chunk"], out["embed"]
        self.assertEqual((documents["document_count"], documents["checksum_mismatches"]), (2, 0))
        self.assertEqual(documents["runtime"]["packages"]["feast"], "0.66.0")
        self.assertTrue(chunks["deterministic"])
        self.assertEqual(chunks["chunk_count"], 4)
        self.assertEqual([c["chunk_id"] for c in chunks["chunks"]],
                         ["policy-account-takeover:0000", "policy-account-takeover:0001",
                          "policy-payment-anomaly:0000", "policy-payment-anomaly:0001"])
        self.assertEqual((embeddings["dimensions"], embeddings["vector_count"], embeddings["invalid_vectors"]),
                         (384, 4, 0))
        publication = out["publish"]
        self.assertEqual((publication["row_count"], publication["offline_status"]), (4, "WRITTEN"))
        self.assertEqual(publication["feast"]["profile"], "local-dev")
        validation = out["validate"]
        self.assertEqual((validation["readback_status"], validation["online_row_count"]), ("MATCH", 4))
        self.assertEqual((validation["recall_at_k"], validation["citation_errors"]), (1.0, 0))
        manifest = json.loads((CORPUS / "manifest.json").read_text())
        sources = {d["document_id"]: d for d in manifest["documents"]}
        for result in validation["results"]:
            self.assertLessEqual(len(result["citations"]), validation["top_k"])
            top = result["citations"][0]
            self.assertIn(top["document_id"], result["expected_document_ids"])
            self.assertEqual(top["source_uri"], sources[top["document_id"]]["source_uri"])
            self.assertEqual(top["source_checksum"], sources[top["document_id"]]["sha256"])
            self.assertEqual(top["corpus_revision"], manifest["corpus_revision"])
        datahub = out["datahub"]
        self.assertEqual(datahub["status"], "PLAN_ONLY_NOT_PUBLISHED")
        self.assertTrue(all(check["passed"] for check in datahub["quality_checks"]))

    def test_republishing_the_same_revision_reuses_rows_without_duplicates(self):
        self.run_all("run-001")
        second = self.run_all("run-002")
        self.assertEqual(second["publish"]["offline_status"], "REUSED")
        frame = __import__("pandas").read_parquet(self.folder / "feast/fraud-policy-feast-v1/chunks.parquet")
        self.assertEqual(len(frame), 4)
        self.assertTrue(frame.chunk_id.is_unique)
        self.assertEqual(second["validate"]["online_row_count"], 4)

    def test_image_release_sha_identifies_runs_without_git(self):
        # The CD image has no git binary and no .git; its baked release SHA is the identity.
        sha = "a" * 40
        with mock.patch.dict(os.environ, {"EWALLET_RELEASE_SHA": sha}), \
                mock.patch.object(rag_ingest.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(rag_ingest.runtime_identity()["git_sha"], sha)
        with mock.patch.dict(os.environ, {"EWALLET_RELEASE_SHA": "unknown"}), \
                mock.patch.object(rag_ingest.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(rag_ingest.runtime_identity()["git_sha"], "unavailable")

    def test_publication_records_the_release_that_produced_it(self):
        sha = "b" * 40
        with mock.patch.dict(os.environ, {"EWALLET_RELEASE_SHA": sha}):
            artifacts = self.run_all("run-release")
        self.assertEqual(artifacts["publish"]["release_sha"], sha)
        self.assertEqual(artifacts["publish"]["manifest_sha256"], artifacts["discover"]["manifest_sha256"])
        self.assertEqual(artifacts["datahub"]["release_sha"], artifacts["publish"]["release_sha"])
        self.assertEqual(artifacts["validate"]["release_sha"], artifacts["publish"]["release_sha"])
        self.assertEqual(artifacts["chunk"]["release_sha"], sha)
        self.assertEqual(artifacts["embed"]["release_sha"], sha)

    def test_completed_stage_is_never_overwritten(self):
        self.run_stage("discover")
        with self.assertRaises(FileExistsError):
            rag_ingest.main(["discover", "--run-label", "run-001", "--online-store", "sqlite"])

    def test_stage_without_upstream_artifact_fails(self):
        with self.assertRaisesRegex(FileNotFoundError, "documents.json"):
            rag_ingest.main(["chunk", "--run-label", "run-009", "--online-store", "sqlite"])

    def test_missing_and_tampered_documents_fail_discovery(self):
        corpus = self.repo / "data/llm_agent/rag/fraud-policy-v1"
        (corpus / "policy-payment-anomaly.md").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            rag_ingest.main(["discover", "--run-label", "run-010", "--online-store", "sqlite"])
        (corpus / "policy-payment-anomaly.md").unlink()
        with self.assertRaises(FileNotFoundError):
            rag_ingest.main(["discover", "--run-label", "run-011", "--online-store", "sqlite"])
        self.assertFalse((rag_ingest.run_dir("run-011", root=self.repo) / "documents.json").exists())

    def test_unsafe_manifest_path_is_rejected(self):
        manifest_path = self.repo / "data/llm_agent/rag/fraud-policy-v1/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["documents"][0]["path"] = "../../../../etc/passwd"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "unsafe document path"):
            rag_ingest.main(["discover", "--run-label", "run-012", "--online-store", "sqlite"])

    def test_redis_profile_requires_explicit_configuration(self):
        for stage in ("discover", "chunk", "embed"):
            self.run_stage(stage)
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("EWALLET_RAG_FEAST_REGISTRY", "EWALLET_RAG_FEAST_PROJECT", "EWALLET_REDIS_CONNECTION_STRING"):
                os.environ.pop(name, None)
            with self.assertRaisesRegex(ValueError, "missing configuration"):
                rag_ingest.main(["publish", "--run-label", "run-001", "--online-store", "redis"])

    def test_datahub_publish_mode_fails_without_gms_url(self):
        for stage in ("discover", "chunk", "embed", "publish", "validate"):
            self.run_stage(stage)
        with mock.patch.dict(os.environ, {"DATAHUB_GMS_URL": ""}):
            with self.assertRaisesRegex(ValueError, "DATAHUB_GMS_URL is not configured"):
                rag_ingest.main(["datahub", "--run-label", "run-001", "--online-store", "sqlite"])
        self.assertFalse((rag_ingest.run_dir("run-001", root=self.repo) / "datahub.json").exists())


@unittest.skipUnless(HAS_RUNTIME, "requires the uv-locked RAG ingestion runtime")
class RunIdentityTests(unittest.TestCase):
    def test_run_id_is_label_or_sanitized_airflow_run_id(self):
        self.assertEqual(rag_ingest.derive_run_id(run_label="demo-01", airflow_run_id="x"), "demo-01")
        self.assertEqual(rag_ingest.derive_run_id(airflow_run_id="manual__2026-11-01T00:00:00+00:00"),
                         "manual-2026-11-01t00-00-00-00-00")
        self.assertEqual(rag_ingest.derive_run_id(airflow_run_id="$(id);`x`/../.."), "id-x")
        self.assertEqual(len(rag_ingest.derive_run_id(airflow_run_id="a" * 300)), 80)
        with self.assertRaises(ValueError):
            rag_ingest.derive_run_id(airflow_run_id="//")

    def test_run_dir_rejects_unsafe_ids(self):
        for value in ("", "-x", "../x", "a b", "a" * 81):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rag_ingest.run_dir(value)

    def test_private_env_file_fills_only_unset_allowlisted_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runtime.env"
            path.write_text("# comment\nDATAHUB_GMS_URL=http://datahub:8080\nEWALLET_RAG_FEAST_PROJECT=from-file\n")
            path.chmod(0o600)
            env = {"EWALLET_RAG_ENV_FILE": str(path), "EWALLET_RAG_FEAST_PROJECT": "from-env"}
            self.assertEqual(rag_ingest.load_env_file(env), ["DATAHUB_GMS_URL"])
            self.assertEqual((env["DATAHUB_GMS_URL"], env["EWALLET_RAG_FEAST_PROJECT"]),
                             ("http://datahub:8080", "from-env"))
            path.chmod(0o644)
            with self.assertRaises(PermissionError):
                rag_ingest.load_env_file({"EWALLET_RAG_ENV_FILE": str(path)})
            path.chmod(0o600)
            path.write_text("PATH=/tmp/evil\n")
            with self.assertRaisesRegex(ValueError, "only KEY=VALUE"):
                rag_ingest.load_env_file({"EWALLET_RAG_ENV_FILE": str(path)})
            with self.assertRaises(FileNotFoundError):
                rag_ingest.load_env_file({"EWALLET_RAG_ENV_FILE": f"{folder}/missing.env"})
        self.assertEqual(rag_ingest.load_env_file({}), [])
        with tempfile.TemporaryDirectory() as folder:
            example = Path(folder) / "runtime.env"
            shutil.copy(ROOT / "infra/docker/rag-ingestion/runtime.env.example", example)
            example.chmod(0o600)
            env = {"EWALLET_RAG_ENV_FILE": str(example)}
            loaded = rag_ingest.load_env_file(env)
            self.assertIn("EWALLET_REDIS_CONNECTION_STRING", loaded)
            self.assertNotIn("DATAHUB_GMS_TOKEN", env)  # empty values stay unset
        with mock.patch.object(rag_ingest, "DEFAULT_ENV_FILE", Path("/nonexistent/runtime.env")):
            self.assertEqual(rag_ingest.load_env_file({"EWALLET_RAG_ENV_FILE": "/nonexistent/runtime.env"}), [])

    def test_write_once_is_atomic_and_create_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x/out.json"
            rag_ingest.write_once(path, {"a": 1})
            with self.assertRaises(FileExistsError):
                rag_ingest.write_once(path, {"a": 2})
            self.assertEqual(json.loads(path.read_text())["a"], 1)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["out.json"])


@unittest.skipUnless(HAS_RUNTIME, "requires the uv-locked RAG ingestion runtime")
class EmbeddingContractTests(unittest.TestCase):
    def test_mean_pooling_ignores_padding_and_normalizes(self):
        tokens = np.array([[[1.0, 0.0], [3.0, 4.0], [100.0, 100.0]]], dtype=np.float32)
        mask = np.array([[1, 1, 0]])
        (vector,) = mean_pool_normalize(tokens, mask)
        expected = np.array([2.0, 2.0]) / math.sqrt(8.0)
        np.testing.assert_allclose(vector, expected, rtol=1e-6)

    def test_only_mean_pooled_normalized_pipeline_is_accepted(self):
        modules = [{"type": t} for t in ("sentence_transformers.models.Transformer",
                                         "sentence_transformers.models.Pooling",
                                         "sentence_transformers.models.Normalize")]
        check_sentence_transformer_layout(modules, {"pooling_mode_mean_tokens": True})
        with self.assertRaises(ValueError):
            check_sentence_transformer_layout(modules[:2], {"pooling_mode_mean_tokens": True})
        with self.assertRaises(ValueError):
            check_sentence_transformer_layout(modules, {"pooling_mode_mean_tokens": True,
                                                        "pooling_mode_cls_token": True})

    def test_model_revision_must_be_immutable(self):
        for revision in ("main", "1110a243", "Z" * 40):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                OnnxSentenceEmbedder("m", revision, 384)
        with self.assertRaises(ValueError):
            OnnxSentenceEmbedder("m", "a" * 40, 0)

    def test_empty_text_is_rejected_before_loading_a_model(self):
        embedder = OnnxSentenceEmbedder("m", "a" * 40, 384)
        self.assertEqual(embedder([]), [])
        with self.assertRaises(ValueError):
            embedder(["ok", "  "])

    @unittest.skipUnless(os.environ.get("EWALLET_RAG_MODEL_TESTS") == "1", "set EWALLET_RAG_MODEL_TESTS=1")
    def test_pinned_model_produces_deterministic_unit_vectors(self):
        manifest = TrustedChunkIndex.from_manifest(CORPUS / "manifest.json").manifest
        embedder = OnnxSentenceEmbedder(manifest.embedding_model_id, manifest.embedding_model_revision,
                                        manifest.embedding_dimensions)
        first = embedder(["account takeover manual review", "the weather is sunny"])
        self.assertEqual(first, embedder(["account takeover manual review", "the weather is sunny"]))
        for vector in first:
            self.assertEqual(len(vector), 384)
            self.assertAlmostEqual(math.sqrt(sum(v * v for v in vector)), 1.0, places=4)
        query = asyncio.run(LocalQueryEmbedder(embedder).embed("account takeover"))
        self.assertGreater(float(np.dot(query, first[0])), float(np.dot(query, first[1])))


@unittest.skipUnless(HAS_RUNTIME, "requires the uv-locked RAG ingestion runtime")
class FeastPublicationContractTests(unittest.TestCase):
    def rows(self):
        from datetime import datetime, timezone

        from ai_platform.llm_agent.rag.pipeline import (
            chunk_documents, embed_chunks, feast_publication_rows, ingest_documents,
        )
        manifest, documents = ingest_documents(CORPUS / "manifest.json")
        chunks = chunk_documents(documents, size_words=80, overlap_words=16)
        embedded = embed_chunks(chunks, embed=FakeEmbedder(), dimensions=DIMENSIONS,
                                model_revision=manifest.embedding_model_revision,
                                index_revision=manifest.feature_index_revision,
                                published_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
        return manifest, list(feast_publication_rows(embedded))

    def test_frame_schema_matches_the_feature_view(self):
        from ai_platform.features.feast.feature_repo.rag_feature_definitions import build_rag_feature_objects

        _, rows = self.rows()
        frame = fp.publication_frame(rows, dimensions=DIMENSIONS)
        entity, view = build_rag_feature_objects("x.parquet")
        expected = {f.name for f in view.schema} | {entity.join_key} | {"event_timestamp", "publication_timestamp"}
        self.assertEqual(set(frame.columns), expected)
        self.assertEqual(frame.embedding[0].dtype, np.float32)
        self.assertEqual(str(frame.event_timestamp.dt.tz), "UTC")

    def test_invalid_embeddings_and_duplicates_are_rejected(self):
        _, rows = self.rows()
        for change in ({"embedding": [1.0] * 3}, {"embedding": [0.0] * DIMENSIONS},
                       {"embedding": [float("nan")] * DIMENSIONS}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                fp.publication_frame([{**rows[0], **change}, *rows[1:]], dimensions=DIMENSIONS)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            fp.publication_frame([rows[0], rows[0]], dimensions=DIMENSIONS)
        with self.assertRaises(ValueError):
            fp.publication_frame([], dimensions=DIMENSIONS)

    def test_identity_ignores_order_and_detects_text_changes(self):
        _, rows = self.rows()
        self.assertEqual(fp.publication_identity(rows), fp.publication_identity(rows[::-1]))
        self.assertNotEqual(fp.publication_identity(rows),
                            fp.publication_identity([{**rows[0], "chunk_text": "x"}, *rows[1:]]))

    def test_immutable_revision_rejects_a_different_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            _, rows = self.rows()
            target = fp.FeastTarget("sqlite", "p", f"{folder}/r.db", f"{folder}/rev")
            frame = fp.publication_frame(rows, dimensions=DIMENSIONS)
            self.assertEqual(fp.write_offline_source(frame, target), "WRITTEN")
            self.assertEqual(fp.write_offline_source(frame, target), "REUSED")
            changed = fp.publication_frame([{**rows[0], "chunk_text": "changed"}, *rows[1:]], dimensions=DIMENSIONS)
            with self.assertRaisesRegex(ValueError, "different publication"):
                fp.write_offline_source(changed, target)

    def test_target_profiles_and_secret_free_description(self):
        with self.assertRaisesRegex(ValueError, "missing configuration"):
            fp.resolve_target(online_store="redis", publication_base="gs://b/rag", feature_index_revision="v1",
                              environment={"EWALLET_RAG_FEAST_REGISTRY": "gs://b/registry.db"})
        registry = {"EWALLET_RAG_FEAST_REGISTRY": "gs://b/registry.db"}
        # No database (Redis would use production DB 0), or DB 0 for verification: refused before any write.
        for connection in ("redis:6379,password=s3cret", "redis:6379,password=s3cret,db=0"):
            with self.subTest(connection=connection), self.assertRaisesRegex(ValueError, "database"):
                fp.resolve_target(online_store="redis", publication_base="gs://b/rag", feature_index_revision="v1",
                                  environment={**registry, "EWALLET_REDIS_CONNECTION_STRING": connection,
                                               "EWALLET_RAG_FEAST_PROJECT": "ewallet_rag_verify"})
        with self.assertRaisesRegex(ValueError, "own Feast project"):
            fp.resolve_target(online_store="redis", publication_base="gs://b/rag", feature_index_revision="v1",
                              environment={**registry, "EWALLET_REDIS_CONNECTION_STRING": "redis:6379,db=3"})
        target = fp.resolve_target(online_store="redis", publication_base="gs://b/rag", feature_index_revision="v1",
                                   environment={**registry,
                                                "EWALLET_REDIS_CONNECTION_STRING": "redis:6379,password=s3cret,db=3",
                                                "EWALLET_RAG_FEAST_PROJECT": "ewallet_rag_verify"})
        self.assertEqual(target.source_path, "gs://b/rag/v1/chunks.parquet")
        self.assertEqual(target.project, "ewallet_rag_verify")
        production = fp.resolve_target(online_store="redis", publication_base="gs://b/rag", feature_index_revision="v1",
                                       environment={**registry, "EWALLET_RAG_ONLINE_TARGET": "production",
                                                    "EWALLET_REDIS_CONNECTION_STRING": "redis:6379,db=0"})
        self.assertEqual(production.project, fp.DEFAULT_RAG_PROJECT)
        self.assertNotIn("s3cret", json.dumps(target.describe()))
        self.assertEqual(target.describe()["profile"], "canonical")
        self.assertEqual(fp.repo_config(target).online_store.type, "redis")
        local = fp.resolve_target(online_store="sqlite", publication_base="/tmp/x", feature_index_revision="v1",
                                  environment={})
        self.assertEqual((local.project, local.describe()["profile"]), (fp.LOCAL_PROJECT, "local-dev"))
        with self.assertRaises(ValueError):
            fp.FeastTarget("memory", "p", "r", "x")

    def test_feast_readback_feeds_top_k_retrieval_and_mcp_contract(self):
        from feast import FeatureStore

        from ai_platform.llm_agent.contracts import RequestContext
        from ai_platform.llm_agent.tools.knowledge_search import KnowledgeSearchInput, KnowledgeSearchTool

        manifest, rows = self.rows()
        with tempfile.TemporaryDirectory() as folder:
            target = fp.resolve_target(online_store="sqlite", publication_base=folder,
                                       feature_index_revision=manifest.feature_index_revision, environment={})
            frame = fp.publication_frame(rows, dimensions=DIMENSIONS)
            fp.write_offline_source(frame, target)
            store = FeatureStore(config=fp.repo_config(target))
            fp.apply_and_materialize(store, frame, target)
            readback = fp.online_readback(store, [r["chunk_id"] for r in rows] + ["missing:0000"])
            self.assertEqual([r["chunk_id"] for r in readback[:4]], [r["chunk_id"] for r in rows])
            self.assertIsNone(readback[4]["embedding"])
            self.assertEqual(len(readback[0]["embedding"]), DIMENSIONS)

            index = TrustedChunkIndex.from_manifest(CORPUS / "manifest.json")
            retriever = FeastOnlineExactRetriever(index, FeastChunkRowSource(store, timeout_seconds=10.0),
                                                  FakeQueryEmbedder(manifest), cache_seconds=0.0)
            hits = asyncio.run(retriever.search("account takeover manual review", top_k=2, min_score=0.0))
            self.assertEqual(len(hits), 2)
            self.assertEqual(hits[0].document_id, "policy-account-takeover")
            self.assertGreaterEqual(hits[0].score, hits[1].score)

            tool = KnowledgeSearchTool(retriever)
            context = mock.Mock(spec=RequestContext)
            outcome = asyncio.run(tool.run(KnowledgeSearchInput(query="payment anomaly citation revision", top_k=3),
                                           context))
            result = outcome.result
            self.assertEqual(result["retrieval_backend"], "feast-online-redis+exact-cosine")
            self.assertEqual(result["results"][0]["document_id"], "policy-payment-anomaly")
            self.assertNotIn("query", result)
            self.assertEqual([c.chunk_id for c in outcome.citations], [r["chunk_id"] for r in result["results"]])
            self.assertTrue(all(c.source_checksum.startswith("sha256:") for c in outcome.citations))
            empty = asyncio.run(tool.run(KnowledgeSearchInput(query="unrelated", top_k=3, min_score=0.999), context))
            self.assertEqual((empty.result["results"], empty.citations), ([], ()))
            for bad in ({"query": "x"}, {"query": "ok query", "top_k": 6}, {"query": "a\x00bc"},
                        {"query": "ok query", "extra": 1}):
                with self.subTest(bad=bad), self.assertRaises(Exception):
                    KnowledgeSearchInput(**bad)


@unittest.skipUnless(HAS_RUNTIME, "requires the uv-locked RAG ingestion runtime")
class GovernanceTests(unittest.TestCase):
    ARTIFACTS = {
        "documents": {"document_count": 2, "checksum_mismatches": 0, "source_location": "data/llm_agent/rag/x",
                      "corpus_revision": "c1", "manifest_sha256": "m" * 64},
        "chunks": {"chunks": [{"chunk_id": "a:0000"}, {"chunk_id": "a:0001"}], "deterministic": True},
        "embeddings": {"invalid_vectors": 0, "vector_count": 2, "dimensions": 384, "model_id": "mid",
                       "model_revision": "r" * 40},
        "publication": {"row_count": 2, "feature_index_revision": "f1", "publication_identity": "i" * 64,
                        "feast": {"project": "ewallet_fraud_online", "offline_source": "gs://b/f1/chunks.parquet",
                                  "online_store": "redis"}},
        "validation": {"readback_status": "MATCH", "online_row_count": 2, "empty_retrieval_rate": 0.0,
                       "citation_errors": 0, "recall_at_k": 1.0},
    }

    class MemoryAdapter:
        def __init__(self, corrupt=None):
            self.store, self.corrupt = {}, corrupt

        def put(self, urn, aspect, payload):
            self.store[(urn, aspect)] = json.loads(json.dumps(payload))
            if aspect == self.corrupt:
                self.store[(urn, aspect)]["tampered"] = True

        def get(self, urn, aspect):
            return self.store.get((urn, aspect))

    def records(self, **overrides):
        artifacts = {**self.ARTIFACTS, **overrides}
        checks = governance.quality_checks(**artifacts)
        feast = governance.feast_urn("ewallet_fraud_online", "f1")
        return checks, governance.lineage_records(
            documents=artifacts["documents"], embeddings=artifacts["embeddings"],
            publication=artifacts["publication"], run_id="run-001", git_sha="abc",
        ) + governance.assertion_records(checks, feast_dataset=feast, feature_index_revision="f1",
                                         run_id="run-001", timestamp_millis=1)

    def test_lineage_connects_corpus_to_feast_through_dag_tasks(self):
        checks, records = self.records()
        self.assertTrue(all(check.passed for check in checks))
        source = governance.corpus_urn("data/llm_agent/rag/x", "c1")
        feast = governance.feast_urn("ewallet_fraud_online", "f1")
        lineage = {(u, a): p for u, a, p in records}
        self.assertEqual(lineage[(feast, "upstreamLineage")], {"upstreams": [{"dataset": source, "type": "TRANSFORMED"}]})
        publish_job = "urn:li:dataJob:(urn:li:dataFlow:(airflow,rag_fraud_policy_ingestion,ewallet-gcp),publish_feast_features)"
        self.assertEqual(lineage[(publish_job, "dataJobInputOutput")]["outputDatasets"], [feast])
        self.assertEqual(lineage[(feast, "datasetProperties")]["customProperties"]["embedding_dimensions"], "384")
        self.assertEqual(sum(a == "assertionInfo" for _, a, _ in records), 5)
        self.assertEqual(governance.corpus_urn("gs://b/x", "c1").split(",")[0], "urn:li:dataset:(urn:li:dataPlatform:gcs")

    def test_failed_measurements_become_failed_assertions(self):
        validation = {**self.ARTIFACTS["validation"], "online_row_count": 1, "citation_errors": 1}
        checks, records = self.records(validation=validation)
        failed = {c.assertion_id for c in checks if not c.passed}
        self.assertEqual(failed, {"online_rows_match_offline_publication", "retrieval_returns_cited_source"})
        results = [p["result"] for _, a, p in records if a == "assertionRunEvent"]
        self.assertEqual(results.count("FAILURE"), 2)

    def test_publication_requires_matching_readback(self):
        _, records = self.records()
        verified = governance.publish_with_readback(records, self.MemoryAdapter())
        self.assertEqual(len(verified), len(records))
        self.assertTrue(all(item["readback"] == "MATCH" for item in verified))
        with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
            governance.publish_with_readback(records, self.MemoryAdapter(corrupt="upstreamLineage"))

    def test_timeseries_readback_waits_for_async_indexing_but_stays_strict(self):
        _, records = self.records()

        class LaggingAdapter(self.MemoryAdapter):
            def __init__(self, lag):
                super().__init__()
                self.lag, self.reads = lag, {}

            def get(self, urn, aspect):
                if aspect != "assertionRunEvent":
                    return super().get(urn, aspect)
                self.reads[urn] = self.reads.get(urn, 0) + 1
                return None if self.reads[urn] <= self.lag else super().get(urn, aspect)

        verified = governance.publish_with_readback(records, LaggingAdapter(lag=2), delay_seconds=0)
        events = [item for item in verified if item["aspect"] == "assertionRunEvent"]
        self.assertTrue(events and all(item["attempts"] == "3" for item in events))
        with self.assertRaisesRegex(RuntimeError, "assertionRunEvent"):
            governance.publish_with_readback(records, LaggingAdapter(lag=5), timeseries_attempts=3, delay_seconds=0)
        # Versioned aspects are never retried.
        with self.assertRaisesRegex(RuntimeError, "upstreamLineage"):
            governance.publish_with_readback(records, self.MemoryAdapter(corrupt="upstreamLineage"), delay_seconds=0)

    def test_assertion_urns_are_stable_per_revision(self):
        self.assertEqual(governance.assertion_urn("x", "f1"), governance.assertion_urn("x", "f1"))
        self.assertNotEqual(governance.assertion_urn("x", "f1"), governance.assertion_urn("x", "f2"))


@unittest.skipUnless(HAS_RUNTIME, "requires the uv-locked RAG ingestion runtime")
class McpWiringTests(unittest.TestCase):
    def test_rag_registry_gets_its_own_store_on_the_same_redis(self):
        from types import SimpleNamespace

        from ai_platform.inference.errors import ConfigurationError
        from ai_platform.llm_agent.tools.service import _rag_feature_store

        main = SimpleNamespace(config=SimpleNamespace(online_store=SimpleNamespace(
            type="redis", connection_string="redis.feature-store:6379,password=x")))
        self.assertIs(_rag_feature_store(main, {}, timeout_seconds=1.0), main)
        with tempfile.TemporaryDirectory() as folder:
            rag = _rag_feature_store(main, {"EWALLET_RAG_FEAST_REGISTRY": f"{folder}/registry.db"},
                                     timeout_seconds=1.0)
            self.assertEqual(rag.config.project, "ewallet_rag")
            self.assertEqual(rag.config.online_store.connection_string,
                             "redis.feature-store:6379,password=x,socket_connect_timeout=1,socket_timeout=1")
            self.assertEqual(rag.config.registry.path, f"{folder}/registry.db")
        sqlite_main = SimpleNamespace(config=SimpleNamespace(online_store=SimpleNamespace(type="sqlite")))
        with self.assertRaises(ConfigurationError):
            _rag_feature_store(sqlite_main, {"EWALLET_RAG_FEAST_REGISTRY": "gs://b/r.db"}, timeout_seconds=1.0)

    def test_local_onnx_embedder_wires_exact_retrieval_without_loading_the_model(self):
        from ai_platform.inference.errors import ConfigurationError
        from ai_platform.llm_agent.tools.service import _knowledge_retriever

        index = TrustedChunkIndex.from_manifest(CORPUS / "manifest.json")
        retriever = _knowledge_retriever(index, object(), 1.0, {"EWALLET_RAG_RETRIEVER": "feast-online-exact",
                                                                 "EWALLET_RAG_EMBEDDER": "local-onnx"})
        self.assertIsInstance(retriever, FeastOnlineExactRetriever)
        # The production profile sets a baked model cache (G3 GKE: /opt/hf-cache);
        # that branch once raised NameError (Path) at pod start.
        cached = _knowledge_retriever(index, object(), 1.0, {"EWALLET_RAG_RETRIEVER": "feast-online-exact",
                                                              "EWALLET_RAG_EMBEDDER": "local-onnx",
                                                              "EWALLET_RAG_MODEL_CACHE": "/opt/hf-cache"})
        self.assertIsInstance(cached, FeastOnlineExactRetriever)
        for values in ({"EWALLET_RAG_RETRIEVER": "feast-online-exact", "EWALLET_RAG_EMBEDDER": "remote"},
                       {"EWALLET_RAG_RETRIEVER": "feast-online-exact"}):
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                _knowledge_retriever(index, object(), 1.0, values)


if __name__ == "__main__":
    unittest.main()
