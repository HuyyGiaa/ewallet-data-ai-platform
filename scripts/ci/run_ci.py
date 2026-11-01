"""Execute selected catalog components and preserve evidence on every outcome."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Any, Callable

from scripts.ci.catalog import Catalog, load_catalog
from scripts.ci.evidence import EvidenceWriter, classify_failure


Executor = Callable[[list[str], Path], subprocess.CompletedProcess[str]]
ToolLookup = Callable[[str], str | None]


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    results: list[dict[str, Any]]
    evidence_dir: Path


def _execute(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)


def _selected_projects(catalog: Catalog, components: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({
        project
        for name in components
        for project in catalog.components[name].python_projects
    }))


def _log_path(index: int, stage: str) -> str:
    safe_stage = re.sub(r"[^A-Za-z0-9._-]+", "-", stage).strip("-.") or "stage"
    return f"logs/{index:04d}-{safe_stage}.log"


def execute_components(
    catalog: Catalog,
    components: tuple[str, ...],
    root: Path,
    evidence_dir: Path,
    *,
    executor: Executor = _execute,
    tool_lookup: ToolLookup = shutil.which,
    metadata: dict[str, Any] | None = None,
) -> Outcome:
    writer = EvidenceWriter(evidence_dir, {**(metadata or {}), "components": list(components)})
    results: list[dict[str, Any]] = []
    exit_code = 0
    projects: tuple[str, ...] = ()
    try:
        unknown = sorted(set(components) - set(catalog.components))
        if unknown:
            raise ValueError(f"unknown selected components: {unknown}")
        projects = _selected_projects(catalog, components)
        writer.write_lock_hashes(root, projects)
        writer.write_text("component-matrix.md", catalog.matrix_markdown())
        commands: list[tuple[str, list[str], Path]] = []
        for project in projects:
            commands.extend((
                (f"uv-lock:{project}", ["uv", "lock", "--check"], root / project),
                (f"uv-sync:{project}", ["uv", "sync", "--locked"], root / project),
            ))
        for component_name in components:
            for index, command in enumerate(catalog.components[component_name].commands, start=1):
                commands.append((f"component:{component_name}:{index}", shlex.split(command), root))
        for index, (stage, argv, cwd) in enumerate(commands, start=1):
            executable = tool_lookup(argv[0])
            if not executable:
                exit_code = 127
                classification = classify_failure(stage, exit_code, "missing")
                log_path = _log_path(index, stage)
                writer.write_text(log_path, f"missing required tool: {argv[0]}\n")
                results.append({
                    "stage": stage,
                    "argv": argv,
                    "cwd": str(cwd.relative_to(root) or Path(".")),
                    "exit_code": exit_code,
                    "classification": classification,
                    "log": log_path,
                })
                break
            resolved = [executable, *argv[1:]]
            completed = executor(resolved, cwd)
            exit_code = completed.returncode
            classification = classify_failure(stage, exit_code, "available")
            log_path = _log_path(index, stage)
            writer.write_text(
                log_path,
                "$ " + shlex.join(resolved) + "\n"
                + f"cwd: {cwd}\nexit_code: {exit_code}\n"
                + "--- stdout ---\n" + completed.stdout
                + "--- stderr ---\n" + completed.stderr,
            )
            results.append({
                "stage": stage,
                "argv": resolved,
                "cwd": str(cwd.relative_to(root) or Path(".")),
                "exit_code": exit_code,
                "classification": classification,
                "log": log_path,
            })
            if exit_code != 0:
                break
    except Exception as error:
        exit_code = exit_code or 2
        log_path = "logs/0000-runner-error.log"
        writer.write_text(log_path, f"{type(error).__name__}: {error}\n")
        results.append({
            "stage": "runner",
            "exit_code": exit_code,
            "classification": "ENVIRONMENT_FAILURE",
            "log": log_path,
        })
    finally:
        writer.finalize(status="PASS" if exit_code == 0 else "FAILED", results=results)
    return Outcome(exit_code=exit_code, results=results, evidence_dir=evidence_dir)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "UNAVAILABLE"


def worktree_metadata(root: Path) -> dict[str, object]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return {"working_tree_dirty": None, "working_tree_status_sha256": None}
    status = result.stdout
    return {
        "working_tree_dirty": bool(status),
        "working_tree_status_sha256": hashlib.sha256(status.encode()).hexdigest() if status else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("jenkins/catalog/components.json"))
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    catalog.validate(args.root)
    scope = json.loads(args.scope.read_text(encoding="utf-8"))
    metadata = {
        "git_branch": _git(args.root, "branch", "--show-current"),
        "git_sha": _git(args.root, "rev-parse", "HEAD"),
        "commit_message": _git(args.root, "log", "-1", "--pretty=%s"),
        "build_number": os.environ.get("BUILD_NUMBER", "LOCAL"),
        "trigger": os.environ.get("BUILD_CAUSE", "local-manual"),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "merge_base": scope.get("merge_base"),
        "changed_files": scope.get("changed_files", []),
        "full_validation_reason": scope.get("full_validation_reason"),
        **worktree_metadata(args.root),
    }
    outcome = execute_components(
        catalog,
        tuple(scope.get("components", [])),
        args.root,
        args.evidence_dir,
        metadata=metadata,
    )
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
