"""Render or execute the bounded Bronze-to-Silver Dataproc batch."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from data_platform.storage.scripts.config import silver_tables
from ops.gcp.dataproc_batch_properties import (
    DEFAULT_PERFORMANCE_PROFILE,
    add_performance_profile_argument,
    build_dataproc_batch_properties,
)
from ops.gcp.package_python_job import build_python_archive
from ops.gcp.submit_dataproc_delta_smoke import (
    TERRAFORM_DEV_ROOT,
    dependency_jar_paths,
    ensure_delta_dependencies,
)
from ops.gcp.submit_source_to_bronze import (
    CloudRuntime,
    REPOSITORY_ROOT,
    _validate_id,
    _validate_ttl,
    load_cloud_runtime,
)


SILVER_SCRIPT = (
    REPOSITORY_ROOT / "data_platform/processing/spark/silver/silver_pipeline.py"
)
DEFAULT_ARCHIVE = REPOSITORY_ROOT / "tmp/dataproc/ewallet-python.zip"
GCP_RUNTIME_PROFILE = REPOSITORY_ROOT / "configs/environments/gcp/platform.yaml"


def run_roots(
    runtime: CloudRuntime,
    *,
    source_bronze_run_id: str,
    run_id: str,
) -> tuple[str, str]:
    source_bronze_run_id = _validate_id(
        source_bronze_run_id, "source_bronze_run_id"
    )
    run_id = _validate_id(run_id, "run_id")
    if run_id == source_bronze_run_id:
        raise ValueError("Silver run_id must be different from source Bronze run")
    base = f"gs://{runtime.data_bucket}/lakehouse/runs"
    return (
        f"{base}/{source_bronze_run_id}/bronze",
        f"{base}/{run_id}/silver",
    )


def assert_silver_prefix_absent(
    runtime: CloudRuntime,
    *,
    run_id: str,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Refuse an execute request when its immutable Silver prefix exists."""

    _, silver_root = run_roots(
        runtime,
        source_bronze_run_id="source-check",
        run_id=run_id,
    )
    completed = runner(
        ["gcloud", "storage", "ls", f"{silver_root}/**"],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        raise ValueError(
            f"Silver output prefix already exists and is immutable: {silver_root}"
        )
    missing_prefix = (
        completed.returncode == 1
        and "matched no objects" in completed.stderr.lower()
    )
    if completed.returncode != 0 and not missing_prefix:
        raise RuntimeError(
            "Could not verify that the Silver output prefix is absent: "
            f"{completed.stderr.strip()}"
        )


def build_submission_command(
    runtime: CloudRuntime,
    *,
    dataset_version: str,
    source_bronze_run_id: str,
    run_id: str,
    script_path: Path = SILVER_SCRIPT,
    archive_path: Path = DEFAULT_ARCHIVE,
    runtime_profile_path: Path = GCP_RUNTIME_PROFILE,
    jar_paths: Sequence[Path] | None = None,
    ttl: str = "2h",
    performance_profile: str = DEFAULT_PERFORMANCE_PROFILE,
) -> tuple[str, ...]:
    dataset_version = _validate_id(dataset_version, "dataset_version")
    run_id = _validate_id(run_id, "run_id")
    ttl = _validate_ttl(ttl)
    bronze_root, silver_root = run_roots(
        runtime,
        source_bronze_run_id=source_bronze_run_id,
        run_id=run_id,
    )
    tables = silver_tables(include_fraud_labels=True)
    jars = tuple(jar_paths or dependency_jar_paths())
    properties = ",".join(
        build_dataproc_batch_properties(
            performance_tier=runtime.performance_tier,
            data_bucket=runtime.data_bucket,
            profile=performance_profile,
        )
    )
    return (
        "gcloud",
        "dataproc",
        "batches",
        "submit",
        "pyspark",
        str(script_path),
        f"--batch={run_id}",
        f"--project={runtime.project_id}",
        f"--region={runtime.region}",
        "--version=2.2",
        f"--service-account={runtime.service_account_email}",
        f"--subnet={runtime.subnet}",
        "--jars=" + ",".join(str(path) for path in jars),
        f"--py-files={archive_path}",
        f"--files={runtime_profile_path}",
        f"--deps-bucket=gs://{runtime.temporary_bucket}",
        f"--staging-bucket={runtime.temporary_bucket}",
        f"--ttl={ttl}",
        "--labels=component=bronze-silver,phase=s1a",
        f"--properties={properties}",
        "--",
        "--environment=gcp",
        "--mode=optimized",
        "--include-fraud-labels",
        f"--expected-table-count={len(tables)}",
        f"--dataset-version={dataset_version}",
        f"--source-bronze-run-id={source_bronze_run_id}",
        f"--run-id={run_id}",
        f"--project-id={runtime.project_id}",
        f"--region={runtime.region}",
        f"--data-bucket={runtime.data_bucket}",
        f"--runtime-config-file={runtime_profile_path.name}",
        f"--bronze-root={bronze_root}",
        f"--silver-root={silver_root}",
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-bronze-run-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--ttl", default="2h")
    add_performance_profile_argument(parser)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def _print_plan(
    runtime: CloudRuntime,
    *,
    dataset_version: str,
    source_bronze_run_id: str,
    run_id: str,
    command: Sequence[str],
) -> None:
    bronze_root, silver_root = run_roots(
        runtime,
        source_bronze_run_id=source_bronze_run_id,
        run_id=run_id,
    )
    table_names = silver_tables(include_fraud_labels=True)
    print(f"Dataset version: {dataset_version}")
    print(f"Source Bronze run: {source_bronze_run_id}")
    print(f"Bronze input root: {bronze_root}")
    print(f"Silver output root: {silver_root}")
    print(f"Project: {runtime.project_id}")
    print(f"Region: {runtime.region}")
    print(f"Subnet: {runtime.subnet}")
    print(f"Service account: {runtime.service_account_email}")
    print(f"Tables ({len(table_names)}): {', '.join(table_names)}")
    print("Dataproc batch command:")
    print(shlex.join(command))


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    args = parse_args(argv)
    runtime = load_cloud_runtime(_load_outputs(args.terraform_output_json, runner))
    jar_paths = dependency_jar_paths()
    archive = DEFAULT_ARCHIVE
    if args.execute:
        assert_silver_prefix_absent(runtime, run_id=args.run_id, runner=runner)
        jar_paths = ensure_delta_dependencies()
        archive = build_python_archive(REPOSITORY_ROOT, DEFAULT_ARCHIVE)
    command = build_submission_command(
        runtime,
        dataset_version=args.dataset_version,
        source_bronze_run_id=args.source_bronze_run_id,
        run_id=args.run_id,
        ttl=args.ttl,
        performance_profile=args.performance_profile,
        archive_path=archive,
        jar_paths=jar_paths,
    )
    if not args.execute:
        print("DRY RUN — no Dataproc batch was submitted")
        _print_plan(
            runtime,
            dataset_version=args.dataset_version,
            source_bronze_run_id=args.source_bronze_run_id,
            run_id=args.run_id,
            command=command,
        )
        return 0
    runner(command, check=True)
    print(f"S1A Bronze-to-Silver batch submitted: {args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
