from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest

from urllib.error import HTTPError

from scripts.ci.catalog import load_catalog
from scripts.ci.cloud_build import (
    DOCKER_BUILDER, LABEL_BUILDER, LABEL_DOCKERFILE, LABEL_REVISION, LABEL_TARGET, BuildError, Http, ImageSpec,
    Registry, RegistryImage, Settings, build_image, build_or_reuse, build_request, expected_labels, image_specs,
    reuse_existing, source_archive,
)


CATALOG = load_catalog(Path(__file__).resolve().parents[2] / "jenkins/catalog/components.json")


SHA = "e" * 40
SETTINGS = Settings("proj-1", "asia-southeast1", "ewallet-platform", "proj-1-cicd-builds",
                    "ewallet-cloudbuild@proj-1.iam.gserviceaccount.com", "proj-1-cicd-build-logs")
TAG = f"asia-southeast1-docker.pkg.dev/proj-1/ewallet-platform/rag-ingestion:{SHA}"


class Response(io.BytesIO):
    def __init__(self, data: bytes = b"", headers: dict | None = None) -> None:
        super().__init__(data)
        self.status, self.headers = 200, dict(headers or {})

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return False


class FakeGoogle:
    def __init__(self, status: str = "SUCCESS", digest: str = "sha256:" + "1" * 64,
                 existing_md5: str | None = None) -> None:
        self.status, self.digest, self.existing_md5 = status, digest, existing_md5
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, request, timeout):  # type: ignore[no-untyped-def]
        url, method = request.full_url, request.get_method()
        body = request.data
        self.calls.append((method, url, body))
        if "computeMetadata" in url:
            assert request.get_header("Metadata-flavor") == "Google"
            return Response(json.dumps({"access_token": "tok", "expires_in": 3600}).encode())
        assert request.get_header("Authorization") == "Bearer tok"
        if "upload/storage" in url:
            assert "ifGenerationMatch=0" in url, url
            if self.existing_md5 is not None:
                raise HTTPError(url, 412, "precondition", {}, io.BytesIO(b"{}"))
            return Response(b'{"generation": "77"}')
        if "/o/source%2F" in url and "alt=media" not in url:
            return Response(json.dumps({"generation": "55", "md5Hash": self.existing_md5}).encode())
        if method == "POST" and url.endswith("/builds"):
            return Response(json.dumps({"metadata": {"build": {"id": "b-1"}}}).encode())
        if url.endswith("/builds/b-1"):
            return Response(json.dumps({"status": self.status, "results": {
                "images": [{"name": TAG, "digest": self.digest}]}}).encode())
        if "alt=media" in url:
            assert "/b/proj-1-cicd-build-logs/o/log-b-1.txt" in url, url
            return Response(b"STEP 1: docker build\nDONE\n")
        raise AssertionError(url)


class CloudBuildTests(unittest.TestCase):
    def test_request_uses_runner_identity_pinned_builder_gcs_logs_and_release_sha(self) -> None:
        request = build_request(SETTINGS, ImageSpec("rag-ingestion", "infra/docker/rag-ingestion/Dockerfile"),
                                SHA, {"bucket": "b", "object": "o", "generation": "1"})
        self.assertEqual(request["serviceAccount"],
                         "projects/proj-1/serviceAccounts/ewallet-cloudbuild@proj-1.iam.gserviceaccount.com")
        self.assertEqual(request["options"]["logging"], "GCS_ONLY")
        self.assertEqual(request["logsBucket"], "gs://proj-1-cicd-build-logs")
        self.assertEqual(request["steps"][0]["name"], DOCKER_BUILDER)
        self.assertIn(f"EWALLET_RELEASE_SHA={SHA}", request["steps"][0]["args"])
        args = request["steps"][0]["args"]
        for label in (f"{LABEL_REVISION}={SHA}", f"{LABEL_DOCKERFILE}=infra/docker/rag-ingestion/Dockerfile",
                      f"{LABEL_TARGET}=", f"{LABEL_BUILDER}={DOCKER_BUILDER}"):
            self.assertIn(label, args)
        self.assertEqual(request["images"], [TAG])

    def test_successful_build_returns_digest_and_source_provenance(self) -> None:
        google = FakeGoogle()
        with tempfile.TemporaryDirectory() as tmp:
            result = build_image(Http(google), SETTINGS, ImageSpec("rag-ingestion", "Dockerfile"), SHA,
                                 b"archive", Path(tmp), sleep=lambda _: None)
            self.assertTrue((Path(tmp) / "cloud-build-rag-ingestion.log").is_file())
        self.assertEqual(result["digest_ref"],
                         "asia-southeast1-docker.pkg.dev/proj-1/ewallet-platform/rag-ingestion@sha256:" + "1" * 64)
        self.assertEqual(result["build_id"], "b-1")
        self.assertEqual(result["source_object"], f"gs://proj-1-cicd-builds/source/{SHA}/rag-ingestion.tar.gz#77")

    def test_existing_identical_source_object_is_reused_without_overwrite(self) -> None:
        md5 = base64.b64encode(hashlib.md5(b"archive").digest()).decode()
        with tempfile.TemporaryDirectory() as tmp:
            result = build_image(Http(FakeGoogle(existing_md5=md5)), SETTINGS,
                                 ImageSpec("rag-ingestion", "Dockerfile"), SHA, b"archive", Path(tmp),
                                 sleep=lambda _: None)
        self.assertTrue(result["source_object"].endswith("#55"))

    def test_existing_different_source_object_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(BuildError, "differs"):
            build_image(Http(FakeGoogle(existing_md5="AAAA")), SETTINGS, ImageSpec("rag-ingestion", "Dockerfile"),
                        SHA, b"archive", Path(tmp), sleep=lambda _: None)

    def test_failed_build_or_missing_digest_fails_closed(self) -> None:
        for google in (FakeGoogle(status="FAILURE"), FakeGoogle(digest="latest")):
            with self.subTest(status=google.status), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(BuildError):
                    build_image(Http(google), SETTINGS, ImageSpec("rag-ingestion", "Dockerfile"), SHA,
                                b"archive", Path(tmp), sleep=lambda _: None)

    def test_image_specs_come_from_the_catalog_for_exactly_the_planned_images(self) -> None:
        self.assertEqual(image_specs(CATALOG, ["rag-ingestion"]),
                         [ImageSpec("rag-ingestion", "infra/docker/rag-ingestion/Dockerfile")])
        self.assertEqual(image_specs(CATALOG, ["mcp-tools", "g3-byo-coordinator"]),
                         [ImageSpec("g3-byo-coordinator", "infra/docker/g3-byo-coordinator/Dockerfile"),
                          ImageSpec("mcp-tools", "infra/docker/llm-agent-tooling/Dockerfile", "mcp-server")])
        self.assertEqual([spec.name for spec in image_specs(CATALOG, ["streaming-sinks", "flink-block-a"])],
                         ["flink-block-a", "streaming-sinks"])
        with self.assertRaisesRegex(BuildError, "no catalog component builds"):
            image_specs(CATALOG, ["g3-byo-feature-rag-specialist"])

    def test_operator_token_command_replaces_the_metadata_server(self) -> None:
        opened = []
        http = Http(opener=lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(AssertionError("metadata")),
                    token_command=[sys.executable, "-c", "print('operator-token')"])
        self.assertEqual(http.token(), "operator-token")
        self.assertEqual(http.token(), "operator-token")
        self.assertEqual(opened, [])

    def test_empty_operator_token_fails_closed(self) -> None:
        http = Http(token_command=[sys.executable, "-c", "print('')"])
        with self.assertRaises(BuildError):
            http.token()

    def test_archive_refuses_a_checkout_that_is_not_the_release_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = lambda *a: subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True)  # noqa: E731
            run("init", "-q")
            run("-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "--allow-empty", "-m", "x")
            with self.assertRaisesRegex(BuildError, "not the release SHA"):
                source_archive(Path(tmp), "f" * 40)


SPEC = ImageSpec("mcp-tools", "infra/docker/llm-agent-tooling/Dockerfile", "mcp-server")
DIGEST = "sha256:" + "2" * 64


def test_an_image_less_release_writes_an_empty_manifest_without_calling_google(tmp_path) -> None:
    from scripts.ci import cloud_build

    root = Path(__file__).resolve().parents[2]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    images = tmp_path / "images.json"
    images.write_text(json.dumps({"units": ["prompt-registry-gate"], "images": []}))
    output = tmp_path / "manifest.json"
    code = cloud_build.main(["--root", str(root), "--git-sha", head, "--images-file", str(images),
                             "--evidence-dir", str(tmp_path / "ev"), "--output", str(output)])
    assert code == 0
    assert json.loads(output.read_text())["images"] == {}


class StubRegistry:
    def __init__(self, *answers: RegistryImage | None) -> None:
        self.answers = list(answers)
        self.lookups: list[tuple[str, str]] = []

    def lookup(self, name: str, tag: str) -> RegistryImage | None:
        self.lookups.append((name, tag))
        return self.answers.pop(0)


class ArtifactReuseTests(unittest.TestCase):
    def test_an_existing_image_with_matching_provenance_is_reused_without_building(self) -> None:
        registry = StubRegistry(RegistryImage(DIGEST, expected_labels(SPEC, SHA)))
        google = FakeGoogle()
        with tempfile.TemporaryDirectory() as tmp:
            result = build_or_reuse(Http(google), registry, SETTINGS, SPEC, SHA, b"archive", Path(tmp))
        self.assertTrue(result["reused"])
        self.assertEqual(result["digest_ref"], f"asia-southeast1-docker.pkg.dev/proj-1/ewallet-platform/mcp-tools@{DIGEST}")
        self.assertEqual("VERIFIED", result["registry_readback"])
        self.assertFalse([call for call in google.calls if "cloudbuild" in call[1]], "nothing was built")

    def test_conflicting_or_missing_provenance_is_refused(self) -> None:
        cases = (
            ({**expected_labels(SPEC, SHA), LABEL_REVISION: "f" * 40}, "built from"),
            ({**expected_labels(SPEC, SHA), LABEL_TARGET: "runtime"}, "conflicting provenance"),
            ({LABEL_REVISION: SHA}, "without build provenance labels"),
        )
        for labels, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(BuildError, message):
                reuse_existing(RegistryImage(DIGEST, labels), SETTINGS, SPEC, SHA)

    def test_a_new_build_is_read_back_and_must_match(self) -> None:
        reported = "sha256:" + "1" * 64
        with tempfile.TemporaryDirectory() as tmp:
            ok = build_or_reuse(Http(FakeGoogle()), StubRegistry(None, RegistryImage(reported, {})), SETTINGS,
                                ImageSpec("rag-ingestion", "Dockerfile"), SHA, b"archive", Path(tmp), sleep=lambda _: None)
            self.assertFalse(ok["reused"])
            self.assertEqual("VERIFIED", ok["registry_readback"])
            with self.assertRaisesRegex(BuildError, "does not resolve"):
                build_or_reuse(Http(FakeGoogle()), StubRegistry(None, RegistryImage(DIGEST, {})), SETTINGS,
                               ImageSpec("rag-ingestion", "Dockerfile"), SHA, b"archive", Path(tmp), sleep=lambda _: None)

    def test_lookup_off_builds_and_records_the_skipped_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build_or_reuse(Http(FakeGoogle()), None, SETTINGS, ImageSpec("rag-ingestion", "Dockerfile"), SHA,
                                    b"archive", Path(tmp), sleep=lambda _: None)
        self.assertEqual("SKIPPED_BY_PARAMETER", result["registry_readback"])


class FakeRegistryHttp:
    """Docker Registry v2 responses; the token is never sent to the blob storage redirect."""

    def __init__(self, status: int = 200, index: bool = False) -> None:
        self.status, self.index = status, index
        self.urls: list[str] = []

    def opener(self, request, timeout):  # type: ignore[no-untyped-def]
        url = request.full_url
        self.urls.append(url)
        assert request.get_header("Authorization", "").startswith("Basic "), url
        if self.status != 200:
            raise HTTPError(url, self.status, "x", {}, io.BytesIO(b""))
        if "/blobs/" in url:
            raise HTTPError(url, 307, "redirect", {"Location": "https://storage.example/blob"}, io.BytesIO(b""))
        if url.endswith(f"/manifests/{SHA}") and self.index:
            body = {"manifests": [{"digest": "sha256:" + "3" * 64, "platform": {"os": "linux", "architecture": "amd64"}}]}
            return Response(json.dumps(body).encode(), headers={"Docker-Content-Digest": DIGEST})
        return Response(json.dumps({"config": {"digest": "sha256:" + "4" * 64}}).encode(),
                        headers={"Docker-Content-Digest": DIGEST})

    def blob(self, request, timeout):  # type: ignore[no-untyped-def]
        assert request.get_header("Authorization") is None
        return Response(json.dumps({"config": {"Labels": {LABEL_REVISION: SHA}}}).encode())


class RegistryClientTests(unittest.TestCase):
    def registry(self, fake: FakeRegistryHttp) -> Registry:
        http = Http(token_command=[sys.executable, "-c", "print('tok')"])
        return Registry(http, SETTINGS, opener=fake.opener, blob_opener=fake.blob)

    def test_reads_digest_and_labels_through_an_index(self) -> None:
        fake = FakeRegistryHttp(index=True)
        image = self.registry(fake).lookup("mcp-tools", SHA)
        self.assertEqual(DIGEST, image.digest)
        self.assertEqual({LABEL_REVISION: SHA}, image.labels)
        self.assertTrue(fake.urls[0].startswith("https://asia-southeast1-docker.pkg.dev/v2/proj-1/ewallet-platform/mcp-tools/"))

    def test_absent_tag_is_none_and_a_permission_error_fails_closed(self) -> None:
        self.assertIsNone(self.registry(FakeRegistryHttp(status=404)).lookup("mcp-tools", SHA))
        with self.assertRaisesRegex(BuildError, "artifactregistry.reader"):
            self.registry(FakeRegistryHttp(status=403)).lookup("mcp-tools", SHA)


if __name__ == "__main__":
    unittest.main()
