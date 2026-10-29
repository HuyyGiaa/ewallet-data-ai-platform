"""Render or execute a read-only Dataproc Spark A/B output summary scan."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from data_platform.config.spark_benchmark import (
    load_pair_manifest,
    method2_runtime_properties,
    validate_pair_member,
)
from data_platform.config.spark_profiles import (
    require_profile_binding,
    validate_git_sha,
)
from ops.gcp.dataproc_batch_properties import (
    build_dataproc_batch_properties,
    serialize_dataproc_batch_properties,
)
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
    _validate_id,
    load_cloud_runtime,
)
from ops.gcp.summarize_spark_output import expected_output_root, stage_run_kind


SUMMARY_SCRIPT = REPOSITORY_ROOT / "ops/gcp/summarize_spark_output.py"


def build_submission_command(
    runtime: CloudRuntime,
    *,
    batch_id: str,
    benchmark_id: str,
    spark_profile: str,
    stage: str,
    output_run_id: str,
    dataset_version: str,
    git_sha: str,
    summary_uri: str,
    source_run_ids: Sequence[str],
    pair_manifest: Mapping[str, object],
    pair_manifest_path: Path,
    script_path: Path = SUMMARY_SCRIPT,
    archive_path: Path = DEFAULT_ARCHIVE,
    jar_paths: Sequence[Path] | None = None,
) -> tuple[str, ...]:
    batch_id = _validate_id(batch_id, "batch_id")
    dataset_version = _validate_id(dataset_version, "dataset_version")
    git_sha = validate_git_sha(git_sha)
    if not source_run_ids or any(not value for value in source_run_ids):
        raise ValueError("source_run_ids must be explicit")
    require_profile_binding(
        output_run_id,
        benchmark_id=benchmark_id,
        profile=spark_profile,
        stage=stage_run_kind(stage),
    )
    root = expected_output_root(
        data_bucket=runtime.data_bucket,
        output_run_id=output_run_id,
        stage=stage,
    )
    validate_pair_member(
        pair_manifest,
        spark_profile=spark_profile,
        benchmark_id=benchmark_id,
        stage=stage,
        dataset_version=dataset_version,
        git_sha=git_sha,
        upstream_run_ids=source_run_ids,
        output_run_id=output_run_id,
        output_root=root,
    )
    expected_summary_uri = (
        f"gs://{runtime.data_bucket}/evidence/spark_benchmark/"
        f"{benchmark_id}/{spark_profile}/{stage}/"
        f"{output_run_id}-summary.json"
    )
    if summary_uri != expected_summary_uri:
        raise ValueError(
            f"summary URI must be the immutable profile path {expected_summary_uri}"
        )
    property_items = list(
        build_dataproc_batch_properties(
            performance_tier=runtime.performance_tier,
            data_bucket=runtime.data_bucket,
            profile=spark_profile,
            stage=stage,
        )
    )
    property_items.extend(
        f"{key}={value}"
        for key, value in method2_runtime_properties(
            pair_manifest, spark_profile
        ).items()
    )
    properties = serialize_dataproc_batch_properties(property_items)
    jars = tuple(jar_paths or dependency_jar_paths())
    return (
        "gcloud", "dataproc", "batches", "submit", "pyspark", str(script_path),
        f"--batch={batch_id}", f"--project={runtime.project_id}",
        f"--region={runtime.region}", "--version=2.2",
        f"--service-account={runtime.service_account_email}",
        f"--subnet={runtime.subnet}",
        "--jars=" + ",".join(str(path) for path in jars),
        f"--py-files={archive_path}",
        f"--files={pair_manifest_path}",
        f"--deps-bucket=gs://{runtime.temporary_bucket}",
        f"--staging-bucket={runtime.temporary_bucket}", "--ttl=8h",
        (
            "--labels=component=ab-validator,"
            f"benchmark_id={benchmark_id},spark_profile={spark_profile},git_sha={git_sha}"
        ),
        f"--properties={properties}", "--",
        f"--root={root}", f"--data-bucket={runtime.data_bucket}",
        f"--stage={stage}", f"--benchmark-id={benchmark_id}",
        f"--spark-profile={spark_profile}", f"--output-run-id={output_run_id}",
        f"--dataset-version={dataset_version}", f"--git-sha={git_sha}",
        f"--pair-manifest={pair_manifest_path.name}",
        "--expected-logical-transactions=369600000",
        "--expected-physical-transactions=376992000",
        *(f"--source-run-id={value}" for value in source_run_ids),
        f"--output={summary_uri}",
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


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--spark-profile", choices=("baseline", "optimized"), required=True)
    parser.add_argument("--stage", choices=("dp1-bronze", "dp2-silver", "dp2-gold", "dp3-features"), required=True)
    parser.add_argument("--output-run-id", required=True)
    parser.add_argument("--dataset-version", default="canonical-100gb-v1")
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--summary-uri", required=True)
    parser.add_argument("--source-run-id", action="append", dest="source_run_ids", required=True)
    parser.add_argument("--pair-manifest", type=Path, required=True)
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    runtime = load_cloud_runtime(_load_outputs(args.terraform_output_json, runner))
    jars = dependency_jar_paths()
    archive = DEFAULT_ARCHIVE
    if args.execute:
        jars = ensure_delta_dependencies()
        archive = build_python_archive(REPOSITORY_ROOT, DEFAULT_ARCHIVE)
    command = build_submission_command(
        runtime,
        batch_id=args.batch_id,
        benchmark_id=args.benchmark_id,
        spark_profile=args.spark_profile,
        stage=args.stage,
        output_run_id=args.output_run_id,
        dataset_version=args.dataset_version,
        git_sha=args.git_sha,
        summary_uri=args.summary_uri,
        source_run_ids=args.source_run_ids,
        pair_manifest=load_pair_manifest(args.pair_manifest),
        pair_manifest_path=args.pair_manifest,
        archive_path=archive,
        jar_paths=jars,
    )
    if not args.execute:
        print("DRY RUN — no validation batch was submitted")
        print(shlex.join(command))
        return 0
    runner(command, check=True)
    print(f"Spark output summary batch submitted: {args.batch_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
