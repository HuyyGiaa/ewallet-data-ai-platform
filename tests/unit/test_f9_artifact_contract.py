from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from ops.gcp.validate_f9_artifacts import validate_f9_artifacts


class F9ArtifactContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sha = "a" * 40
        self.image = "registry.example/f9@sha256:" + "b" * 64
        self.yaml = f"components:\n  executor:\n    image: {self.image}\n".encode()
        self.evidence = {
            "schema_version": "f9-immutable-artifact-evidence-v1",
            "status": "BUILT_AND_DIGEST_VERIFIED",
            "source_git_sha": self.sha,
            "image_uri": self.image,
            "image_digest": "sha256:" + "b" * 64,
            "compiled_yaml_sha256": hashlib.sha256(self.yaml).hexdigest(),
        }

    def test_digest_image_and_compiled_yaml_match(self) -> None:
        validate_f9_artifacts(
            self.evidence, self.yaml, expected_source_git_sha=self.sha
        )

    def test_stale_source_tag_or_mismatched_yaml_image_is_rejected(self) -> None:
        cases = []
        stale = dict(self.evidence, source_git_sha="c" * 40)
        cases.append((stale, self.yaml))
        tagged = dict(
            self.evidence,
            image_uri="registry.example/f9:mutable",
            image_digest="sha256:" + "b" * 64,
        )
        cases.append((tagged, self.yaml))
        wrong_yaml = b"components:\n  executor:\n    image: registry.example/f9@sha256:" + b"c" * 64 + b"\n"
        wrong_evidence = dict(
            self.evidence,
            compiled_yaml_sha256=hashlib.sha256(wrong_yaml).hexdigest(),
        )
        cases.append((wrong_evidence, wrong_yaml))
        for evidence, payload in cases:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                validate_f9_artifacts(
                    evidence, payload, expected_source_git_sha=self.sha
                )

    def test_source_controlled_procedure_binds_commit_and_digest(self) -> None:
        script = Path("ops/gcp/prepare_f9_immutable_artifacts.sh").read_text()
        self.assertIn("set -euo pipefail", script)
        self.assertIn('test "$(git rev-parse HEAD)" = "$EXPECTED_REVIEWED_GIT_SHA"', script)
        self.assertIn('F9_RUNTIME_IMAGE="${F9_IMAGE_REPOSITORY}@${F9_IMAGE_DIGEST}"', script)
        self.assertIn("ops.gcp.validate_f9_artifacts", script)


if __name__ == "__main__":
    unittest.main()
