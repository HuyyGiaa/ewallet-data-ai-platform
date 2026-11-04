from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SHA = "48bf89c5c26fe6da9ba1b61fd0725535299f289b"
RAG_REF = ("asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/rag-ingestion@sha256:"
           + "a" * 64)


class ReleaseRequestChainTests(unittest.TestCase):
    """The exact CLI chain the Deploy and verify stage runs, short of --execute."""

    def run_cli(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, *argv], cwd=ROOT, text=True, capture_output=True, check=False)

    def test_scope_to_request_to_plan_to_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "scope.json").write_text(json.dumps({"components": ["agents", "mcp-agents", "rag-pipeline"]}))
            (work / "image-manifest.json").write_text(json.dumps(
                {"git_sha": SHA, "images": {"rag-ingestion": {"digest_ref": RAG_REF}}}))
            request = self.run_cli(
                "-m", "scripts.ci.release_request", "--scope", str(work / "scope.json"),
                "--profile", "llm-agent", "--approved-by", "admin", "--branch", "release/rag-pipeline",
                "--git-sha", SHA, "--environment", "gke-cpu", "--kube-context", "in-cluster",
                "--image-manifest", str(work / "image-manifest.json"), "--evidence-dir", str(work / "release"),
                "--output", str(work / "release-request.json"))
            self.assertEqual(request.returncode, 0, request.stderr)
            body = json.loads((work / "release-request.json").read_text())
            self.assertEqual(body["components"], ["agents", "mcp-agents", "rag-pipeline"])
            self.assertTrue(body["owner_approved"])
            plan = self.run_cli(
                "-m", "scripts.ci.release_policy", "--root", str(ROOT), "build",
                "--request", str(work / "release-request.json"),
                "--image-manifest", str(work / "image-manifest.json"), "--output", str(work / "plan.json"))
            self.assertEqual(plan.returncode, 0, plan.stderr)
            units = json.loads((work / "plan.json").read_text())["units"]
            self.assertEqual([unit["component"] for unit in units], ["rag-pipeline"])
            dispatch = subprocess.run([str(ROOT / "scripts/ci/deploy_release.sh"), "--plan", str(work / "plan.json")],
                                      cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(dispatch.returncode, 0, dispatch.stderr)
            self.assertIn(f"rag-pipeline -> scripts/ci/deploy/rag_pipeline.sh ({RAG_REF}) --git-sha {SHA}",
                          dispatch.stdout)

    def test_empty_approver_is_not_owner_approval(self) -> None:
        from scripts.ci.release_request import build_request

        body = build_request(scope={"components": ["rag-pipeline"]},
                             catalog_path=ROOT / "jenkins/catalog/components.json", profile="llm-agent",
                             approved_by=" ", branch="release/x", git_sha=SHA, environment="gke-cpu",
                             kube_context="in-cluster", image_manifest="m", evidence_dir="e")
        self.assertFalse(body["owner_approved"])


if __name__ == "__main__":
    unittest.main()
