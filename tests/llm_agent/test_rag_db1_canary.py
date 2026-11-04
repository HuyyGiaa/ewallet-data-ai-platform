"""Contracts for the short-lived, production-equivalent RAG DB1 MCP canary."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from ops.llm_agent.rag_db1_canary import (
    CanaryConfig,
    derive_redis_database,
    mcp_headers,
    render_resources,
    verify_read_after_write,
)


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "docs/evidence/llm_agent/rag_g3-functional-rag-20261102t045700z"


class CanaryManifestTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "image": "registry.example/mcp-tools@sha256:" + "a" * 64,
            "git_sha": "b" * 40,
            "registry_config_map": "rag-db1-registry-test",
            "redis_secret": "rag-db1-redis-test",
            "auth_secret": "mcp-auth-feature-rag-mcp-r1-transition",
            "project": "ewallet_rag_campaign",
            "run_id": "g3-functional-rag-20261102t045700z",
            "corpus_revision": "fraud-policy-v1-20261030",
            "feature_revision": "fraud-policy-feast-v1",
        }
        values.update(overrides)
        return CanaryConfig(**values)

    def test_manifest_is_isolated_hardened_and_contains_no_secret_values(self):
        items = render_resources(self.config())
        kinds = [(item["kind"], item["metadata"]["namespace"], item["metadata"]["name"]) for item in items]
        self.assertEqual(kinds, [
            ("Deployment", "ewallet-mcp", "rag-db1-canary"),
            ("NetworkPolicy", "ewallet-mcp", "allow-ambient-probes-rag-db1-canary"),
            ("NetworkPolicy", "ewallet-mcp", "allow-rag-db1-canary-to-feast-redis"),
            ("NetworkPolicy", "feature-store", "allow-ewallet-mcp-rag-db1-canary-to-feast-redis"),
        ])
        deployment = items[0]
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/name"], "rag-db1-canary")
        self.assertNotEqual(labels["app.kubernetes.io/name"], "feature-rag-mcp")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        container = pod["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        env = {entry["name"]: entry for entry in container["env"]}
        self.assertEqual(env["EWALLET_RAG_FEAST_PROJECT"]["value"], "ewallet_rag_campaign")
        self.assertEqual(env["EWALLET_RAG_FEAST_REGISTRY"]["value"], "/feast/rag/registry.db")
        self.assertEqual(json.loads(env["EWALLET_MCP_TOOL_GRANTS"]["value"]), {
            "drift-specialist": ["feature_lookup"],
            "feature-rag-specialist": ["feature_lookup", "fraud_scoring", "knowledge_search"],
        })
        self.assertEqual(json.loads(env["EWALLET_MCP_DISCOVERY_SUBJECTS"]["value"]), ["kagent-controller"])
        self.assertEqual(env["EWALLET_REDIS_CONNECTION_STRING"]["valueFrom"]["secretKeyRef"]["name"],
                         "rag-db1-redis-test")
        self.assertEqual(env["EWALLET_MCP_SERVICE_TOKENS"]["valueFrom"]["secretKeyRef"]["name"],
                         "mcp-auth-feature-rag-mcp-r1-transition")
        serialized = json.dumps(items)
        self.assertNotIn("connection_string\": \"redis", serialized)
        self.assertNotIn("kind\": \"Secret", serialized)
        self.assertNotIn("ClusterRole", serialized)

    def test_mutable_image_and_invalid_identity_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "digest"):
            self.config(image="registry.example/mcp-tools:latest")
        with self.assertRaisesRegex(ValueError, "git_sha"):
            self.config(git_sha="short")

    def test_feast_redis_connection_is_derived_without_changing_credentials(self):
        self.assertEqual(
            derive_redis_database("redis.internal:6379,password=test-only", 1),
            "redis.internal:6379,password=test-only,db=1",
        )
        self.assertEqual(
            derive_redis_database("redis.internal:6379,db=0,password=test-only", 1),
            "redis.internal:6379,db=1,password=test-only",
        )

    def test_mcp_client_uses_the_production_service_token_header(self):
        self.assertEqual(mcp_headers("test-only-token"), {
            "x-ewallet-mcp-service-token": "test-only-token",
        })


class CanaryReadAfterWriteTests(unittest.TestCase):
    def load(self, name):
        return json.loads((RUN / name).read_text())

    def test_real_mcp_response_binds_to_immutable_airflow_publication(self):
        result = verify_read_after_write(
            response=self.load("current-mcp-knowledge-search.json"),
            documents=self.load("documents.json"),
            chunks=self.load("chunks.json"),
            publication=self.load("publication.json"),
            redis_readback=self.load("redis-db1-readback.json"),
            registry_sha256="f" * 64,
        )
        self.assertEqual(result["verdict"], "MCP_CANARY_READ_AFTER_WRITE_PASS")
        self.assertEqual(result["airflow_run_id"], "g3-functional-rag-20261102t045700z")
        self.assertEqual(result["publication_identity"],
                         "b85ae796221aa0b9e2adfd7ff19488f612dff18c8d60c3c44f0134b7266bf549")
        self.assertEqual(result["redis_db"], 1)
        self.assertEqual(result["production_db0_cutover"], "NOT_PERFORMED")

    def test_tampered_citation_is_rejected(self):
        response = self.load("current-mcp-knowledge-search.json")
        response["structured_content"]["citations"][0]["source_checksum"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "citation"):
            verify_read_after_write(
                response=response,
                documents=self.load("documents.json"),
                chunks=self.load("chunks.json"),
                publication=self.load("publication.json"),
                redis_readback=self.load("redis-db1-readback.json"),
                registry_sha256="f" * 64,
            )


if __name__ == "__main__":
    unittest.main()
