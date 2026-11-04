from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.ci.provision_tools import ToolError, load_manifest, provision


ROOT = Path(__file__).resolve().parents[2]
BINARY = b"#!/bin/sh\necho fake-helm\n"


def _archive(member: str = "linux-amd64/helm", content: bytes = BINARY) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _spec(archive: bytes, content: bytes = BINARY) -> dict[str, str]:
    return {
        "version": "v3.22.0",
        "url": "https://get.helm.sh/helm-v3.22.0-linux-amd64.tar.gz",
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "member": "linux-amd64/helm",
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }


class RepositoryManifestTests(unittest.TestCase):
    def test_helm_is_pinned_to_an_exact_https_release_with_digests(self) -> None:
        tools = load_manifest(ROOT / "jenkins/ci-tools.json")
        self.assertEqual(set(tools), {"helm"})
        helm = tools["helm"]
        self.assertEqual(helm["version"], "v3.22.0")
        self.assertEqual(helm["url"], "https://get.helm.sh/helm-v3.22.0-linux-amd64.tar.gz")

    def test_pipeline_provisions_tools_before_component_ci(self) -> None:
        text = (ROOT / "jenkins/pipeline/componentPipeline.groovy").read_text(encoding="utf-8")
        provision_at = text.index("python3 -m scripts.ci.provision_tools")
        self.assertLess(provision_at, text.index("python3 -m scripts.ci.run_ci"))
        self.assertIn("--manifest jenkins/ci-tools.json", text)
        self.assertIn('export PATH="\\$tools_path:\\$PATH"', text)


class ProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.archive = _archive()
        self.fetches: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.fetches.append(url)
        return self.archive

    def test_verified_binary_is_installed_and_reused_from_cache(self) -> None:
        spec = _spec(self.archive)
        directory = provision("helm", spec, self.cache, fetch=self.fetch)
        self.assertEqual((directory / "helm").read_bytes(), BINARY)
        self.assertTrue((directory / "helm").stat().st_mode & 0o111)
        self.assertEqual(provision("helm", spec, self.cache, fetch=self.fetch), directory)
        self.assertEqual(len(self.fetches), 1)

    def test_archive_digest_mismatch_fails_closed(self) -> None:
        spec = {**_spec(self.archive), "archive_sha256": "0" * 64}
        with self.assertRaisesRegex(ToolError, "archive sha256 mismatch"):
            provision("helm", spec, self.cache, fetch=self.fetch)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_binary_digest_mismatch_fails_closed(self) -> None:
        spec = {**_spec(self.archive), "binary_sha256": "1" * 64}
        with self.assertRaisesRegex(ToolError, "extracted binary sha256 mismatch"):
            provision("helm", spec, self.cache, fetch=self.fetch)

    def test_tampered_cache_is_rejected(self) -> None:
        spec = _spec(self.archive)
        directory = provision("helm", spec, self.cache, fetch=self.fetch)
        (directory / "helm").write_bytes(b"tampered")
        with self.assertRaisesRegex(ToolError, "cached binary"):
            provision("helm", spec, self.cache, fetch=self.fetch)

    def test_missing_member_fails_closed(self) -> None:
        self.archive = _archive(member="other/helm")
        with self.assertRaisesRegex(ToolError, "no member"):
            provision("helm", _spec(self.archive), self.cache, fetch=self.fetch)


class ManifestValidationTests(unittest.TestCase):
    def write(self, spec: dict[str, str]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "ci-tools.json"
        path.write_text(json.dumps({"schema_version": 1, "tools": {"helm": spec}}), encoding="utf-8")
        return path

    def test_mutable_or_unverified_references_are_rejected(self) -> None:
        base = _spec(_archive())
        cases = {
            "latest version": {**base, "version": "latest"},
            "plain http": {**base, "url": "http://get.helm.sh/helm-v3.22.0-linux-amd64.tar.gz"},
            "url without version": {**base, "url": "https://get.helm.sh/helm-latest-linux-amd64.tar.gz"},
            "short digest": {**base, "archive_sha256": "abc"},
            "path traversal": {**base, "member": "../helm"},
        }
        for label, spec in cases.items():
            with self.subTest(label):
                with self.assertRaises(ToolError):
                    load_manifest(self.write(spec))


if __name__ == "__main__":
    unittest.main()
