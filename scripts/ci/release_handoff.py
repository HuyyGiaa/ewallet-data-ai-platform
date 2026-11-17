"""Jenkins -> operator release handoff: what Jenkins produced, and the proof the operator checks first.

Jenkins tests, builds, scans, rehearses and plans a production release, but holds no production
identity (owner decision D1). Its production stage therefore ends with
``READY_FOR_OPERATOR_PROMOTION`` and archives a handoff directory; the approved operator runs the
production transaction from that directory on a workstation.

``handoff.json`` records the Jenkins job, build number, URL and tag; the exact Git SHA and release
branch; the release units with their dependency graph, target namespaces and immutable digests; the
approved target context, the required production identity and the approval provenance (triggering
user, production approver); the runtime verification contract; the source snapshot identity; the
recovery entry point; and the SHA-256 of every artifact it names (plan, image manifest, request, CI
verdict, scan verdict, Jenkins build record, source metadata).

Trust boundary. A SHA-256 is integrity, not authentication: anyone who can rewrite the files can
rewrite their checksums. The anchor is the handoff digest Jenkins prints on its console
(``RELEASE_HANDOFF sha256=<hex>``), which the operator reads from the authenticated Jenkins UI
(or from the archived artifact fingerprint) and passes as ``--handoff-sha256``. Everything else is
bound to that digest. There is no signature; a compromised Jenkins controller is out of scope and
is covered by the separate production approval and the operator's own review of the plan.

``verify`` fails closed on: a digest that is not the anchor, a missing or changed artifact, a plan
that no longer verifies against the catalog, the release units, the checked-out SHA and the image
manifest, any SHA mismatch along the chain (reviewed SHA = Jenkins checkout = source snapshot =
manifest revision = plan SHA = operator HEAD), a target context or production identity other than
the approved ones, an approver equal to the trigger, units that differ from the plan, and a
verification contract that differs from the operator's checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
STATUS = "READY_FOR_OPERATOR_PROMOTION"
SCHEMA = 1
ARTIFACTS = ("release-plan.json", "image-manifest.json", "release-request.json", "ci-verdict.json",
             "image-scan.json", "jenkins-build.txt")
OPTIONAL_ARTIFACTS = ("source-metadata.txt",)
CONTRACTS = ("jenkins/cd/sandbox-candidate.contract.json", "jenkins/catalog/release-units.json")


class HandoffError(ValueError):
    """The handoff cannot be produced."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def key_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def build(out: Path, sources: Mapping[str, Path], root: Path = ROOT) -> tuple[dict[str, Any], str]:
    """Copy the artifacts into ``out`` and write ``handoff.json``; returns (handoff, its sha256)."""

    from scripts.ci.runtime_verify import checks_for

    out.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, dict[str, str]] = {}
    for name in ARTIFACTS + OPTIONAL_ARTIFACTS:
        source = sources.get(name)
        if source is None or not source.is_file():
            if name in OPTIONAL_ARTIFACTS:
                continue
            raise HandoffError(f"the handoff needs {name}")
        shutil.copyfile(source, out / name)
        artifacts[name] = {"path": name, "sha256": sha256(out / name)}
    plan = json.loads((out / "release-plan.json").read_text(encoding="utf-8"))
    if plan.get("stage") != "production":
        raise HandoffError("a handoff carries a production plan only")
    build_record = key_values(out / "jenkins-build.txt")
    source = key_values(out / "source-metadata.txt")
    handoff = {
        "schema": SCHEMA, "status": STATUS,
        "jenkins": {key: build_record.get(key, "") for key in ("job_name", "build_number", "build_url", "build_tag",
                                                               "git_sha", "triggered_by")},
        "git_sha": plan["git_sha"], "release_branch": plan["release_branch"], "environment": plan["environment"],
        "target_context": plan["kube_context"], "production_identity": plan["production_identity"],
        "approval": {"triggered_by": plan["triggered_by"], "production_approved_by": plan["approved_by"]},
        "image_manifest_sha256": plan["image_manifest_sha256"],
        "units": [{"name": u["name"], "kind": u["kind"], "depends_on": u["depends_on"],
                   "target_namespace": u["target_namespace"], "images": u["images"]} for u in plan["units"]],
        "verification": {
            "runtime_checks": checks_for(plan["units"]),
            "requires": ["AGI_ENV whose CTX equals target_context", "PROMOTION_APPROVED=yes",
                         "operator HEAD equals git_sha"],
            "contracts": {path: sha256(root / path) for path in CONTRACTS},
        },
        "source_snapshot": {"source_sha": source.get("source_sha"), "bundle_sha256": source.get("bundle_sha256")}
        if source else None,
        "recovery": {"journal": "<evidence>/transaction-journal.json",
                     "command": "python -m scripts.ci.production_release recovery-plan --journal <journal>"},
        "artifacts": artifacts,
        "trust": "sha256 integrity bound to the digest printed on the authenticated Jenkins console; not a signature",
    }
    text = json.dumps(handoff, indent=2, sort_keys=True) + "\n"
    (out / "handoff.json").write_text(text, encoding="utf-8")
    return handoff, hashlib.sha256(text.encode()).hexdigest()


def verify(directory: Path, expected_sha256: str, *, head_sha: str, production_context: str,
           production_identity: str | None = None, root: Path = ROOT) -> tuple[dict[str, Any] | None, list[str]]:
    """Return (handoff, problems); the operator may proceed only with no problems."""

    from scripts.ci.catalog import CatalogError, load_catalog
    from scripts.ci.release_policy import ReleasePolicyError, verify_release_plan
    from scripts.ci.release_units import load_release_units

    path = directory / "handoff.json"
    if not path.is_file():
        return None, ["MISSING_HANDOFF: handoff.json is missing"]
    if sha256(path) != expected_sha256:
        return None, ["HANDOFF_DIGEST_MISMATCH: handoff.json is not the one Jenkins announced"]
    try:
        handoff = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None, ["MALFORMED_HANDOFF: handoff.json is not JSON"]
    problems: list[str] = []
    if handoff.get("schema") != SCHEMA or handoff.get("status") != STATUS:
        problems.append(f"WRONG_STATUS: {handoff.get('status')} (schema {handoff.get('schema')})")
    artifacts = handoff.get("artifacts") or {}
    for name in ARTIFACTS:
        if name not in artifacts:
            problems.append(f"MISSING_ARTIFACT: {name} is not in the handoff")
    for name, entry in sorted(artifacts.items()):
        file = directory / str(entry.get("path", ""))
        if file.parent != directory or not file.is_file():
            problems.append(f"MISSING_ARTIFACT: {name}")
        elif sha256(file) != entry.get("sha256"):
            problems.append(f"TAMPERED_ARTIFACT: {name} changed after Jenkins produced it")
    if problems:
        return handoff, problems
    plan = json.loads((directory / "release-plan.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "image-manifest.json").read_text(encoding="utf-8"))
    sha = handoff.get("git_sha")
    chain = {"plan": plan.get("git_sha"), "image manifest": manifest.get("git_sha"),
             "Jenkins checkout": (handoff.get("jenkins") or {}).get("git_sha"), "operator HEAD": head_sha}
    if handoff.get("source_snapshot"):
        chain["source snapshot"] = handoff["source_snapshot"].get("source_sha")
    for label, value in chain.items():
        if value != sha:
            problems.append(f"SHA_MISMATCH: {label} is {value}, the handoff is {sha}")
    for name, image in (manifest.get("images") or {}).items():
        if image.get("revision", manifest.get("git_sha")) != sha:
            problems.append(f"SHA_MISMATCH: image {name} was built from {image.get('revision')}")
    if handoff.get("target_context") != production_context or plan.get("kube_context") != production_context:
        problems.append(f"WRONG_TARGET: approved context {handoff.get('target_context')}, operator uses {production_context}")
    if production_identity is not None and handoff.get("production_identity") != production_identity:
        problems.append(f"WRONG_IDENTITY: approved {handoff.get('production_identity')}, operator is {production_identity}")
    approval = handoff.get("approval") or {}
    if not approval.get("production_approved_by") or approval.get("production_approved_by") == approval.get("triggered_by"):
        problems.append("APPROVAL: the production approver must exist and differ from the triggering user")
    if [u.get("name") for u in handoff.get("units") or []] != [u.get("name") for u in plan.get("units") or []]:
        problems.append("UNITS_MISMATCH: the handoff units are not the plan units")
    for contract, digest in ((handoff.get("verification") or {}).get("contracts") or {}).items():
        if not (root / contract).is_file() or sha256(root / contract) != digest:
            problems.append(f"CONTRACT_MISMATCH: {contract} differs from the operator checkout")
    try:
        catalog = load_catalog(root / "jenkins/catalog/components.json")
        units = load_release_units(root / "jenkins/catalog/release-units.json")
        verify_release_plan(catalog, units, plan, head_sha=head_sha, manifest_path=directory / "image-manifest.json")
    except (CatalogError, ReleasePolicyError, OSError, ValueError) as error:
        problems.append(f"PLAN_REFUSED: {error}")
    return handoff, problems


def _head(root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    made = sub.add_parser("build")
    made.add_argument("--out", type=Path, required=True)
    for name in ARTIFACTS + OPTIONAL_ARTIFACTS:
        made.add_argument(f"--{name.split('.')[0]}", type=Path, required=name in ARTIFACTS)
    checked = sub.add_parser("verify")
    checked.add_argument("--dir", type=Path, required=True)
    checked.add_argument("--handoff-sha256", required=True)
    checked.add_argument("--production-context", required=True)
    checked.add_argument("--production-identity")
    args = parser.parse_args(argv)
    if args.command == "build":
        sources = {name: getattr(args, name.split(".")[0].replace("-", "_")) for name in ARTIFACTS + OPTIONAL_ARTIFACTS}
        try:
            _, digest = build(args.out, {k: v for k, v in sources.items() if v is not None})
        except (HandoffError, OSError, KeyError, ValueError) as error:
            parser.error(str(error))
        # The trust anchor: the operator copies this digest from the authenticated Jenkins console.
        print(f"RELEASE_HANDOFF sha256={digest} status={STATUS}")
        return 0
    handoff, problems = verify(args.dir, args.handoff_sha256, head_sha=_head(ROOT),
                               production_context=args.production_context,
                               production_identity=args.production_identity)
    print(json.dumps({"verdict": "PASS" if not problems else "FAIL", "problems": problems}, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
