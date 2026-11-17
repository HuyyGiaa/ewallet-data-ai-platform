"""The exact CLI chain the release stages run, short of --execute: scope -> request -> images -> plan -> dispatch."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HEAD = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def ref(name: str) -> str:
    return f"asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/{name}@sha256:" + "a" * 64


class ReleaseRequestChainTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, *argv], cwd=ROOT, text=True, capture_output=True, check=False)

    def chain(self, work: Path, branch: str, components: list[str], images: list[str], profile: str = "llm-agent") -> dict:
        (work / "scope.json").write_text(json.dumps({"components": components}))
        (work / "image-manifest.json").write_text(json.dumps(
            {"git_sha": HEAD, "images": {name: {"digest_ref": ref(name), "revision": HEAD} for name in images}}))
        request = self.run_cli(
            "-m", "scripts.ci.release_request", "--scope", str(work / "scope.json"),
            "--profile", profile, "--approved-by", "admin", "--triggered-by", "admin", "--branch", branch,
            "--git-sha", HEAD, "--environment", "gke-cpu", "--kube-context", "in-cluster",
            "--image-manifest", str(work / "image-manifest.json"), "--evidence-dir", str(work / "release"),
            "--output", str(work / "release-request.json"))
        self.assertEqual(request.returncode, 0, request.stderr)
        needed = self.run_cli("-m", "scripts.ci.release_policy", "--root", str(ROOT), "images",
                              "--request", str(work / "release-request.json"), "--output", str(work / "images.json"))
        self.assertEqual(needed.returncode, 0, needed.stderr)
        plan = self.run_cli("-m", "scripts.ci.release_policy", "--root", str(ROOT), "build",
                            "--request", str(work / "release-request.json"),
                            "--image-manifest", str(work / "image-manifest.json"), "--output", str(work / "plan.json"))
        self.assertEqual(plan.returncode, 0, plan.stderr)
        verify = self.run_cli("-m", "scripts.ci.release_policy", "--root", str(ROOT), "verify",
                              "--plan", str(work / "plan.json"), "--head", HEAD)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        dispatch = self.run_cli("-m", "scripts.ci.release_dispatch", "--plan", str(work / "plan.json"))
        self.assertEqual(dispatch.returncode, 0, dispatch.stdout + dispatch.stderr)
        return {"images": json.loads((work / "images.json").read_text()),
                "plan": json.loads((work / "plan.json").read_text()), "dispatch": dispatch.stdout}

    def test_rag_release_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.chain(Path(tmp), "release/rag-pipeline", ["agents", "mcp-agents", "rag-pipeline"],
                                ["rag-ingestion"])
        self.assertEqual({"units": ["rag-pipeline"], "images": ["rag-ingestion"]}, result["images"])
        self.assertIn(f"DRY RUN: rag-pipeline scripts/ci/deploy/rag_pipeline.sh --unit rag-pipeline --git-sha {HEAD}",
                      result["dispatch"])
        self.assertIn(f"--image rag-ingestion={ref('rag-ingestion')}", result["dispatch"])

    def test_coordinator_release_chain_builds_two_images_and_orders_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.chain(Path(tmp), "release/coordinator", ["agents"], ["mcp-tools", "g3-byo-coordinator"])
        self.assertEqual(["g3-byo-coordinator", "mcp-tools"], result["images"]["images"])
        lines = [line for line in result["dispatch"].splitlines() if line.startswith("DRY RUN:")]
        self.assertEqual(["mcp-servers", "agent-fleet", "coordinator"], [line.split()[2] for line in lines])
        self.assertIn("--contract jenkins/cd/mcp-servers.contract.json", lines[0])
        self.assertNotIn("deploy_atomic", result["dispatch"])

    def test_streaming_release_chain_runs_the_image_smoke_then_the_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.chain(Path(tmp), "release/streaming", ["streaming"], ["flink-block-a", "streaming-sinks"],
                                profile="platform")  # edai2-cd-streaming runs the platform Jenkinsfile
        lines = [line for line in result["dispatch"].splitlines() if line.startswith("DRY RUN:")]
        self.assertEqual(2, len(lines))
        self.assertIn("scripts.ci.bounded_cd --contract jenkins/cd/streaming.contract.json", lines[0])
        self.assertIn("scripts.ci.streaming_verify", lines[1])
        self.assertIn(f"--image streaming-sinks={ref('streaming-sinks')}", lines[1])

    def test_empty_approver_is_not_owner_approval(self) -> None:
        from scripts.ci.release_request import build_request

        body = build_request(scope={"components": ["rag-pipeline"]},
                             catalog_path=ROOT / "jenkins/catalog/components.json", profile="llm-agent",
                             approved_by=" ", branch="release/x", git_sha=HEAD, environment="gke-cpu",
                             kube_context="in-cluster", image_manifest="m", evidence_dir="e")
        self.assertFalse(body["owner_approved"])
        self.assertEqual("rehearsal", body["stage"])


if __name__ == "__main__":
    unittest.main()
