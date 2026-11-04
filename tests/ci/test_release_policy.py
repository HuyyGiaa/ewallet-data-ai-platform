from __future__ import annotations

from pathlib import Path
import unittest

from scripts.ci.catalog import load_catalog
from scripts.ci.release_policy import ReleasePolicyError, ReleaseRequest, build_release_plan


ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "jenkins/catalog/components.json")
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "e" * 64


class ReleasePolicyTests(unittest.TestCase):
    def request(self, **changes: object) -> ReleaseRequest:
        values = {
            "requested": True,
            "owner_approved": True,
            "owner_approval": "owner@example.invalid",
            "branch": "release/2026-11-01",
            "git_sha": SHA,
            "checkout_sha": SHA,
            "environment": "gcp",
            "kube_context": "gke-project-region-cluster",
            "components": ("mlflow", "ml-training"),
            "image_manifest_path": "evidence/image-digests.json",
            "evidence_dir": "evidence/release",
        }
        values.update(changes)
        return ReleaseRequest(**values)

    def manifest(self, **changes: str) -> dict:
        artifacts = {
            "ml-training": f"registry.example/ewallet/training@{DIGEST}",
            "mlflow": f"registry.example/ewallet/mlflow@{DIGEST}",
        }
        artifacts.update(changes)
        return {"git_sha": SHA, "artifacts": artifacts}

    def test_deployment_is_denied_by_default(self) -> None:
        with self.assertRaisesRegex(ReleasePolicyError, "disabled"):
            build_release_plan(CATALOG, self.request(requested=False), self.manifest())

    def test_requires_owner_approval_release_branch_and_matching_sha(self) -> None:
        cases = (
            (self.request(owner_approved=False), "owner approval"),
            (self.request(branch="codex/feature"), "release branch"),
            (self.request(checkout_sha="f" * 40), "checkout SHA"),
        )
        for request, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ReleasePolicyError, message):
                build_release_plan(CATALOG, request, self.manifest())

    def test_rejects_unknown_environment_and_mutable_image(self) -> None:
        with self.assertRaisesRegex(ReleasePolicyError, "environment"):
            build_release_plan(CATALOG, self.request(environment="production"), self.manifest())
        with self.assertRaisesRegex(ReleasePolicyError, "immutable digest"):
            build_release_plan(
                CATALOG,
                self.request(),
                self.manifest(mlflow="registry.example/ewallet/mlflow:latest"),
            )

    def test_dependency_order_and_extension_only_exclusion(self) -> None:
        plan = build_release_plan(
            CATALOG,
            self.request(components=("mlflow", "ml-training", "llm-d-routing")),
            self.manifest(),
        )
        self.assertEqual([unit["component"] for unit in plan["units"]], ["ml-training", "mlflow"])
        self.assertNotIn("llm-d-routing", {unit["component"] for unit in plan["units"]})
        self.assertTrue(all("@sha256:" in unit["digest_ref"] for unit in plan["units"]))

    def test_extension_only_component_can_never_create_a_release_unit(self) -> None:
        with self.assertRaisesRegex(ReleasePolicyError, "no deployable"):
            build_release_plan(
                CATALOG,
                self.request(components=("llm-d-routing",)),
                {"git_sha": SHA, "images": {"x": {"digest_ref": f"registry.example/ewallet/x@{DIGEST}"}}},
            )

    def test_rag_release_takes_only_its_own_cloud_build_digest(self) -> None:
        manifest = {"git_sha": SHA, "images": {
            "rag-ingestion": {"digest_ref": f"registry.example/ewallet/rag-ingestion@{DIGEST}"}}}
        plan = build_release_plan(CATALOG, self.request(components=("rag-pipeline",)), manifest)
        self.assertEqual(plan["units"], [{
            "component": "rag-pipeline",
            "handler": "scripts/ci/deploy/rag_pipeline.sh",
            "target": "infra/kubernetes/cicd-runtime/rag",
            "digest_ref": f"registry.example/ewallet/rag-ingestion@{DIGEST}",
            "digest_refs": [f"registry.example/ewallet/rag-ingestion@{DIGEST}"],
        }])

    def test_llm_release_preserves_existing_images_manifest_contract(self) -> None:
        manifest = {
            "git_sha": SHA,
            "images": {
                "mcp-tools": {"digest_ref": f"registry.example/ewallet/mcp-tools@{DIGEST}"},
                "vllm-cpu-model": {"digest_ref": f"registry.example/ewallet/vllm@{DIGEST}"},
            },
        }
        plan = build_release_plan(
            CATALOG,
            self.request(components=("llm-serving", "mcp-agents")),
            manifest,
        )
        self.assertEqual([unit["component"] for unit in plan["units"]], ["llm-serving"])
        self.assertEqual(len(plan["units"][0]["digest_refs"]), 2)


if __name__ == "__main__":
    unittest.main()
