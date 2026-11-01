"""Build and verify fail-closed release plans from the component catalog."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from scripts.ci.catalog import Catalog, load_catalog


class ReleasePolicyError(ValueError):
    """A release request or plan violates the approved deployment policy."""


SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(
    r"[a-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}"
)
RELEASE_BRANCH_PATTERN = re.compile(r"release/[A-Za-z0-9._/-]+")
ALLOWED_ENVIRONMENTS = frozenset({"dev", "gcp", "gke-cpu"})


@dataclass(frozen=True)
class ReleaseRequest:
    requested: bool
    owner_approved: bool
    owner_approval: str
    branch: str
    git_sha: str
    checkout_sha: str
    environment: str
    kube_context: str
    components: tuple[str, ...]
    image_manifest_path: str
    evidence_dir: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleasePolicyError(message)


def _ordered_deployable_components(catalog: Catalog, selected: set[str]) -> list[str]:
    deployable = {
        name
        for name in selected
        if catalog.components[name].maturity == "active"
        and catalog.components[name].deploy_handler
    }
    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name not in deployable or name in ordered:
            return
        if name in visiting:
            raise ReleasePolicyError(f"deployment dependency cycle at {name}")
        visiting.add(name)
        for dependency in catalog.components[name].dependencies:
            visit(dependency)
        visiting.remove(name)
        ordered.append(name)

    for name in sorted(deployable):
        visit(name)
    return ordered


def _validate_request(catalog: Catalog, request: ReleaseRequest, manifest: dict[str, Any]) -> None:
    _require(request.requested, "deployment is disabled by default")
    _require(
        request.owner_approved and bool(request.owner_approval.strip()),
        "owner approval is required",
    )
    _require(
        RELEASE_BRANCH_PATTERN.fullmatch(request.branch) is not None,
        "release branch must match release/<name>",
    )
    _require(SHA_PATTERN.fullmatch(request.git_sha) is not None, "git SHA must be a full lowercase SHA")
    _require(
        SHA_PATTERN.fullmatch(request.checkout_sha) is not None
        and request.checkout_sha == request.git_sha,
        "checkout SHA must exactly match git SHA",
    )
    _require(request.environment in ALLOWED_ENVIRONMENTS, "environment is not approved")
    _require(bool(request.kube_context.strip()), "explicit kube context is required")
    _require(bool(request.image_manifest_path.strip()), "image manifest path is required")
    _require(bool(request.evidence_dir.strip()), "release evidence directory is required")
    unknown = set(request.components) - set(catalog.components)
    _require(not unknown, f"unknown release components: {sorted(unknown)}")
    _require(isinstance(manifest, dict), "image manifest must be an object")
    _require(manifest.get("git_sha") == request.git_sha, "image manifest git SHA must match checkout SHA")
    artifacts = manifest.get("artifacts")
    _require(isinstance(artifacts, dict), "image manifest artifacts must be an object")
    for component, digest_ref in artifacts.items():
        _require(isinstance(component, str), "image artifact component names must be strings")
        _require(
            isinstance(digest_ref, str) and DIGEST_PATTERN.fullmatch(digest_ref) is not None,
            f"image artifact {component} must use an immutable digest",
        )


def build_release_plan(
    catalog: Catalog,
    request: ReleaseRequest,
    image_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Return an executable plan only after every release gate passes."""

    _validate_request(catalog, request, image_manifest)
    ordered = _ordered_deployable_components(catalog, set(request.components))
    _require(bool(ordered), "release request has no deployable components")
    artifacts = image_manifest["artifacts"]
    units: list[dict[str, str]] = []
    for name in ordered:
        component = catalog.components[name]
        digest_ref = artifacts.get(name)
        _require(
            isinstance(digest_ref, str) and DIGEST_PATTERN.fullmatch(digest_ref) is not None,
            f"deployable component {name} has no immutable digest",
        )
        units.append(
            {
                "component": name,
                "handler": component.deploy_handler or "",
                "target": component.deployment_target or "",
                "digest_ref": digest_ref,
            }
        )
    return {
        "schema_version": 1,
        "policy_status": "PASS",
        "release_branch": request.branch,
        "approved_by": request.owner_approval,
        "git_sha": request.git_sha,
        "environment": request.environment,
        "kube_context": request.kube_context,
        "image_manifest_path": request.image_manifest_path,
        "evidence_dir": request.evidence_dir,
        "units": units,
    }


def verify_release_plan(catalog: Catalog, plan: dict[str, Any]) -> None:
    """Revalidate a serialized plan immediately before dispatch."""

    _require(isinstance(plan, dict), "release plan must be an object")
    _require(plan.get("schema_version") == 1, "unsupported release plan schema")
    _require(plan.get("policy_status") == "PASS", "release plan policy status is not PASS")
    _require(
        isinstance(plan.get("release_branch"), str)
        and RELEASE_BRANCH_PATTERN.fullmatch(plan["release_branch"]) is not None,
        "release plan has no approved release branch",
    )
    _require(
        isinstance(plan.get("approved_by"), str) and bool(plan["approved_by"].strip()),
        "release plan has no owner approval",
    )
    _require(
        isinstance(plan.get("git_sha"), str) and SHA_PATTERN.fullmatch(plan["git_sha"]) is not None,
        "release plan git SHA is invalid",
    )
    _require(plan.get("environment") in ALLOWED_ENVIRONMENTS, "release plan environment is invalid")
    _require(
        isinstance(plan.get("kube_context"), str) and bool(plan["kube_context"].strip()),
        "release plan kube context is required",
    )
    _require(
        isinstance(plan.get("image_manifest_path"), str)
        and bool(plan["image_manifest_path"].strip()),
        "release plan image manifest path is required",
    )
    _require(
        isinstance(plan.get("evidence_dir"), str) and bool(plan["evidence_dir"].strip()),
        "release plan evidence directory is required",
    )
    units = plan.get("units")
    _require(isinstance(units, list) and bool(units), "release plan has no deployable units")
    names: list[str] = []
    for unit in units:
        _require(isinstance(unit, dict), "release plan units must be objects")
        name = unit.get("component")
        _require(isinstance(name, str) and name in catalog.components, "release unit component is unknown")
        component = catalog.components[name]
        _require(component.maturity == "active", f"component {name} is extension-only")
        _require(
            bool(component.deploy_handler) and unit.get("handler") == component.deploy_handler,
            f"component {name} release handler does not match catalog",
        )
        _require(
            bool(component.deployment_target) and unit.get("target") == component.deployment_target,
            f"component {name} release target does not match catalog",
        )
        _require(
            isinstance(unit.get("digest_ref"), str)
            and DIGEST_PATTERN.fullmatch(unit["digest_ref"]) is not None,
            f"component {name} does not use an immutable digest",
        )
        names.append(name)
    expected = _ordered_deployable_components(catalog, set(names))
    _require(names == expected, "release units are not in dependency order")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleasePolicyError(f"cannot load {label} {path}: {error}") from error
    _require(isinstance(data, dict), f"{label} must be an object")
    return data


def _request_from_file(path: Path) -> ReleaseRequest:
    data = _load_object(path, "release request")
    components = data.get("components")
    _require(
        isinstance(components, list) and all(isinstance(item, str) for item in components),
        "release request components must be a list of strings",
    )
    data["components"] = tuple(components)
    try:
        return ReleaseRequest(**data)
    except TypeError as error:
        raise ReleasePolicyError(f"invalid release request fields: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("jenkins/catalog/components.json"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--request", type=Path, required=True)
    build.add_argument("--image-manifest", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    try:
        catalog = load_catalog(args.catalog)
        catalog.validate(args.root)
        if args.command == "build":
            request = _request_from_file(args.request)
            manifest = _load_object(args.image_manifest, "image manifest")
            plan = build_release_plan(catalog, request, manifest)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            plan = _load_object(args.plan, "release plan")
            verify_release_plan(catalog, plan)
    except ReleasePolicyError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
