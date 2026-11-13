"""Render or execute the isolated GCS + Delta storage-layout benchmark."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from data_platform.config.dataset_identity import require_approved_dataset
from data_platform.config.spark_profiles import (
    OPTIMIZED_SPARK_PROFILE,
    parse_benchmark_run_id,
    validate_git_sha,
)
from data_platform.storage.cloud_optimization import validate_benchmark_paths
from ops.gcp.dataproc_batch_properties import build_dataproc_batch_properties
from ops.gcp.package_python_job import build_python_archive
from ops.gcp.submit_dataproc_delta_smoke import (
    TERRAFORM_DEV_ROOT,
    dependency_jar_paths,
    ensure_delta_dependencies,
)
from ops.gcp.submit_source_to_bronze import (
    CloudRuntime,
    DEFAULT_ARCHIVE,
    REPOSITORY_ROOT,
    apply_dataset_bucket,
    load_cloud_runtime,
)


JOB_SCRIPT = REPOSITORY_ROOT / "ops/gcp/storage_benchmark_job.py"
PLAN_SCHEMA = 2
SAFE_BENCHMARK_ID = re.compile(r"^[a-z][a-z0-9-]{2,62}[a-z0-9]$")
SPARK_BENCHMARK_ID = re.compile(r"^ab-[0-9]{14}$")
STAGE_LAYER = {
    "dp2-silver": "silver",
    "dp2-gold": "gold",
    "dp3-features": "gold",
}


def benchmark_root(runtime: CloudRuntime, benchmark_id: str) -> str:
    if not SAFE_BENCHMARK_ID.fullmatch(benchmark_id):
        raise ValueError("benchmark_id must be a safe lowercase GCP identifier")
    return (
        f"gs://{runtime.data_bucket}/lakehouse/benchmarks/storage/"
        f"{benchmark_id}"
    )


def _validate_plan(plan: Mapping[str, Any]) -> None:
    """Refuse a plan that is not a schema-2 plan bound to a verified 5GB campaign."""

    required = {"schema_version", "benchmark_id", "dataset_version", "git_sha", "source_spark_git_sha",
                "source_campaign", "source_run_ids", "spark_benchmark_id", "benchmark_root", "tables"}
    if not required.issubset(plan):
        raise ValueError(f"storage plan is missing: {sorted(required - set(plan))}")
    if plan["schema_version"] != PLAN_SCHEMA:
        raise ValueError(f"storage plan schema {plan['schema_version']!r} is not {PLAN_SCHEMA}")
    require_approved_dataset(str(plan["dataset_version"]))
    validate_git_sha(str(plan["git_sha"]))
    validate_git_sha(str(plan["source_spark_git_sha"]))
    campaign = plan["source_campaign"]
    if (campaign.get("benchmark_id") != plan["spark_benchmark_id"]
            or campaign.get("spark_profile") != "optimized"
            or campaign.get("source_spark_git_sha") != plan["source_spark_git_sha"]
            or campaign.get("dataset_version") != plan["dataset_version"]):
        raise ValueError("plan source campaign identity is inconsistent")
    if not SPARK_BENCHMARK_ID.fullmatch(str(plan["spark_benchmark_id"])):
        raise ValueError("plan has an invalid Spark benchmark ID")
    run_ids = dict(campaign.get("run_ids") or {})
    for run_id in run_ids.values():
        parsed = parse_benchmark_run_id(str(run_id))
        if parsed["benchmark_id"] != plan["spark_benchmark_id"] or parsed["spark_profile"] != "optimized":
            raise ValueError("source run IDs do not belong to the optimized campaign profile")
    selected = [table for table in plan["tables"] if table.get("selected")] if isinstance(plan["tables"], list) else []
    if not selected:
        raise ValueError("plan contains no selected table")
    root = urlsplit(str(plan["benchmark_root"]))
    for table in selected:
        stage = str(table.get("stage", ""))
        run_id = run_ids.get(stage)
        expected_uri = f"gs://{root.netloc}/lakehouse/runs/{run_id}/{STAGE_LAYER.get(stage)}/{table.get('table')}"
        if run_id is None or str(table.get("uri", "")) != expected_uri:
            raise ValueError("selected table is outside the verified campaign lineage")
        if (table.get("optimization") or {}).get("action") != "ZORDER" or not table.get("compacted_uri"):
            raise ValueError("every table needs the three-arm compaction/Z-order ablation")
        if not (table.get("workload_parameters") or {}).get("merchant_id"):
            raise ValueError("table lacks data-derived workload parameters")
        validate_benchmark_paths(
            source_uri=str(table["uri"]),
            benchmark_root=str(plan["benchmark_root"]),
            baseline_uri=str(table.get("baseline_uri", "")),
            optimized_uri=str(table.get("optimized_uri", "")),
            compacted_uri=str(table["compacted_uri"]),
        )


def _assert_tool_git_state(
    expected_sha: str,
    git_runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Ensure the packaged tracked sources match the immutable plan exactly."""

    expected_sha = validate_git_sha(expected_sha)
    completed = git_runner(
        ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    )
    if completed.stdout.strip() != expected_sha:
        raise ValueError("tool Git SHA differs from the immutable storage plan")
    status = git_runner(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "status",
            "--porcelain",
            "--untracked-files=no",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    if status.stdout.strip():
        raise ValueError("tracked worktree must be clean before packaging benchmark code")
    tracked = git_runner(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "ls-files",
            "--",
            "data_platform",
            "ops",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    tracked_files = set(tracked.stdout.splitlines())
    package_candidates = {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for root in (REPOSITORY_ROOT / "data_platform", REPOSITORY_ROOT / "ops")
        for path in root.rglob("*.py")
    }
    outside_git = sorted(package_candidates - tracked_files)
    if outside_git:
        raise ValueError(
            "Python package candidates outside Git would differ from TOOL_GIT_SHA: "
            + ",".join(outside_git)
        )


def build_submission_command(
    runtime: CloudRuntime,
    *,
    benchmark_id: str,
    dataset_version: str,
    source_run_ids: Sequence[str],
    git_sha: str,
    plan_path: Path,
    archive_path: Path = DEFAULT_ARCHIVE,
    jar_paths: Sequence[Path] | None = None,
    ttl: str = "8h",
) -> tuple[str, ...]:
    if runtime.performance_tier != "standard":
        raise ValueError("storage benchmark is pinned to Standard tier")
    if ttl != "8h":
        raise ValueError("storage benchmark TTL is frozen at 8h")
    validate_git_sha(git_sha)
    if not source_run_ids:
        raise ValueError("source run IDs are required")
    for value in source_run_ids:
        parsed = parse_benchmark_run_id(value)
        if parsed["spark_profile"] != "optimized":
            raise ValueError("all source run IDs must identify optimized Spark lineage")
    root = benchmark_root(runtime, benchmark_id)
    batch_id = f"{benchmark_id}-storage"
    if len(batch_id) > 63:
        raise ValueError("benchmark_id is too long for a Dataproc batch ID")
    properties = ",".join(
        build_dataproc_batch_properties(
            performance_tier="standard",
            data_bucket=runtime.data_bucket,
            profile=OPTIMIZED_SPARK_PROFILE,
            # Storage candidates are selected from the verified DP2 Gold
            # output.  The shared profile API is stage-aware; leaving this
            # implicit would silently mix an unrelated stage contract.
            stage="dp2-gold",
        )
    )
    jars = tuple(jar_paths or dependency_jar_paths())
    return (
        "gcloud",
        "dataproc",
        "batches",
        "submit",
        "pyspark",
        str(JOB_SCRIPT),
        f"--batch={batch_id}",
        f"--project={runtime.project_id}",
        f"--region={runtime.region}",
        "--version=2.2",
        f"--service-account={runtime.service_account_email}",
        f"--subnet={runtime.subnet}",
        "--jars=" + ",".join(str(path) for path in jars),
        f"--py-files={archive_path}",
        f"--files={plan_path}",
        f"--deps-bucket=gs://{runtime.temporary_bucket}",
        f"--staging-bucket={runtime.temporary_bucket}",
        f"--ttl={ttl}",
        (
            "--labels=component=storage-benchmark,phase=storage-ab,"
            f"benchmark_id={benchmark_id}"
        ),
        f"--properties={properties}",
        "--",
        f"--plan={plan_path.name}",
        f"--benchmark-root={root}",
        f"--dataset-version={dataset_version}",
        f"--git-sha={git_sha}",
    )


def _load_outputs(path: Path | None, runner: Callable[..., Any]) -> Mapping[str, Any]:
    if path is not None:
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        completed = runner(
            ["terraform", f"-chdir={TERRAFORM_DEV_ROOT}", "output", "-json"],
            check=True,
            text=True,
            capture_output=True,
        )
        value = json.loads(completed.stdout)
    if not isinstance(value, Mapping):
        raise ValueError("Terraform output JSON must be an object")
    return value


def _assert_output_absent(
    root: str, runner: Callable[..., Any]
) -> None:
    completed = runner(
        ["gcloud", "storage", "ls", f"{root}/**"],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        raise ValueError(f"immutable storage benchmark prefix exists: {root}")
    missing = completed.returncode == 1 and "matched no objects" in completed.stderr.lower()
    if completed.returncode != 0 and not missing:
        raise RuntimeError(f"could not verify benchmark prefix: {completed.stderr.strip()}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
    git_runner: Callable[..., Any] = subprocess.run,
) -> int:
    args = parse_args(argv)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if not isinstance(plan, Mapping):
        raise ValueError("storage plan must be a JSON object")
    _validate_plan(plan)
    _assert_tool_git_state(str(plan["git_sha"]), git_runner)
    runtime = apply_dataset_bucket(
        load_cloud_runtime(_load_outputs(args.terraform_output_json, runner)), str(plan["dataset_version"]))
    root = benchmark_root(runtime, str(plan["benchmark_id"]))
    if plan["benchmark_root"] != root:
        raise ValueError("plan benchmark root does not match the dataset's data bucket")
    jars = dependency_jar_paths()
    archive = DEFAULT_ARCHIVE
    if args.execute:
        _assert_output_absent(root, runner)
        jars = ensure_delta_dependencies()
        archive = build_python_archive(REPOSITORY_ROOT, DEFAULT_ARCHIVE)
    command = build_submission_command(
        runtime,
        benchmark_id=str(plan["benchmark_id"]),
        dataset_version=str(plan["dataset_version"]),
        source_run_ids=tuple(str(value) for value in plan["source_run_ids"]),
        git_sha=str(plan["git_sha"]),
        plan_path=args.plan,
        archive_path=archive,
        jar_paths=jars,
    )
    print("EXECUTION: " + ("ENABLED" if args.execute else "DRY_RUN"))
    print(f"SPARK_BENCHMARK_ID: {plan['spark_benchmark_id']}")
    print(f"SOURCE_SPARK_GIT_SHA: {plan['source_spark_git_sha']}")
    print(f"TOOL_GIT_SHA: {plan['git_sha']}")
    print(f"SOURCE_RUN_IDS: {','.join(plan['source_run_ids'])}")
    print(f"BENCHMARK_ROOT: {root}")
    print("SELECTED_TABLES: " + ",".join(str(t.get("table")) for t in plan["tables"] if t.get("selected")))
    for table in plan["tables"]:
        if not table.get("selected"):
            continue
        layout = table.get("file_size_bytes") or {}
        print(f"TABLE: {table['stage']}.{table['table']}")
        print(f"  INPUT: {table['uri']}@v{table['delta_version']}")
        print(f"  BASELINE: {table['baseline_uri']}")
        print(f"  COMPACTED: {table['compacted_uri']}")
        print(f"  OPTIMIZED: {table['optimized_uri']}")
        print(f"  WORKLOAD: {json.dumps(table['workload_parameters'], sort_keys=True)}")
        print(
            "  CURRENT_LAYOUT: "
            f"files={table['file_count']},bytes={table['total_bytes']},"
            f"median={layout.get('median')},p95={layout.get('p95')}"
        )
        print(f"  PARTITION_DECISION: {json.dumps(table['partition_decision'], sort_keys=True)}")
        print(f"  OPTIMIZATION: {json.dumps(table['optimization'], sort_keys=True)}")
        print(f"  ESTIMATED_REWRITE_BYTES: {json.dumps(table['estimated_rewrite_bytes'], sort_keys=True)}")
    print("COMMAND:")
    print(shlex.join(command))
    if args.execute:
        runner(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
