"""Build and verify fail-closed release plans from typed release units.

A plan is built once from the release request (``scripts/ci/release_request.py``) and the image
manifest (``scripts/ci/cloud_build.py``), then re-verified immediately before dispatch against the
catalog, the release units, the checkout and the manifest bytes. Every gate fails closed:

* the release branch must name a release group (``release/<group>``); an unknown branch no longer
  "keeps the whole scope", so a change-detection scope can never select a deployment by itself;
* at least one CI component of the group must have changed;
* production needs an explicit production stage, an approver distinct from the person who
  triggered the build, a declared production identity, and no blocked unit;
* every consumed image must be pinned by digest, built from the release SHA and present in the
  manifest; the manifest's SHA-256 is recorded so a stale or edited manifest is refused;
* units are ordered by their declared dependencies, and a dependency outside the release is an
  error rather than an implicit redeploy;
* plans of the previous schema (component ``handler`` dispatch) are refused outright.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from scripts.ci.catalog import Catalog, CatalogError, load_catalog
from scripts.ci.release_units import ReleaseUnitError, ReleaseUnits, load_release_units


class ReleasePolicyError(ValueError):
    """A release request or plan violates the approved deployment policy."""


SCHEMA_VERSION = 2
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}")
RELEASE_BRANCH_PATTERN = re.compile(r"release/[A-Za-z0-9._/-]+")
ALLOWED_ENVIRONMENTS = frozenset({"dev", "gcp", "gke-cpu"})
STAGES = frozenset({"rehearsal", "production"})
# A plan naming one of these is stale or hand-edited: they were reachable only through the
# removed component-handler dispatch (P0 finding CICD-P0-F01).
FORBIDDEN_HANDLERS = ("scripts/llm_agent/deploy_atomic.sh", "scripts/mlops/deploy_kfp.sh",
                      "scripts/mlops/deploy_mlflow.sh")


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
    stage: str = "rehearsal"
    triggered_by: str = ""
    production_identity: str = ""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleasePolicyError(message)


def manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_images(manifest: dict[str, Any], git_sha: str) -> dict[str, str]:
    """Image name -> digest reference, after checking every entry's identity.

    An empty ``images`` object is valid (a release of image-less units, such as the Prompt Management
    gate); a unit that needs an image still fails on the missing digest.
    """

    _require(isinstance(manifest, dict), "image manifest must be an object")
    _require(manifest.get("git_sha") == git_sha, "image manifest git SHA must match the release SHA")
    images = manifest.get("images", {})
    _require(isinstance(images, dict), "image manifest images must be an object")
    result: dict[str, str] = {}
    for name, image in images.items():
        _require(isinstance(name, str) and isinstance(image, dict), "image entries must be named objects")
        reference = image.get("digest_ref")
        _require(isinstance(reference, str) and DIGEST_PATTERN.fullmatch(reference) is not None,
                 f"image {name} must use an immutable digest")
        revision = image.get("revision", git_sha)
        _require(revision == git_sha, f"image {name} was built from {revision}, not the release SHA")
        result[name] = reference
    return result


def _validate_request(request: ReleaseRequest) -> None:
    _require(request.requested, "deployment is disabled by default")
    _require(request.owner_approved and bool(request.owner_approval.strip()), "owner approval is required")
    _require(RELEASE_BRANCH_PATTERN.fullmatch(request.branch) is not None, "release branch must match release/<name>")
    _require(SHA_PATTERN.fullmatch(request.git_sha) is not None, "git SHA must be a full lowercase SHA")
    _require(SHA_PATTERN.fullmatch(request.checkout_sha) is not None and request.checkout_sha == request.git_sha,
             "checkout SHA must exactly match git SHA")
    _require(request.environment in ALLOWED_ENVIRONMENTS, "environment is not approved")
    _require(request.stage in STAGES, "release stage must be rehearsal or production")
    _require(bool(request.kube_context.strip()), "explicit kube context is required")
    _require(bool(request.image_manifest_path.strip()), "image manifest path is required")
    _require(bool(request.evidence_dir.strip()), "release evidence directory is required")
    if request.stage == "production":
        _require(bool(request.triggered_by.strip()), "production needs the identity that triggered the build")
        _require(request.owner_approval.strip() != request.triggered_by.strip(),
                 "production approval must come from someone other than the person who triggered the build")
        _require(bool(request.production_identity.strip()),
                 "production needs an explicit deployer identity (owner decision D1); Jenkins has none by default")


def select_units(units: ReleaseUnits, request: ReleaseRequest, catalog: Catalog) -> list[str]:
    """Ordered unit names a valid request deploys (no manifest needed: drives the image build)."""

    _validate_request(request)
    unknown = set(request.components) - set(catalog.components)
    _require(not unknown, f"unknown release components: {sorted(unknown)}")
    try:
        group = units.group(request.branch)
        names = units.ordered(group.units_for(request.stage))
    except ReleaseUnitError as error:
        raise ReleasePolicyError(str(error)) from error
    _require(bool(names), f"release group {request.branch} has no {request.stage} units")
    owned = {component for name in names for component in units.units[name].components}
    _require(bool(owned & set(request.components)),
             f"release group {request.branch} changes none of its components {sorted(owned)} in this scope")
    for name in names:
        unit = units.units[name]
        _require(unit.automation == "enabled", f"release unit {name} is not automated: {unit.blocked_reason}")
    return names


def required_images(units: ReleaseUnits, names: list[str]) -> list[str]:
    return sorted({image for name in names for image in units.units[name].images})


def build_release_plan(catalog: Catalog, units: ReleaseUnits, request: ReleaseRequest,
                       image_manifest: dict[str, Any], manifest_digest: str) -> dict[str, Any]:
    """Return an executable plan only after every release gate passes."""

    names = select_units(units, request, catalog)
    available = manifest_images(image_manifest, request.git_sha)
    planned = []
    for name in names:
        unit = units.units[name]
        missing = [image for image in unit.images if image not in available]
        _require(not missing, f"release unit {name} has no immutable digest for {missing}")
        planned.append({**unit.to_plan(), "images": {image: available[image] for image in unit.images}})
    _require(re.fullmatch(r"[0-9a-f]{64}", manifest_digest) is not None, "image manifest checksum is invalid")
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_status": "PASS",
        "release_branch": request.branch,
        "stage": request.stage,
        "approved_by": request.owner_approval,
        "triggered_by": request.triggered_by,
        "production_identity": request.production_identity,
        "git_sha": request.git_sha,
        "environment": request.environment,
        "kube_context": request.kube_context,
        "image_manifest_path": request.image_manifest_path,
        "image_manifest_sha256": manifest_digest,
        "evidence_dir": request.evidence_dir,
        "units": planned,
    }


def verify_release_plan(catalog: Catalog, units: ReleaseUnits, plan: dict[str, Any], *,
                        head_sha: str | None = None, manifest_path: Path | None = None) -> None:
    """Revalidate a serialized plan immediately before dispatch."""

    _require(isinstance(plan, dict), "release plan must be an object")
    text = json.dumps(plan)
    for handler in FORBIDDEN_HANDLERS:
        _require(handler not in text, f"release plan names the removed handler {handler}")
    _require(plan.get("schema_version") == SCHEMA_VERSION,
             "unsupported release plan schema (plans with component handlers are no longer accepted)")
    _require(plan.get("policy_status") == "PASS", "release plan policy status is not PASS")
    branch = plan.get("release_branch")
    _require(isinstance(branch, str) and RELEASE_BRANCH_PATTERN.fullmatch(branch) is not None,
             "release plan has no approved release branch")
    _require(isinstance(plan.get("approved_by"), str) and bool(plan["approved_by"].strip()),
             "release plan has no owner approval")
    _require(isinstance(plan.get("git_sha"), str) and SHA_PATTERN.fullmatch(plan["git_sha"]) is not None,
             "release plan git SHA is invalid")
    if head_sha is not None:
        _require(plan["git_sha"] == head_sha, "release plan SHA is not the checked-out commit (stale plan)")
    _require(plan.get("environment") in ALLOWED_ENVIRONMENTS, "release plan environment is invalid")
    stage = plan.get("stage")
    _require(stage in STAGES, "release plan stage is invalid")
    if stage == "production":
        _require(bool(str(plan.get("production_identity", "")).strip()), "production plan has no deployer identity")
        _require(str(plan.get("triggered_by", "")).strip() not in {"", plan["approved_by"].strip()},
                 "production plan approver must differ from the triggering user")
    for key in ("kube_context", "image_manifest_path", "evidence_dir"):
        _require(isinstance(plan.get(key), str) and bool(plan[key].strip()), f"release plan {key} is required")
    planned = plan.get("units")
    _require(isinstance(planned, list) and bool(planned), "release plan has no deployable units")
    available: dict[str, str] | None = None
    if manifest_path is not None:
        _require(manifest_path.is_file(), "release plan image manifest is missing")
        _require(manifest_sha256(manifest_path) == plan.get("image_manifest_sha256"),
                 "image manifest changed after the plan was built (stale plan)")
        available = manifest_images(json.loads(manifest_path.read_text(encoding="utf-8")), plan["git_sha"])
    names = []
    for item in planned:
        _require(isinstance(item, dict), "release plan units must be objects")
        name = item.get("name")
        _require(isinstance(name, str) and name in units.units, f"release unit {name} is unknown")
        unit = units.units[name]
        _require(unit.stage == stage, f"release unit {name} does not belong to stage {stage}")
        _require(unit.automation == "enabled", f"release unit {name} is not automated: {unit.blocked_reason}")
        recorded = {key: value for key, value in item.items() if key != "images"}
        _require(recorded == unit.to_plan(), f"release unit {name} no longer matches the release-unit catalog")
        images = item.get("images")
        _require(isinstance(images, dict) and set(images) == set(unit.images),
                 f"release unit {name} image set is invalid")
        for image, reference in images.items():
            _require(isinstance(reference, str) and DIGEST_PATTERN.fullmatch(reference) is not None,
                     f"release unit {name} image {image} is not an immutable digest")
            if available is not None:
                _require(available.get(image) == reference, f"release unit {name} image {image} is not the manifest digest")
        names.append(name)
    try:
        group = units.group(branch)
        expected = units.ordered(group.units_for(stage))
    except ReleaseUnitError as error:
        raise ReleasePolicyError(str(error)) from error
    _require(names == expected, "release units are not the group's units in dependency order")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleasePolicyError(f"cannot load {label} {path}: {error}") from error
    _require(isinstance(data, dict), f"{label} must be an object")
    return data


def request_from_file(path: Path) -> ReleaseRequest:
    data = _load_object(path, "release request")
    components = data.get("components")
    _require(isinstance(components, list) and all(isinstance(item, str) for item in components),
             "release request components must be a list of strings")
    data["components"] = tuple(components)
    try:
        return ReleaseRequest(**data)
    except TypeError as error:
        raise ReleasePolicyError(f"invalid release request fields: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", type=Path, default=Path("jenkins/catalog/components.json"))
    parser.add_argument("--units", type=Path, default=Path("jenkins/catalog/release-units.json"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)
    images = subparsers.add_parser("images", help="print the images the request's release units need (JSON)")
    images.add_argument("--request", type=Path, required=True)
    images.add_argument("--output", type=Path, required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--request", type=Path, required=True)
    build.add_argument("--image-manifest", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--head", default="", help="checked-out SHA the plan must match")
    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog)
        catalog.validate(args.root)
        units = load_release_units(args.units)
        units.validate(args.root, catalog)
        if args.command == "images":
            request = request_from_file(args.request)
            names = select_units(units, request, catalog)
            content = {"units": names, "images": required_images(units, names)}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        elif args.command == "build":
            request = request_from_file(args.request)
            manifest = _load_object(args.image_manifest, "image manifest")
            plan = build_release_plan(catalog, units, request, manifest, manifest_sha256(args.image_manifest))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            plan = _load_object(args.plan, "release plan")
            manifest = Path(plan["image_manifest_path"]) if isinstance(plan.get("image_manifest_path"), str) else None
            verify_release_plan(catalog, units, plan, head_sha=args.head or None,
                                manifest_path=(args.root / manifest) if manifest else None)
    except (ReleasePolicyError, CatalogError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
