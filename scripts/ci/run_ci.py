"""Execute selected catalog components and preserve evidence on every outcome.

Every run ends with ``ci-verdict.json`` and one of:

* ``PASS``: every selected command passed and every test command reported at least one test case;
* ``FAILED``: a command failed, a test command executed no test case (``NO_TESTS_EXECUTED``),
  or the scope was empty without a reason (``EMPTY_SCOPE``: e.g. an empty diff);
* ``NOT_APPLICABLE``: nothing needed testing, for a stated reason (documentation-only change, or
  the job's profile owns none of the changed components). It is not a test pass: no test ran,
  and a release refuses it.
"""

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
import xml.etree.ElementTree as ElementTree

from scripts.ci.catalog import Catalog, load_catalog
from scripts.ci.evidence import EvidenceWriter, classify_failure


Executor = Callable[[list[str], Path], subprocess.CompletedProcess[str]]
ToolLookup = Callable[[str], str | None]


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    results: list[dict[str, Any]]
    evidence_dir: Path


def project_environment(argv: list[str], cwd: Path, root: Path) -> Path | None:
    """The locked environment a uv command uses, outside the source tree.

    A project's default ``.venv`` lives inside the checkout, so its path (for example
    ``infra/docker/llm-agent-tooling/.venv/bin``) ends up on ``PATH`` and in anything a test
    records from the environment. Keeping each project's environment in a cache directory named
    by a hash of the project path keeps the checkout clean and the environment identical across
    runs. ``EWALLET_CI_VENV_ROOT`` overrides the cache root.
    """

    if not argv or Path(argv[0]).name != "uv":
        return None
    if "--project" in argv:
        project = argv[argv.index("--project") + 1]
    else:
        try:
            project = cwd.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
    cache = Path(os.environ.get("EWALLET_CI_VENV_ROOT") or Path.home() / ".cache/ewallet-ci-venvs")
    return cache / hashlib.sha256(project.encode()).hexdigest()[:16]


def _execute(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[2]
    environment = project_environment(argv, cwd, root)
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(environment)} if environment else None
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False, env=env)


def _selected_projects(catalog: Catalog, components: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({
        project
        for name in components
        for project in catalog.components[name].python_projects
    }))


def _safe_stage(stage: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stage).strip("-.") or "stage"


def _log_path(index: int, stage: str) -> str:
    return f"logs/{index:04d}-{_safe_stage(stage)}.log"


def _with_junit_report(argv: list[str], report: Path, stage: str) -> list[str]:
    """Give each test stage one JUnit report; leave other commands unchanged.

    ``-m unittest`` goes through the stdlib JUnit runner. ``-m pytest`` gets its own
    ``--junitxml`` with the stage as the suite name.
    """

    for position in range(len(argv) - 1):
        if argv[position:position + 2] == ["-m", "unittest"]:
            return [
                *argv[:position], "-m", "scripts.ci.junit_unittest",
                "--junit-xml", str(report), "--suite-name", stage,
                *argv[position + 2:],
            ]
        if argv[position:position + 2] == ["-m", "pytest"]:
            return [
                *argv[:position + 2], f"--junitxml={report}",
                "-o", f"junit_suite_name={stage}", *argv[position + 2:],
            ]
    return argv


NO_TESTS = 4
EMPTY_SCOPE = 3


def junit_counts(path: Path) -> dict[str, int] | None:
    """Test-case totals of one JUnit report, or ``None`` when it is missing or unreadable."""

    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            try:
                totals[key] += int(suite.get(key, 0) or 0)
            except ValueError:
                return None
    return totals


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
            report = evidence_dir / "junit" / f"junit-{index:04d}-{_safe_stage(stage)}.xml"
            original = argv
            argv = _with_junit_report(argv, report, stage)
            kind = "environment" if stage.startswith(("uv-lock:", "uv-sync:")) else (
                "test" if argv != original else "check")
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
            result: dict[str, Any] = {
                "stage": stage,
                "kind": kind,
                "argv": resolved,
                "cwd": str(cwd.relative_to(root) or Path(".")),
                "exit_code": exit_code,
                "classification": classification,
                "log": log_path,
            }
            if kind == "test":
                counts = junit_counts(report)
                result["test_cases"] = counts
                if exit_code == 0 and (counts is None or counts["tests"] == 0):
                    # A green exit with no test case proves nothing (wrong module, empty selection).
                    exit_code = NO_TESTS
                    result["exit_code"] = exit_code
                    result["classification"] = "NO_TESTS_EXECUTED"
            results.append(result)
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
    parser.add_argument("--profile")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    catalog = load_catalog(args.catalog)
    catalog.validate(args.root)
    scope = json.loads(args.scope.read_text(encoding="utf-8"))
    scope_components = tuple(scope.get("components", []))
    selected = scope_components
    if args.profile:
        if args.profile not in catalog.profiles:
            parser.error(f"unknown catalog profile: {args.profile}")
        allowed = catalog.profiles[args.profile]
        selected = (
            allowed
            if scope.get("full_validation_reason") and scope.get("full_validation_reason") != "non_runtime_paths_only"
            else tuple(component for component in selected if component in allowed)
        )
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
    status, reason, exit_code, results = selection_status(scope, scope_components, selected, args.profile)
    if status is None:
        outcome = execute_components(catalog, selected, args.root, args.evidence_dir, metadata=metadata)
        exit_code, results = outcome.exit_code, outcome.results
        status = "PASS" if exit_code == 0 else "FAILED"
        reason = None if exit_code == 0 else results[-1].get("classification") if results else "RUNNER_ERROR"
    else:
        writer = EvidenceWriter(args.evidence_dir, {**metadata, "components": list(selected)})
        writer.finalize(status=status, results=results)
    write_verdict(args.evidence_dir, status=status, reason=reason, scope_components=scope_components,
                  selected=selected, profile=args.profile, results=results)
    return exit_code


def selection_status(scope: dict, scope_components: tuple[str, ...], selected: tuple[str, ...],
                     profile: str | None) -> tuple[str | None, str | None, int, list[dict[str, Any]]]:
    """Decide the outcome of a run that selects nothing; ``None`` means run the selection."""

    if selected:
        return None, None, 0, []
    if scope.get("full_validation_reason") == "non_runtime_paths_only":
        return "NOT_APPLICABLE", "documentation-only change: no runtime component to test", 0, []
    if profile and scope_components:
        return "NOT_APPLICABLE", f"profile {profile} owns none of the changed components {list(scope_components)}", 0, []
    return "FAILED", "EMPTY_SCOPE: no component selected and no reason (empty diff or wrong target ref)", EMPTY_SCOPE, []


def write_verdict(evidence_dir: Path, *, status: str, reason: str | None, scope_components: tuple[str, ...],
                  selected: tuple[str, ...], profile: str | None, results: list[dict[str, Any]]) -> dict[str, Any]:
    tests = [item for item in results if item.get("kind") == "test"]
    cases = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for item in tests:
        for key, value in (item.get("test_cases") or {}).items():
            cases[key] += value
    verdict = {
        "status": status, "reason": reason, "profile": profile,
        "scope_components": list(scope_components), "selected_components": list(selected),
        "commands_executed": len(results), "test_commands": len(tests),
        "check_commands": sum(1 for item in results if item.get("kind") == "check"),
        "test_cases": cases,
        "executed": [{"stage": item.get("stage"), "kind": item.get("kind"), "exit_code": item.get("exit_code"),
                      "classification": item.get("classification"), "test_cases": item.get("test_cases")}
                     for item in results],
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "ci-verdict.json").write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
