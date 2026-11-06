from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from urllib.error import HTTPError

from scripts.ci.catalog import load_catalog
from scripts.ci.cloud_build import (
    DOCKER_BUILDER, BuildError, Http, ImageSpec, Settings, build_image, build_request, release_image_specs,
    source_archive,
)


CATALOG = load_catalog(Path(__file__).resolve().parents[2] / "jenkins/catalog/components.json")


SHA = "e" * 40
SETTINGS = Settings("proj-1", "asia-southeast1", "ewallet-platform", "proj-1-cicd-builds",
                    "ewallet-cloudbuild@proj-1.iam.gserviceaccount.com", "proj-1-cicd-build-logs")
TAG = f"asia-southeast1-docker.pkg.dev/proj-1/ewallet-platform/rag-ingestion:{SHA}"


class Response(io.BytesIO):
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

    def test_release_specs_come_only_from_selected_deployable_catalog_components(self) -> None:
        scope = ["rag-pipeline", "mcp-agents", "agents", "fraud-api"]
        specs = release_image_specs(CATALOG, scope, "llm-agent", "release/rag-pipeline")
        self.assertEqual(specs, [ImageSpec("rag-ingestion", "infra/docker/rag-ingestion/Dockerfile")])
        self.assertEqual(release_image_specs(CATALOG, scope, "llm-agent", "release/mcp-agents"),
                         [ImageSpec("mcp-tools", "infra/docker/llm-agent-tooling/Dockerfile", "mcp-server")])
        self.assertEqual(release_image_specs(CATALOG, scope, "llm-agent", "release/agents"),
                         [ImageSpec("g3-byo-coordinator", "infra/docker/g3-byo-coordinator/Dockerfile")])
        self.assertEqual(release_image_specs(CATALOG, ["fraud-api"], "llm-agent"), [])
        self.assertEqual(release_image_specs(CATALOG, ["llm-serving"], "llm-agent"), [])

    def test_archive_refuses_a_checkout_that_is_not_the_release_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = lambda *a: subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True)  # noqa: E731
            run("init", "-q")
            run("-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "--allow-empty", "-m", "x")
            with self.assertRaisesRegex(BuildError, "not the release SHA"):
                source_archive(Path(tmp), "f" * 40)


if __name__ == "__main__":
    unittest.main()
