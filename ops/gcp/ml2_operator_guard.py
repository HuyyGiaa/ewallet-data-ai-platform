"""Offline-testable fail-closed gates for the ML-2 operator workflow."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path


GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
PINNED_DVC_VERSION = "3.63.0"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True
    ).strip()


def validate_preflight(
    repo: Path,
    *,
    expected_reviewed_git_sha: str,
    expected_branch: str,
    evidence_paths: tuple[Path, ...],
    dvc_executable: str,
) -> None:
    if not GIT_SHA.fullmatch(expected_reviewed_git_sha):
        raise ValueError("EXPECTED_REVIEWED_GIT_SHA must be supplied externally")
    if _git(repo, "rev-parse", "HEAD") != expected_reviewed_git_sha:
        raise ValueError("actual HEAD does not match the reviewed Git SHA")
    if _git(repo, "branch", "--show-current") != expected_branch:
        raise ValueError("operator is on the wrong Git branch")
    if _git(repo, "status", "--porcelain"):
        raise ValueError("operator worktree is not clean")
    missing = [str(path) for path in evidence_paths if not path.is_file()]
    if missing:
        raise ValueError(f"required evidence is missing: {missing}")
    resolved = shutil.which(dvc_executable)
    if resolved is None:
        raise ValueError("pinned DVC executable is unavailable")
    version = subprocess.check_output([resolved, "--version"], text=True).strip()
    if version not in {PINNED_DVC_VERSION, f"dvc {PINNED_DVC_VERSION}"}:
        raise ValueError(
            f"DVC version {version!r} does not match pinned {PINNED_DVC_VERSION}"
        )


def run_checked(stage: str, command: list[str]) -> None:
    if not stage or not command:
        raise ValueError("a named stage and command are required")
    subprocess.run(command, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--repo", type=Path, required=True)
    preflight.add_argument("--expected-reviewed-git-sha", required=True)
    preflight.add_argument(
        "--expected-branch", default="codex/ml1-training-snapshot"
    )
    preflight.add_argument("--evidence", type=Path, action="append", default=[])
    preflight.add_argument("--dvc-executable", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--stage", required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action == "preflight":
        validate_preflight(
            args.repo,
            expected_reviewed_git_sha=args.expected_reviewed_git_sha,
            expected_branch=args.expected_branch,
            evidence_paths=tuple(args.evidence),
            dvc_executable=args.dvc_executable,
        )
    else:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        run_checked(args.stage, command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
