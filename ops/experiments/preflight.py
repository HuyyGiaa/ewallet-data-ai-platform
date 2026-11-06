"""Capture an experiment's provenance before it runs.

Writes ``manifest.json`` and ``hypothesis.md``. Reads only: it asks git, kubectl and the
filesystem what is there. A value it cannot read is recorded as ``null`` with the reason,
never guessed. It writes no evidence that a run has not produced.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ops.experiments.spec import ExperimentSpec


Runner = Callable[[Sequence[str]], tuple[int, str]]


def run_command(command: Sequence[str]) -> tuple[int, str]:
    """Run a read-only command and return its exit code and output."""

    result = subprocess.run(list(command), capture_output=True, text=True, check=False, timeout=60)
    return result.returncode, result.stdout.strip() or result.stderr.strip()


def fixture_hash(paths: Sequence[Path]) -> str | None:
    """Return one SHA-256 over the fixtures, or ``None`` when there are none."""

    if not paths:
        return None
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _capture(runner: Runner, command: Sequence[str], problems: list[str], label: str) -> str | None:
    code, output = runner(command)
    if code != 0:
        problems.append(f"{label}: {output[:200]}")
        return None
    return output


def capture(
    spec: ExperimentSpec,
    *,
    kube_context: str,
    namespace: str,
    settings: Mapping[str, object],
    fixtures: Sequence[Path] = (),
    runner: Runner = run_command,
    disk_path: Path = Path("."),
) -> dict[str, object]:
    """Build the experiment manifest.

    Args:
        spec: Experiment rules.
        kube_context: Explicit context; the kubeconfig default is never used.
        namespace: Namespace under test.
        settings: Benchmark parameters, image digests and model revision.
        fixtures: Workload files to hash.
        runner: Command runner, replaced in tests.
        disk_path: Filesystem whose free space is recorded.

    Returns:
        The manifest, with ``capture_problems`` listing anything unreadable.
    """

    problems: list[str] = []
    kubectl = ["kubectl", "--context", kube_context]
    return {
        "schema_version": "ewallet-experiment-manifest-v1",
        "experiment_id": spec.experiment_id,
        "hypothesis": spec.hypothesis,
        "variable": spec.variable,
        "arms": {"baseline": spec.baseline, "optimized": spec.optimized},
        "controls": list(spec.controls),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "source_sha": _capture(runner, ["git", "rev-parse", "HEAD"], problems, "source_sha"),
        "source_dirty": bool(_capture(runner, ["git", "status", "--porcelain"], problems, "source_dirty")),
        "kube_context": kube_context,
        "namespace": namespace,
        "nodes": _capture(runner, [*kubectl, "get", "nodes", "-o", "wide"], problems, "nodes"),
        "pods": _capture(runner, [*kubectl, "-n", namespace, "get", "pods", "-o", "wide"], problems, "pods"),
        "settings": dict(settings),
        "workload_fixture_sha256": fixture_hash(fixtures),
        "disk_free_bytes": shutil.disk_usage(disk_path).free,
        "deviations": [],
        "capture_problems": problems,
    }


def write_preflight(spec: ExperimentSpec, manifest: Mapping[str, object], directory: Path) -> None:
    """Create ``manifest.json`` and ``hypothesis.md``; existing evidence is never replaced."""

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
    with (directory / "hypothesis.md").open("x", encoding="utf-8") as stream:
        stream.write(f"# {spec.experiment_id}\n\n{spec.hypothesis}\n\nVariable: {spec.variable}\n")


def finish_manifest(directory: Path, deviations: Sequence[str]) -> None:
    """Record the end time and every deviation; earlier fields stay as captured."""

    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest["deviations"] = [*manifest["deviations"], *deviations]
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
