"""Build catalog images with Cloud Build from an exact Git SHA (stdlib only).

Jenkins has no Docker daemon. It authenticates as ``ewallet-jenkins`` through
Workload Identity, uploads ``git archive <sha>`` (tracked files of that commit
only) to the build bucket, and asks Cloud Build to run the repository
Dockerfile as ``ewallet-cloudbuild``, which may push only to the approved
Artifact Registry repository. The image manifest records the source object,
its sha256 and generation, the build ID and the pushed digest.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


METADATA_TOKEN = ("http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token")
DOCKER_BUILDER = ("gcr.io/cloud-builders/docker@sha256:"
                  "fbc05705e6f24467e40ae8a4097ab9b8fae1116551f838fa2c55f4367427fc36")
SHA = re.compile(r"^[0-9a-f]{40}$")
IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
TERMINAL = {"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"}


class BuildError(RuntimeError):
    """Cloud Build or a provenance check failed."""


@dataclass(frozen=True)
class Settings:
    project: str
    region: str
    repository: str
    bucket: str
    runner: str
    logs_bucket: str

    @property
    def registry(self) -> str:
        return f"{self.region}-docker.pkg.dev/{self.project}/{self.repository}"


@dataclass(frozen=True)
class ImageSpec:
    name: str
    dockerfile: str
    target: str | None = None


class Http:
    """Google API client. Jenkins uses the metadata server; an operator passes a token command.

    ``token_command`` (for example ``gcloud auth print-access-token``) is run again every
    ten minutes, so a multi-image build outlives a single one-hour token. Its output is
    never logged.
    """

    COMMAND_TOKEN_SECONDS = 600

    def __init__(self, opener: Callable[..., Any] = urlopen, token_command: list[str] | None = None) -> None:
        self.opener = opener
        self.token_command = token_command
        self._token: tuple[str, float] | None = None

    def token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        if self.token_command:
            value = subprocess.run(self.token_command, capture_output=True, text=True, check=True).stdout.strip()
            if not value:
                raise BuildError("the token command printed no token")
            self._token = (value, time.time() + self.COMMAND_TOKEN_SECONDS)
            return value
        request = Request(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
        with self.opener(request, timeout=10) as response:
            payload = json.loads(response.read())
        self._token = (payload["access_token"], time.time() + int(payload["expires_in"]))
        return self._token[0]

    def call(self, method: str, url: str, body: Any = None, data: bytes | None = None,
             content_type: str = "application/json", raw: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self.token()}"}
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = content_type
        try:
            with self.opener(Request(url, data=data, headers=headers, method=method), timeout=120) as response:
                payload = response.read()
        except HTTPError as error:
            raise BuildError(f"{method} {url} -> HTTP {error.code}: "
                             f"{error.read().decode(errors='replace')[:400]}") from error
        return payload if raw else (json.loads(payload) if payload else {})


def source_archive(root: Path, sha: str) -> bytes:
    if not SHA.fullmatch(sha):
        raise BuildError("release SHA must be a full lowercase SHA")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True)
    if head.stdout.strip() != sha:
        raise BuildError(f"checkout HEAD {head.stdout.strip()} is not the release SHA {sha}")
    return subprocess.run(["git", "archive", "--format=tar.gz", sha], cwd=root, capture_output=True,
                          check=True).stdout


def build_request(settings: Settings, spec: ImageSpec, sha: str, source: dict[str, Any]) -> dict[str, Any]:
    if not IMAGE_NAME.fullmatch(spec.name):
        raise BuildError(f"invalid image name {spec.name}")
    tag = f"{settings.registry}/{spec.name}:{sha}"
    args = ["build", "-f", spec.dockerfile, "--build-arg", f"EWALLET_RELEASE_SHA={sha}",
            "--label", f"org.opencontainers.image.revision={sha}", "-t", tag]
    if spec.target:
        args += ["--target", spec.target]
    return {
        "source": {"storageSource": source},
        "steps": [{"name": DOCKER_BUILDER, "args": [*args, "."], "env": ["DOCKER_BUILDKIT=1"]}],
        "images": [tag],
        "serviceAccount": f"projects/{settings.project}/serviceAccounts/{settings.runner}",
        "logsBucket": f"gs://{settings.logs_bucket}",
        "options": {"logging": "GCS_ONLY", "requestedVerifyOption": "VERIFIED"},
        "timeout": "2400s",
        "tags": ["ewallet-jenkins", spec.name, f"sha-{sha[:12]}"],
    }


def stage_source(http: Http, bucket: str, object_name: str, archive: bytes) -> str:
    """Create-only upload; an existing object is reused only if it is byte-identical.

    Jenkins may create and read objects but never overwrite or delete them, so a
    rerun of the same SHA must not replace the first upload.
    """

    try:
        uploaded = http.call(
            "POST",
            f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?"
            + urlencode({"uploadType": "media", "name": object_name, "ifGenerationMatch": "0"}),
            data=archive, content_type="application/gzip",
        )
        return str(uploaded["generation"])
    except BuildError as error:
        if "HTTP 412" not in str(error):
            raise
    existing = http.call("GET", f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
                                f"{quote(object_name, safe='')}")
    local_md5 = base64.b64encode(hashlib.md5(archive).digest()).decode()
    if existing.get("md5Hash") != local_md5:
        raise BuildError(f"gs://{bucket}/{object_name} already exists and differs from git archive")
    return str(existing["generation"])


def build_image(http: Http, settings: Settings, spec: ImageSpec, sha: str, archive: bytes,
                evidence: Path, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    object_name = f"source/{sha}/{spec.name}.tar.gz"
    source = {"bucket": settings.bucket, "object": object_name,
              "generation": stage_source(http, settings.bucket, object_name, archive)}
    base = (f"https://cloudbuild.googleapis.com/v1/projects/{settings.project}"
            f"/locations/{settings.region}/builds")
    operation = http.call("POST", base, build_request(settings, spec, sha, source))
    build_id = operation["metadata"]["build"]["id"]
    while True:
        build = http.call("GET", f"{base}/{build_id}")
        if build.get("status") in TERMINAL:
            break
        sleep(15)
    log = http.call("GET", f"https://storage.googleapis.com/storage/v1/b/{settings.logs_bucket}/o/"
                           f"{quote(f'log-{build_id}.txt', safe='')}?alt=media", raw=True)
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"cloud-build-{spec.name}.log").write_bytes(log)
    (evidence / f"cloud-build-{spec.name}.json").write_text(json.dumps(build, indent=2, sort_keys=True) + "\n")
    if build["status"] != "SUCCESS":
        raise BuildError(f"Cloud Build {build_id} for {spec.name} ended {build['status']}")
    images = build.get("results", {}).get("images", [])
    tag = f"{settings.registry}/{spec.name}:{sha}"
    digests = [item["digest"] for item in images if item.get("name") == tag]
    if len(digests) != 1 or not DIGEST.fullmatch(digests[0]):
        raise BuildError(f"Cloud Build {build_id} did not report one digest for {tag}")
    return {
        "digest_ref": f"{settings.registry}/{spec.name}@{digests[0]}",
        "build_id": build_id,
        "source_object": f"gs://{source['bucket']}/{object_name}#{source['generation']}",
        "source_sha256": hashlib.sha256(archive).hexdigest(),
        "dockerfile": spec.dockerfile,
        "target": spec.target,
    }


def release_image_specs(catalog: Any, components: list[str], profile: str, branch: str = "") -> list[ImageSpec]:
    """Images to build: selected, profile-allowed, active components whose release reads ``images/<name>``.

    ``branch`` narrows the scope exactly as the release plan does, so a release never
    builds an image it will not deploy.
    """

    allowed = set(catalog.profiles[profile]) if profile != "platform" else set(catalog.components)
    specs = []
    for name in sorted(set(catalog.release_components(components, branch)) & allowed):
        component = catalog.components[name]
        selector = component.release_artifact_selector or ""
        if component.maturity != "active" or not component.deploy_handler or not selector.startswith("images/"):
            continue
        image = selector.split("/", 1)[1]
        if image == "*":
            # images/* releases take a reviewed IMAGE_MANIFEST; release_policy fails closed without it.
            continue
        if not component.build_artifact:
            raise BuildError(f"component {name} needs a Dockerfile to build image {image}")
        specs.append(ImageSpec(image, component.build_artifact, component.build_target))
    return specs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--git-sha", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--image", action="append", help="name=dockerfile[:target]")
    selection.add_argument("--scope", type=Path, help="scope.json; build every released catalog image in it")
    parser.add_argument("--profile", default="platform")
    parser.add_argument("--release-branch", default="", help="release/<name> ref that narrows the scope")
    parser.add_argument("--catalog", type=Path, default=Path("jenkins/catalog/components.json"))
    parser.add_argument("--project", default="ewallet-data-ai-platform-hg26")
    parser.add_argument("--region", default="asia-southeast1")
    parser.add_argument("--repository", default="ewallet-platform")
    parser.add_argument("--bucket", default="ewallet-data-ai-platform-hg26-cicd-builds")
    parser.add_argument("--logs-bucket", default="ewallet-data-ai-platform-hg26-cicd-build-logs")
    parser.add_argument("--runner", default="ewallet-cloudbuild@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com")
    parser.add_argument("--token-command", default="",
                        help="operator run outside Jenkins, e.g. 'gcloud auth print-access-token'")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    settings = Settings(args.project, args.region, args.repository, args.bucket, args.runner, args.logs_bucket)
    if args.scope:
        from scripts.ci.catalog import load_catalog

        scope = json.loads(args.scope.read_text(encoding="utf-8"))
        specs = release_image_specs(
            load_catalog(args.catalog), list(scope.get("components", [])), args.profile, args.release_branch
        )
        if not specs:
            parser.error("the release scope selects no buildable deployable component")
    else:
        specs = []
        for item in args.image:
            name, _, rest = item.partition("=")
            dockerfile, _, target = rest.partition(":")
            specs.append(ImageSpec(name, dockerfile, target or None))
    for spec in specs:
        if not (args.root / spec.dockerfile).is_file():
            parser.error(f"Dockerfile does not exist: {spec.dockerfile}")
    try:
        archive = source_archive(args.root, args.git_sha)
        http = Http(token_command=shlex.split(args.token_command) or None)
        images = {spec.name: build_image(http, settings, spec, args.git_sha, archive, args.evidence_dir)
                  for spec in specs}
    except (BuildError, subprocess.CalledProcessError, URLError, OSError) as error:
        print(f"cloud_build: {error}", file=sys.stderr)
        return 1
    manifest = {"git_sha": args.git_sha, "builder": DOCKER_BUILDER, "images": images}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
