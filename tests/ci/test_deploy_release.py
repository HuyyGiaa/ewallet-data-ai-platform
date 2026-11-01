from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/deploy_release.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST_REF = "registry.example/ewallet/mlflow@sha256:" + "e" * 64


class DeployDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def write_plan(self, data: dict) -> Path:
        path = self.directory / "plan.json"
        path.write_text(json.dumps(data))
        return path

    def valid_plan(self) -> dict:
        return {
            "schema_version": 1,
            "policy_status": "PASS",
            "release_branch": "release/2026-11-01",
            "approved_by": "owner@example.invalid",
            "git_sha": SHA,
            "environment": "gcp",
            "kube_context": "ctx",
            "image_manifest_path": "evidence/image-digests.json",
            "evidence_dir": "evidence/release",
            "units": [{
                "component": "mlflow",
                "handler": "scripts/mlops/deploy_mlflow.sh",
                "target": "infra/kubernetes/mlflow",
                "digest_ref": DIGEST_REF,
            }],
        }

    def run_script(self, plan: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), "--plan", str(plan), *extra],
            cwd=ROOT,
            env={**os.environ, "DEPLOY_COMMAND_LOG": str(self.directory / "external.log")},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_rejected_plan_makes_zero_external_deploy_calls(self) -> None:
        plan = self.valid_plan()
        plan["policy_status"] = "FAILED"
        result = self.run_script(self.write_plan(plan), "--execute")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.directory / "external.log").exists())

    def test_dry_run_lists_dispatch_without_calling_external_script(self) -> None:
        result = self.run_script(self.write_plan(self.valid_plan()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertIn("scripts/mlops/deploy_mlflow.sh", result.stdout)
        self.assertFalse((self.directory / "external.log").exists())


if __name__ == "__main__":
    unittest.main()
