"""Build and verify the immutable Git bundle the Jenkins controller reads (``/opt/ewallet-source``).

Jenkins never reads GitHub: its jobs check out ``/opt/ewallet-source/repository.bundle`` with
``+refs/heads/*:refs/remotes/origin/*``, and every release job requires
``origin/release/<group> == EXPECTED_GIT_SHA``. Creating a branch on GitHub therefore changes
nothing for Jenkins; a new bundle (and a new snapshot image) has to carry the refs.

``build`` works in a throw-away bare clone, so it never creates, moves or pushes a ref in the
caller's repository. It writes:

* ``refs/heads/release/<group>`` at the reviewed SHA for every release group in
  ``jenkins/catalog/release-units.json``;
* ``refs/heads/proof/{ml,agent,docs}`` at the same SHA (the CI-only proof jobs' branches);
* ``refs/heads/base`` at the change-detection base (an ancestor of the SHA), so ``TARGET_BRANCH``
  and the merge base are inside the bundle;
* ``refs/heads/provenance/<name>`` for every historical commit listed in
  ``jenkins/catalog/snapshot-provenance.json``: commits the repository's own tests read by SHA that
  are not ancestors of the release (a fresh Jenkins workspace must hold their objects);
* ``source-metadata.txt`` with every ref, the bundle sha256 and the git version.

``verify`` re-reads a bundle with git alone: every expected ref at the expected SHA, the base an
ancestor, the bundle self-consistent, and a checkout of the release SHA containing the rebuilt
CI/CD (``scripts/ci/release_dispatch.py``). The Jenkins init container then checks the bundle
sha256 against the metadata before the controller starts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"^[0-9a-f]{40}$")
PROOF_REFS = ("proof/ml", "proof/agent", "proof/docs")
PROVENANCE = "jenkins/catalog/snapshot-provenance.json"
REQUIRED_PATHS = ("scripts/ci/release_dispatch.py", "jenkins/catalog/release-units.json",
                  "jenkins/pipeline/componentPipeline.groovy")


class SnapshotError(RuntimeError):
    """The bundle cannot be built or does not match the reviewed release."""


def git(*args: str, cwd: Path, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise SnapshotError(f"git {' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def release_groups(root: Path = ROOT) -> list[str]:
    units = json.loads((root / "jenkins/catalog/release-units.json").read_text(encoding="utf-8"))
    return sorted(units["groups"])


def provenance_refs(root: Path = ROOT) -> dict[str, str]:
    """refs/heads/provenance/<name> -> commit, from the reviewed provenance list (empty if absent)."""

    path = root / PROVENANCE
    if not path.is_file():
        return {}
    refs = {}
    for name, item in json.loads(path.read_text(encoding="utf-8")).get("refs", {}).items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name) or not SHA.fullmatch(item.get("sha", "")) or not item.get("reason"):
            raise SnapshotError(f"invalid provenance ref {name}: needs a DNS-like name, a full SHA and a reason")
        refs[f"refs/heads/provenance/{name}"] = item["sha"]
    return refs


def expected_refs(sha: str, base: str, groups: list[str]) -> dict[str, str]:
    refs = {f"refs/heads/{group}": sha for group in groups}
    refs.update({f"refs/heads/{name}": sha for name in PROOF_REFS})
    refs["refs/heads/base"] = base
    return refs


def build(source: Path, sha: str, base: str, out: Path) -> dict[str, str]:
    for label, value in (("release SHA", sha), ("base SHA", base)):
        if not SHA.fullmatch(value):
            raise SnapshotError(f"{label} must be a full SHA")
    if subprocess.run(["git", "merge-base", "--is-ancestor", base, sha], cwd=source,
                      capture_output=True).returncode != 0:
        raise SnapshotError("the base must be an ancestor of the release SHA")
    groups = release_groups(source)
    refs = expected_refs(sha, base, groups)
    for ref, target in provenance_refs(source).items():
        if subprocess.run(["git", "cat-file", "-e", f"{target}^{{commit}}"], cwd=source, capture_output=True).returncode:
            raise SnapshotError(f"provenance commit {target} ({ref}) is not in the source repository")
        refs[ref] = target
    out.mkdir(parents=True, exist_ok=True)
    bundle = out / "repository.bundle"
    with tempfile.TemporaryDirectory(prefix="snapshot.") as directory:
        work = Path(directory) / "repo.git"
        git("clone", "--quiet", "--bare", "--no-local", "--no-tags", str(source), str(work), cwd=source)
        for ref, target in refs.items():
            git("update-ref", ref, target, cwd=work)
        git("-c", "pack.threads=1", "bundle", "create", str(bundle), *refs, cwd=work)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    metadata = {"source_sha": sha, "base_sha": base, "release_groups": ",".join(groups),
                **{f"ref:{ref}": target for ref, target in sorted(refs.items())},
                "git_version": git("--version", cwd=source), "bundle_sha256": digest}
    (out / "source-metadata.txt").write_text("".join(f"{key}={value}\n" for key, value in metadata.items()),
                                             encoding="utf-8")
    return metadata


def verify(directory: Path, sha: str) -> list[str]:
    bundle, metadata_path = directory / "repository.bundle", directory / "source-metadata.txt"
    problems: list[str] = []
    metadata = dict(line.split("=", 1) for line in metadata_path.read_text(encoding="utf-8").splitlines() if "=" in line)
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != metadata.get("bundle_sha256"):
        problems.append("bundle sha256 does not match source-metadata.txt")
    if metadata.get("source_sha") != sha:
        problems.append(f"metadata source_sha {metadata.get('source_sha')} is not {sha}")
    with tempfile.TemporaryDirectory(prefix="snapshot-verify.") as work_dir:
        work = Path(work_dir)
        git("init", "--quiet", cwd=work)
        if subprocess.run(["git", "bundle", "verify", str(bundle)], cwd=work, capture_output=True).returncode != 0:
            return [*problems, "git bundle verify failed"]
        heads = dict(reversed(line.split(" ", 1)) for line in git("bundle", "list-heads", str(bundle), cwd=work).splitlines())
        groups = [ref.removeprefix("refs/heads/") for ref in heads if ref.startswith("refs/heads/release/")]
        expected = expected_refs(sha, metadata.get("base_sha", ""), sorted(groups))
        expected.update({ref: target for ref, target in metadata.items() if ref.startswith("ref:refs/heads/provenance/")
                         for ref, target in [(ref.removeprefix("ref:"), target)]})
        if not groups:
            problems.append("the bundle has no release refs")
        for ref, target in expected.items():
            if heads.get(ref) != target:
                problems.append(f"{ref} is {heads.get(ref)}, expected {target}")
        if sorted(groups) != sorted(metadata.get("release_groups", "").split(",")):
            problems.append("release refs differ from the release groups recorded at build time")
        # Exactly what Jenkins does: fetch heads into origin/* and check out the release SHA.
        try:
            git("fetch", "--quiet", str(bundle), "+refs/heads/*:refs/remotes/origin/*", cwd=work)
        except SnapshotError as error:
            return [*problems, f"Jenkins-style fetch of the bundle failed: {error}"]
        for group in groups:
            if git("rev-parse", f"refs/remotes/origin/{group}^{{commit}}", cwd=work) != sha:
                problems.append(f"origin/{group} does not resolve to {sha}")
        if subprocess.run(["git", "merge-base", "--is-ancestor", metadata.get("base_sha", ""), sha], cwd=work,
                          capture_output=True).returncode != 0:
            problems.append("the base is not an ancestor of the release SHA inside the bundle")
        for path in REQUIRED_PATHS:
            if subprocess.run(["git", "cat-file", "-e", f"{sha}:{path}"], cwd=work, capture_output=True).returncode != 0:
                problems.append(f"the release SHA does not contain {path}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    built = sub.add_parser("build")
    built.add_argument("--sha", required=True)
    built.add_argument("--base", required=True)
    built.add_argument("--out", type=Path, required=True)
    checked = sub.add_parser("verify")
    checked.add_argument("--dir", type=Path, required=True)
    checked.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            metadata = build(ROOT, args.sha, args.base, args.out)
            problems = verify(args.out, args.sha)
            print(json.dumps({"bundle_sha256": metadata["bundle_sha256"], "problems": problems}, sort_keys=True))
        else:
            problems = verify(args.dir, args.sha)
            print(json.dumps({"verdict": "PASS" if not problems else "FAIL", "problems": problems}, sort_keys=True))
    except SnapshotError as error:
        parser.error(str(error))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
