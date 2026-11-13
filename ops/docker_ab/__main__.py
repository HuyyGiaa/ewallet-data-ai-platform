"""Run the Docker A/B benchmark and write its evidence.

Usage (from the repository root)::

    python3 -m ops.docker_ab --run-id 20261112a --output-dir <new directory>

Writes ``result.json`` (all measurements), ``summary.txt`` (the compact table for
DK03/DK04), one build log per variant and the Compose logs. The output directory
must not exist yet. Exit code 0 means every variant built, every run passed with
one shared report SHA-256, and Compose completed with exit code 0.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import COMPOSE_PROJECT, REPORT_SCHEMA
from .docker_cli import DockerCli, Mount
from .measure import CONTAINER_EVIDENCE, CONTAINER_SPEC, attribution, functional_run, image_metadata, security_properties
from .report import render_text
from .variants import EVIDENCE_DIR, FULL_BASE, SLIM_BASE, SPEC_FILE, VARIANTS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path("infra/docker/compose.edai1.yml")
COMPOSE_SERVICE = "campaign-verifier"


def git_state(root: Path) -> dict[str, Any]:
    """Tool Git SHA and whether tracked files differ from it."""

    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, capture_output=True, check=True)
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                            text=True, capture_output=True, check=True)
    return {"tool_git_sha": sha.stdout.strip(), "tracked_tree_clean": not status.stdout.strip()}


def run_compose(cli: DockerCli, output: Path) -> dict[str, Any]:
    """Validate, build and run the Compose service once; record residual containers."""

    config = cli.compose(COMPOSE_FILE, "config", log=output / "compose_config.txt", check=False)
    build = cli.compose(COMPOSE_FILE, "build", "--no-cache", COMPOSE_SERVICE,
                        log=output / "compose_build.txt", check=False)
    run = cli.compose(COMPOSE_FILE, "run", "--rm", COMPOSE_SERVICE, log=output / "compose_run.txt", check=False)
    lines = [line for line in run.stdout.splitlines() if line.strip().startswith("{")]
    summary = json.loads(lines[-1]) if lines else {}
    return {
        "project": COMPOSE_PROJECT,
        "service": COMPOSE_SERVICE,
        "config_valid": config.returncode == 0,
        "build_exit_code": build.returncode,
        "run_exit_code": run.returncode,
        "verdict": summary.get("verdict"),
        "report_sha256": summary.get("report_sha256"),
        "residual_containers": cli.project_containers(),
    }


def benchmark(cli: DockerCli, *, run_id: str, output: Path, repetitions: int, with_compose: bool) -> dict[str, Any]:
    """Build and measure every variant; return the full result document."""

    mounts = [Mount(REPOSITORY_ROOT / EVIDENCE_DIR, CONTAINER_EVIDENCE),
              Mount(REPOSITORY_ROOT / SPEC_FILE, CONTAINER_SPEC)]
    spec = json.loads((REPOSITORY_ROOT / SPEC_FILE).read_text(encoding="utf-8"))
    started = datetime.now(timezone.utc).isoformat()
    versions = cli.versions()
    for reference in (FULL_BASE, SLIM_BASE):
        cli.pull(reference)
    variants: dict[str, Any] = {}
    for item in VARIANTS:
        tag = item.tag(run_id)
        seconds = cli.build(item, tag, log=output / f"build_{item.name}.log")
        variants[item.name] = {
            "tag": tag,
            "role": item.role,
            "packaging": item.packaging,
            "dockerfile": str(item.dockerfile),
            "build_args": dict(item.build_args),
            "build_seconds": round(seconds, 3),
            "image": image_metadata(cli, tag, item.base_image),
            "functional": functional_run(cli, tag, mounts, repetitions),
            "security": security_properties(cli, tag),
        }
    fingerprints = {name: value["functional"]["report_sha256"] for name, value in variants.items()}
    equivalent = (len(set(fingerprints.values())) == 1 and None not in fingerprints.values()
                  and all(value["functional"]["all_passed"] for value in variants.values()))
    result: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "run_id": run_id,
        "started_at": started,
        **git_state(REPOSITORY_ROOT),
        "docker": versions,
        "cache_policy": "base images pre-pulled by digest; every build uses --no-cache --pull=false",
        "workload": {
            "spec_file": str(SPEC_FILE),
            "evidence_dir": str(EVIDENCE_DIR),
            "benchmark_id": spec["benchmark_id"],
            "dataset_version": spec["dataset_version"],
            "source_git_sha": spec["source_git_sha"],
            "repetitions_per_variant": repetitions,
        },
        "variants": variants,
        "functional_equivalence": {
            "equivalent": equivalent,
            "variants": len(variants),
            "report_sha256": next(iter(fingerprints.values())) if equivalent else fingerprints,
        },
        "size_metric": "content_size_bytes = docker image inspect .Size (compressed layer content, containerd store)",
        "attribution": attribution({name: value["image"]["content_size_bytes"] for name, value in variants.items()}),
    }
    if with_compose:
        result["compose"] = run_compose(cli, output)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


def passed(result: dict[str, Any]) -> bool:
    """Overall gate of the run."""

    compose = result.get("compose")
    compose_ok = compose is None or (
        compose["config_valid"] and compose["build_exit_code"] == 0 and compose["run_exit_code"] == 0
        and compose["verdict"] == "PASS" and not compose["residual_containers"]
        and compose["report_sha256"] == result["functional_equivalence"]["report_sha256"])
    return bool(result["functional_equivalence"]["equivalent"]) and compose_ok


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""

    parser = argparse.ArgumentParser(prog="python3 -m ops.docker_ab", description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True, help="lowercase id used in image tags, e.g. 20261112a")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--skip-compose", action="store_true")
    parser.add_argument("--minimum-free-gib", type=float, default=6.0)
    args = parser.parse_args(argv)
    if args.output_dir.exists():
        parser.error(f"{args.output_dir} exists; use a new directory per run")
    free_gib = shutil.disk_usage(REPOSITORY_ROOT).free / 2**30
    if free_gib < args.minimum_free_gib:
        parser.error(f"only {free_gib:.1f} GiB free; need {args.minimum_free_gib} GiB")
    args.output_dir.mkdir(parents=True)
    cli = DockerCli(REPOSITORY_ROOT)
    result = benchmark(cli, run_id=args.run_id, output=args.output_dir, repetitions=args.repetitions,
                       with_compose=not args.skip_compose)
    result["disk_free_gib_before"] = round(free_gib, 1)
    (args.output_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text = render_text(result)
    (args.output_dir / "summary.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if passed(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
