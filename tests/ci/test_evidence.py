from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.ci.evidence import EvidenceWriter, classify_failure


class FailureClassificationTests(unittest.TestCase):
    def test_classifies_missing_security_infrastructure_and_source_failures(self) -> None:
        self.assertEqual(classify_failure("uv-sync", 127, "missing"), "MISSING_RUNTIME_DEPENDENCY")
        self.assertEqual(classify_failure("security-scan", 1, "available"), "SECURITY_FAILURE")
        self.assertEqual(classify_failure("deploy-preflight", 1, "available"), "INFRASTRUCTURE_FAILURE")
        self.assertEqual(classify_failure("unit-tests", 1, "available"), "SOURCE_REGRESSION")


class EvidenceWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_failure_manifest_and_checksum_exist_after_failed_command(self) -> None:
        writer = EvidenceWriter(self.root / "run", {"git_sha": "a" * 40, "trigger": "test"})
        writer.write_text("logs/0001-unit-tests.log", "assertion failed\n")
        writer.finalize(
            status="FAILED",
            results=[{"stage": "unit-tests", "exit_code": 1, "classification": "SOURCE_REGRESSION"}],
        )
        manifest = json.loads((writer.directory / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "FAILED")
        self.assertEqual(manifest["results"][0]["classification"], "SOURCE_REGRESSION")
        checksums = (writer.directory / "SHA256SUMS").read_text()
        self.assertIn("manifest.json", checksums)
        self.assertIn("logs/0001-unit-tests.log", checksums)

    def test_checksum_manifest_is_sorted_and_hand_verified(self) -> None:
        writer = EvidenceWriter(self.root / "run", {"git_sha": "b" * 40})
        writer.write_text("z.txt", "last\n")
        writer.write_text("a.txt", "first\n")
        writer.finalize(status="PASS", results=[])
        lines = (writer.directory / "SHA256SUMS").read_text().splitlines()
        paths = [line.split("  ", 1)[1] for line in lines]
        self.assertEqual(paths, sorted(paths))
        expected = hashlib.sha256((writer.directory / "a.txt").read_bytes()).hexdigest()
        self.assertIn(f"{expected}  a.txt", lines)

    def test_lock_hashes_use_real_selected_lockfiles(self) -> None:
        project = self.root / "project"
        project.mkdir()
        (project / "uv.lock").write_text("version = 1\n")
        writer = EvidenceWriter(self.root / "run", {})
        hashes = writer.write_lock_hashes(self.root, ["project"])
        self.assertEqual(hashes["project"], hashlib.sha256(b"version = 1\n").hexdigest())


if __name__ == "__main__":
    unittest.main()
