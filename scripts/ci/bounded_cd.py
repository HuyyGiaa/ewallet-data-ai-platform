"""Run one bounded Jenkins CD release from a component contract.

This generalizes the RAG handler. Jenkins may only write to ``ewallet-cicd``, so a
release proves the built digest in that isolated namespace: it rolls the workload to
the digest, reads the digest back, runs a real smoke Job from the same image, checks
the result against the contract, then parks the workload at zero replicas. A failed
verdict restores the previous workload. Production namespaces are never named.

Promotion to production needs the separately reviewed RBAC proposal; nothing here
widens Jenkins permissions. The driver also refuses any manifest outside the contract
namespace, including a manifest edited on a release branch.

Secrets: Jenkins cannot read or write Secrets. A smoke token is generated per run,
lives only in a temporary ConfigMap of this isolated namespace, authenticates only the
throwaway workload, and is removed during cleanup. It is never logged.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import yaml

from scripts.ci.k8s_api import Client, KubeError, apply, delete, get, job_logs, wait_job, wait_ready


CD_NAMESPACE = "ewallet-cicd"
SUMMARY_PREFIX = "CD_SMOKE_SUMMARY "
DIGEST_PATTERN = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
PLACEHOLDER = re.compile(r"__[A-Z_]+__")
SMOKE_SCRIPT = Path("scripts/ci/cd_smoke.py")
TOKEN_PLACEHOLDER = "__SMOKE_TOKEN__"


class ReleaseError(RuntimeError):
    """A release failed one of its checks."""


@dataclass(frozen=True)
class Contract:
    """Everything one component's release must satisfy."""

    component: str
    namespace: str
    manifest: str
    deployments: tuple[str, ...]
    expected: Mapping[str, Any]
    smoke_job: str
    uses_token: bool
    requires_prompt_registry: bool
    ephemeral: tuple[tuple[str, str], ...] = ()

    @classmethod
    def load(cls, path: Path) -> "Contract":
        """Read and validate a contract file.

        Raises:
            ReleaseError: The contract targets another namespace or is incomplete.
        """

        value = json.loads(path.read_text(encoding="utf-8"))
        try:
            contract = cls(
                component=value["component"],
                namespace=value["namespace"],
                manifest=value["manifest"],
                deployments=tuple(value["deployments"]),
                expected=value["expected"],
                smoke_job=value["smoke_job"],
                uses_token=bool(value.get("uses_token", False)),
                requires_prompt_registry=bool(value.get("requires_prompt_registry", False)),
                ephemeral=tuple((kind, name) for kind, name in value.get("ephemeral", ())),
            )
        except KeyError as error:
            raise ReleaseError(f"contract {path} is missing {error}") from error
        if contract.namespace != CD_NAMESPACE:
            raise ReleaseError(f"contract {path} targets {contract.namespace}, not {CD_NAMESPACE}")
        return contract


class Kube(Protocol):
    """The Kubernetes operations a release needs; tests replace it."""

    def apply_documents(self, documents: Sequence[Mapping[str, Any]]) -> list[str]:
        """Create or update documents."""

    def read(self, kind: str, name: str) -> dict[str, Any] | None:
        """Return an object, or ``None`` when it does not exist."""

    def remove(self, kind: str, name: str) -> None:
        """Delete an object if present."""

    def wait_deployment(self, name: str, timeout: float) -> None:
        """Wait until a Deployment is available."""

    def wait_for_job(self, name: str, timeout: float) -> bool:
        """Wait for a Job and return whether it succeeded."""

    def logs(self, job: str) -> str:
        """Return a Job's pod logs."""


class ApiKube:
    """:class:`Kube` over the namespace-scoped CD client."""

    def __init__(self, client: Client, namespace: str) -> None:
        self.client = client
        self.namespace = namespace

    def apply_documents(self, documents: Sequence[Mapping[str, Any]]) -> list[str]:
        """Create or update documents."""

        return apply(self.client, [dict(item) for item in documents])

    def read(self, kind: str, name: str) -> dict[str, Any] | None:
        """Return an object, or ``None`` when it does not exist."""

        try:
            return get(self.client, kind, self.namespace, name)
        except KubeError as error:
            if "HTTP 404" in str(error):
                return None
            raise

    def remove(self, kind: str, name: str) -> None:
        """Delete an object if present."""

        delete(self.client, kind, self.namespace, name)

    def wait_deployment(self, name: str, timeout: float) -> None:
        """Wait until a Deployment is available."""

        wait_ready(self.client, "Deployment", self.namespace, name, timeout)

    def wait_for_job(self, name: str, timeout: float) -> bool:
        """Wait for a Job and return whether it succeeded."""

        try:
            status = wait_job(self.client, self.namespace, name, timeout)
        except KubeError:
            return False
        return bool(status.get("succeeded"))

    def logs(self, job: str) -> str:
        """Return a Job's pod logs."""

        return job_logs(self.client, self.namespace, job)


def render(template: str, *, image: str, git_sha: str, token: str, smoke_source: str) -> list[dict[str, Any]]:
    """Fill a manifest template and parse it.

    Args:
        template: Manifest text with ``__IMAGE__``-style placeholders.
        image: Released digest reference.
        git_sha: Reviewed release commit.
        token: Per-run smoke token, or empty when unused.
        smoke_source: Text of the smoke script for its ConfigMap.

    Returns:
        The parsed documents.

    Raises:
        ReleaseError: A placeholder stayed unresolved.
    """

    text = (
        template.replace("__IMAGE_DIGEST__", image.split("@", 1)[-1])
        .replace("__IMAGE__", image)
        .replace("__RELEASE_SHA__", git_sha)
        .replace("__RELEASE_SHORT__", git_sha[:12])
        .replace(TOKEN_PLACEHOLDER, token)
    )
    leftover = sorted(set(PLACEHOLDER.findall(text)))
    if leftover:
        raise ReleaseError(f"manifest has unresolved placeholders: {leftover}")
    documents = [item for item in yaml.safe_load_all(text) if item]
    for item in documents:
        if item["kind"] == "ConfigMap" and "cd_smoke.py" in item.get("data", {}):
            item["data"]["cd_smoke.py"] = smoke_source
    return documents


def require_namespace(documents: Sequence[Mapping[str, Any]], namespace: str) -> None:
    """Refuse any document outside the contract namespace."""

    for item in documents:
        found = item.get("metadata", {}).get("namespace")
        if found != namespace:
            raise ReleaseError(f"{item.get('kind')} {item['metadata'].get('name')} is in {found}, not {namespace}")


def deployment_image(item: Mapping[str, Any]) -> str:
    """Return the first container image of a Deployment."""

    return str(item["spec"]["template"]["spec"]["containers"][0]["image"])


def parse_summary(log: str) -> dict[str, Any]:
    """Return the last smoke summary in a Job log.

    Raises:
        ReleaseError: The log has no parsable summary.
    """

    lines = [line[line.index(SUMMARY_PREFIX) + len(SUMMARY_PREFIX):] for line in log.splitlines() if SUMMARY_PREFIX in line]
    if not lines:
        raise ReleaseError("smoke log has no summary")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise ReleaseError("smoke summary is not JSON") from error


def verify_summary(summary: Mapping[str, Any], expected: Mapping[str, Any], git_sha: str) -> list[str]:
    """Return every way a summary breaks the contract."""

    failures = []
    if summary.get("release_sha") != git_sha:
        failures.append(f"release_sha is {summary.get('release_sha')!r}, expected {git_sha}")
    for key, value in expected.items():
        if summary.get(key) != value:
            failures.append(f"{key} is {summary.get(key)!r}, expected {value!r}")
    return failures


def _restorable(previous: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only what is needed to re-apply a previous Deployment."""

    metadata = previous["metadata"]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": metadata["name"], "namespace": metadata["namespace"], "labels": metadata.get("labels", {})},
        "spec": {**previous["spec"], "replicas": 0},
    }


def _parked(document: Mapping[str, Any]) -> dict[str, Any]:
    return {**document, "spec": {**document["spec"], "replicas": 0}}


def run_release(
    contract: Contract,
    kube: Kube,
    documents: Sequence[Mapping[str, Any]],
    *,
    image: str,
    git_sha: str,
    evidence: Path,
    prompt_registry_ok: bool | None = None,
    ready_timeout: float = 300.0,
    job_timeout: float = 600.0,
) -> dict[str, Any]:
    """Roll out, verify, and park or roll back one component.

    Args:
        contract: Validated contract.
        kube: Kubernetes boundary.
        documents: Rendered manifests.
        image: Released digest.
        git_sha: Reviewed commit.
        evidence: Directory for the verdict.
        prompt_registry_ok: Registry check result; ``None`` means it did not run.
        ready_timeout: Seconds to wait for each Deployment.
        job_timeout: Seconds to wait for the smoke Job.

    Returns:
        The verdict document, also written to ``verdict.json``.

    Raises:
        ReleaseError: Any check failed; the previous workload is already restored.
    """

    require_namespace(documents, contract.namespace)
    failures: list[str] = []
    if contract.requires_prompt_registry and prompt_registry_ok is not True:
        failures.append("prompt registry was not verified")
    previous = {name: kube.read("Deployment", name) for name in contract.deployments}
    previous_images = {name: deployment_image(item) if item else None for name, item in previous.items()}
    transient = [(item["kind"], item["metadata"]["name"]) for item in documents if item["kind"] in {"Job", "ConfigMap"}]
    transient += contract.ephemeral
    summary: dict[str, Any] = {}
    try:
        if not failures:
            kube.apply_documents(documents)
            for kind, name in contract.ephemeral:
                if kind == "Deployment":
                    kube.wait_deployment(name, ready_timeout)
            for name in contract.deployments:
                kube.wait_deployment(name, ready_timeout)
                deployed = kube.read("Deployment", name)
                if deployed is None or deployment_image(deployed) != image:
                    failures.append(f"Deployment {name} does not run {image}")
            job_passed = kube.wait_for_job(contract.smoke_job, job_timeout)
            log = kube.logs(contract.smoke_job)
            (evidence / "smoke.log").write_text(log, encoding="utf-8")
            if not job_passed:
                failures.append("smoke Job failed")
            summary = parse_summary(log)
            failures += verify_summary(summary, contract.expected, git_sha)
    except (ReleaseError, KubeError) as error:
        failures.append(str(error))
    finally:
        for kind, name in transient:
            kube.remove(kind, name)
    verdict = {
        "component": contract.component,
        "verdict": "FAIL" if failures else "PASS",
        "failures": failures,
        "release_sha": git_sha,
        "image": image,
        "previous_images": previous_images,
        "prompt_registry_verified": prompt_registry_ok,
        "smoke": summary,
    }
    if failures:
        for name, item in previous.items():
            if item:
                kube.apply_documents([_restorable(item)])
            else:
                kube.remove("Deployment", name)
        verdict["rollback"] = "restored previous workloads" if any(previous.values()) else "removed first deployment"
    else:
        kube.apply_documents([
            _parked(item) for item in documents
            if item["kind"] == "Deployment" and item["metadata"]["name"] in contract.deployments
        ])
    (evidence / "verdict.json").write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if failures:
        raise ReleaseError(f"{contract.component} release failed: {failures}")
    return verdict


def verify_prompt_registry(registry: str, root: Path) -> bool:
    """Run the existing registry check; a missing URL counts as not verified."""

    if not registry:
        return False
    result = subprocess.run(
        ["uv", "run", "--locked", "--project", "infra/docker/llm-agent-tooling", "python", "-m",
         "ops.llm_agent.prompt_registry", "verify-all", "--registry", registry],
        cwd=root, check=False, capture_output=True, text=True,
    )
    return result.returncode == 0


def main(argv: Sequence[str] | None = None) -> int:
    """Validate arguments and run a release from Jenkins."""

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--api", help="local API proxy, for dry runs")
    parser.add_argument("--prompt-registry", default="")
    args = parser.parse_args(argv)
    if not DIGEST_PATTERN.fullmatch(args.image):
        parser.error("image must be a digest reference")
    if not SHA_PATTERN.fullmatch(args.git_sha):
        parser.error("git SHA must be a full SHA")
    root = Path(__file__).resolve().parents[2]
    contract = Contract.load(root / args.contract if not args.contract.is_absolute() else args.contract)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32) if contract.uses_token else ""
    documents = render(
        (root / contract.manifest).read_text(encoding="utf-8"),
        image=args.image, git_sha=args.git_sha, token=token,
        smoke_source=(root / SMOKE_SCRIPT).read_text(encoding="utf-8"),
    )
    prompt_ok = verify_prompt_registry(args.prompt_registry, root) if contract.requires_prompt_registry else None
    kube = ApiKube(Client.from_environment(args.api), contract.namespace)
    try:
        verdict = run_release(contract, kube, documents, image=args.image, git_sha=args.git_sha,
                              evidence=args.evidence_dir, prompt_registry_ok=prompt_ok)
    except ReleaseError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({key: verdict[key] for key in ("component", "verdict", "image", "previous_images")}, sort_keys=True))
    return 0


__all__ = ["Contract", "ReleaseError", "main", "parse_summary", "render",
           "require_namespace", "run_release", "verify_summary"]


if __name__ == "__main__":
    raise SystemExit(main())
