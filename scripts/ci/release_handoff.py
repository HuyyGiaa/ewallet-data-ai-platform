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
verification contract that differs from the operator's checkout. It also checks the artifacts'
meaning, not only their checksums: the source snapshot metadata (``source-metadata.txt``, mandatory:
source SHA, the release branch ref at that SHA, a well-formed bundle digest), the CI verdict (PASS for
this SHA, real test cases executed, no failed command, every changed released component tested), the
image scan (PASS for every image the plan deploys, at exactly the manifest digest, by the pinned
scanner, for this SHA), the units (kind, dependencies, namespace and digests equal the plan's), and
the approved production identity (an exact Kubernetes principal; ``deploy_identity``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
STATUS = "READY_FOR_OPERATOR_PROMOTION"
SCHEMA = 1
ARTIFACTS = ("release-plan.json", "image-manifest.json", "release-request.json", "ci-verdict.json",
             "image-scan.json", "jenkins-build.txt", "source-metadata.txt")
OPTIONAL_ARTIFACTS: tuple[str, ...] = ()
HEX64 = re.compile(r"^[0-9a-f]{64}$")
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
    from scripts.ci.deploy_identity import valid_principal

    if not valid_principal(plan.get("production_identity")):
        raise HandoffError("the production identity must be an exact Kubernetes principal "
                           "(system:serviceaccount:<namespace>:<name> or a user e-mail)")
    build_record = key_values(out / "jenkins-build.txt")
    source = key_values(out / "source-metadata.txt")
    manifest = json.loads((out / "image-manifest.json").read_text(encoding="utf-8"))
    problems = [*source_problems(source, plan["git_sha"], plan["release_branch"]),
                *ci_problems(json.loads((out / "ci-verdict.json").read_text(encoding="utf-8")), plan["git_sha"], plan),
                *scan_problems(json.loads((out / "image-scan.json").read_text(encoding="utf-8")), plan["git_sha"], plan,
                               manifest)]
    if problems:
        # Jenkins never announces READY_FOR_OPERATOR_PROMOTION for artifacts that would not verify.
        raise HandoffError("; ".join(problems))
    handoff = {
        "schema": SCHEMA, "status": STATUS,
        "jenkins": {key: build_record.get(key, "") for key in ("job_name", "build_number", "build_url", "build_tag",
                                                               "git_sha", "triggered_by")},
        "git_sha": plan["git_sha"], "release_branch": plan["release_branch"], "environment": plan["environment"],
        "target_context": plan["kube_context"], "production_identity": plan["production_identity"],
        "approval": {"triggered_by": plan["triggered_by"], "production_approved_by": plan["approved_by"]},
        "image_manifest_sha256": plan["image_manifest_sha256"],
        "units": unit_graph(plan),
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


def unit_graph(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The release-unit graph as the handoff records it; verify compares it field by field with the plan."""

    return [{"name": u["name"], "kind": u["kind"], "depends_on": u["depends_on"],
             "target_namespace": u["target_namespace"], "images": u["images"]} for u in plan["units"]]


def source_problems(metadata: Mapping[str, str], sha: str, branch: str) -> list[str]:
    problems = []
    if metadata.get("source_sha") != sha:
        problems.append(f"SOURCE_MISMATCH: the snapshot source_sha is {metadata.get('source_sha')}, not {sha}")
    if metadata.get(f"ref:refs/heads/{branch}") != sha:
        problems.append(f"SOURCE_MISMATCH: the snapshot does not carry {branch} at {sha}")
    if not HEX64.fullmatch(metadata.get("bundle_sha256", "")):
        problems.append("SOURCE_MISMATCH: the snapshot metadata has no valid bundle_sha256")
    return problems


def ci_problems(verdict: Any, sha: str, plan: Mapping[str, Any]) -> list[str]:
    """Semantic check of scripts/ci/run_ci.py's ci-verdict.json for a production release."""

    if not isinstance(verdict, dict):
        return ["CI_VERDICT_INVALID: ci-verdict.json is not an object"]
    problems = []
    if verdict.get("status") != "PASS":
        problems.append(f"CI_NOT_PASSED: status {verdict.get('status')} ({verdict.get('reason')})")
    if verdict.get("git_sha") != sha:
        problems.append(f"CI_WRONG_RELEASE: the CI verdict is for {verdict.get('git_sha')}, not {sha}")
    cases = verdict.get("test_cases") if isinstance(verdict.get("test_cases"), dict) else {}
    if not isinstance(verdict.get("test_commands"), int) or verdict["test_commands"] < 1 or not cases.get("tests"):
        problems.append("CI_NO_TESTS: no test case was executed")
    if cases.get("failures") or cases.get("errors"):
        problems.append(f"CI_FAILED_TESTS: failures {cases.get('failures')}, errors {cases.get('errors')}")
    failed = [item.get("stage") for item in verdict.get("executed") or []
              if item.get("exit_code") != 0 or item.get("classification")]
    if failed:
        problems.append(f"CI_FAILED_COMMANDS: {failed}")
    released = {c for unit in plan.get("units") or [] for c in unit.get("components") or []}
    changed = released & set(verdict.get("scope_components") or [])
    untested = sorted(changed - set(verdict.get("selected_components") or []))
    if not changed:
        problems.append("CI_NOT_APPLICABLE: none of the released components was in the CI scope")
    if untested:
        problems.append(f"CI_UNTESTED_COMPONENTS: {untested} changed but were not tested")
    return problems


def scan_problems(scan: Any, sha: str, plan: Mapping[str, Any], manifest: Mapping[str, Any]) -> list[str]:
    """Semantic check of scripts/ci/image_scan.py's image-scan.json against the plan and the manifest."""

    from scripts.ci.image_scan import TRIVY

    required = {image: ref for unit in plan.get("units") or [] for image, ref in (unit.get("images") or {}).items()}
    if not isinstance(scan, dict):
        return ["SCAN_INVALID: image-scan.json is not an object"]
    if not required:
        return [] if scan.get("verdict") == "NOT_APPLICABLE" else [f"SCAN_UNEXPECTED: {scan.get('verdict')} for no image"]
    problems = []
    if scan.get("verdict") != "PASS":
        problems.append(f"SCAN_NOT_PASSED: verdict {scan.get('verdict')}")
    if scan.get("scanner") != TRIVY:
        problems.append("SCAN_WRONG_SCANNER: not the pinned scanner")
    if scan.get("git_sha") != sha:
        problems.append(f"SCAN_WRONG_RELEASE: the scan is for {scan.get('git_sha')}, not {sha}")
    images = scan.get("images") if isinstance(scan.get("images"), dict) else {}
    manifest_images = manifest.get("images") or {}
    for name, ref in sorted(required.items()):
        result = images.get(name)
        if not isinstance(result, dict):
            problems.append(f"SCAN_MISSING: {name} was not scanned")
            continue
        if result.get("status") != "PASS":
            problems.append(f"SCAN_FAILED: {name} is {result.get('status')}")
        if not (result.get("digest_ref") == ref == (manifest_images.get(name) or {}).get("digest_ref")):
            problems.append(f"SCAN_DIGEST_MISMATCH: {name} was scanned at {result.get('digest_ref')}, deployed at {ref}")
    return problems


def verify(directory: Path, expected_sha256: str, *, head_sha: str, production_context: str,
           production_identity: str | None, root: Path = ROOT) -> tuple[dict[str, Any] | None, list[str]]:
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
             "Jenkins checkout": (handoff.get("jenkins") or {}).get("git_sha"), "operator HEAD": head_sha,
             "source snapshot": (handoff.get("source_snapshot") or {}).get("source_sha")}
    for label, value in chain.items():
        if value != sha:
            problems.append(f"SHA_MISMATCH: {label} is {value}, the handoff is {sha}")
    for name, image in (manifest.get("images") or {}).items():
        if image.get("revision", manifest.get("git_sha")) != sha:
            problems.append(f"SHA_MISMATCH: image {name} was built from {image.get('revision')}")
    if handoff.get("target_context") != production_context or plan.get("kube_context") != production_context:
        problems.append(f"WRONG_TARGET: approved context {handoff.get('target_context')}, operator uses {production_context}")
    from scripts.ci.deploy_identity import IdentityError, require_approved

    if plan.get("production_identity") != handoff.get("production_identity"):
        problems.append("WRONG_IDENTITY: the plan and the handoff approve different identities")
    try:
        require_approved(handoff.get("production_identity"), production_identity)
    except IdentityError as error:
        problems.append(f"WRONG_IDENTITY: {error}")
    approval = handoff.get("approval") or {}
    if not approval.get("production_approved_by") or approval.get("production_approved_by") == approval.get("triggered_by"):
        problems.append("APPROVAL: the production approver must exist and differ from the triggering user")
    if handoff.get("units") != unit_graph(plan):
        problems.append("UNITS_MISMATCH: the handoff unit graph (kinds, dependencies, namespaces, digests) is not the plan's")
    if handoff.get("release_branch") != plan.get("release_branch"):
        problems.append("UNITS_MISMATCH: the handoff release branch is not the plan's")
    problems += source_problems(key_values(directory / "source-metadata.txt"), sha, plan.get("release_branch", ""))
    try:
        problems += ci_problems(json.loads((directory / "ci-verdict.json").read_text(encoding="utf-8")), sha, plan)
    except ValueError:
        problems.append("CI_VERDICT_INVALID: ci-verdict.json is not JSON")
    try:
        problems += scan_problems(json.loads((directory / "image-scan.json").read_text(encoding="utf-8")), sha, plan,
                                  manifest)
    except ValueError:
        problems.append("SCAN_INVALID: image-scan.json is not JSON")
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
    checked.add_argument("--production-identity", required=True)
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
