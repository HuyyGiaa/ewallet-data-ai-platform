from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops.gcp.ml2_operator_guard import run_checked, validate_preflight


class Ml2OperatorGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        subprocess.run(["git", "init", "-b", "codex/ml1-training-snapshot", self.root], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.root, "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", self.root, "config", "user.name", "Test"], check=True)
        (self.root / "tracked").write_text("ok\n")
        subprocess.run(["git", "-C", self.root, "add", "tracked"], check=True)
        subprocess.run(["git", "-C", self.root, "commit", "-m", "fixture"], check=True, capture_output=True)
        self.sha = subprocess.check_output(["git", "-C", self.root, "rev-parse", "HEAD"], text=True).strip()
        self.evidence = self.root / "evidence.json"
        self.evidence.write_text("{}\n")
        subprocess.run(["git", "-C", self.root, "add", "evidence.json"], check=True)
        subprocess.run(["git", "-C", self.root, "commit", "-m", "evidence"], check=True, capture_output=True)
        self.sha = subprocess.check_output(["git", "-C", self.root, "rev-parse", "HEAD"], text=True).strip()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.dvc = self.bin / "dvc"
        self.dvc.write_text("#!/bin/sh\necho '3.63.0'\n")
        self.dvc.chmod(0o755)
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin}:{self.old_path}"

    def tearDown(self) -> None:
        os.environ["PATH"] = self.old_path
        self.temporary.cleanup()

    def _validate(self, **overrides: object) -> None:
        values = {
            "expected_reviewed_git_sha": self.sha,
            "expected_branch": "codex/ml1-training-snapshot",
            "evidence_paths": (self.evidence,),
            "dvc_executable": "dvc",
        }
        values.update(overrides)
        validate_preflight(self.root, **values)

    def test_valid_preflight_passes(self) -> None:
        self._validate()

    def test_wrong_or_missing_reviewed_sha_and_branch_fail(self) -> None:
        for changes in (
            {"expected_reviewed_git_sha": ""},
            {"expected_reviewed_git_sha": "0" * 40},
            {"expected_branch": "wrong"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self._validate(**changes)

    def test_missing_dvc_or_evidence_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "evidence"):
            self._validate(evidence_paths=(self.root / "missing",))
        with self.assertRaisesRegex(ValueError, "DVC"):
            self._validate(dvc_executable="missing-dvc")

    def test_dirty_worktree_fails(self) -> None:
        (self.root / "tracked").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "not clean"):
            self._validate()

    def test_guarded_shell_is_fail_fast_and_requires_external_sha(self) -> None:
        script = Path("ops/gcp/ml2_operator_preflight.sh").read_text()
        self.assertIn("set -euo pipefail", script)
        self.assertIn("EXPECTED_REVIEWED_GIT_SHA:?", script)
        self.assertNotIn(
            'EXPECTED_REVIEWED_GIT_SHA="$(git rev-parse HEAD)"', script
        )

    def test_nonzero_intermediate_command_stops_the_gate(self) -> None:
        marker = self.root / "must-not-exist"
        with self.assertRaises(subprocess.CalledProcessError):
            run_checked("materialization", ["sh", "-c", "exit 17"])
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
