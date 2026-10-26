"""Render or execute the bounded DP3 historical feature Dataproc batch."""

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
from data_platform.storage.scripts.config import HISTORICAL_FEATURE_TABLES
from ops.gcp.dataproc_batch_properties import (
    DEFAULT_PERFORMANCE_PROFILE,
    add_performance_profile_argument,
    add_spark_profile_argument,
    build_dataproc_batch_properties,
    profile_summary,
    resolve_submission_profile,
)
from data_platform.config.spark_profiles import (
    require_benchmark_pair,
    require_profile_binding,
    validate_git_sha,
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


FEATURE_SCRIPT = (
    REPOSITORY_ROOT
    / "data_platform/processing/spark/gold/historical_feature_pipeline.py"
)
DEFAULT_ARCHIVE = REPOSITORY_ROOT / "tmp/dataproc/ewallet-python.zip"
GCP_RUNTIME_PROFILE = REPOSITORY_ROOT / "configs/environments/gcp/platform.yaml"


def run_roots(
    runtime: CloudRuntime,
    *,
    source_silver_run_id: str,
    source_core_gold_run_id: str,
    run_id: str,
) -> tuple[str, str, str]:
    source_silver_run_id = _validate_id(source_silver_run_id, "source_silver_run_id")
    source_core_gold_run_id = _validate_id(
        source_core_gold_run_id, "source_core_gold_run_id"
    )
    run_id = _validate_id(run_id, "run_id")
    if run_id in {source_silver_run_id, source_core_gold_run_id}:
        raise ValueError("feature run_id must differ from source run IDs")
    base = f"gs://{runtime.data_bucket}/lakehouse/runs"
    return (
        f"{base}/{source_silver_run_id}/silver",
        f"{base}/{source_core_gold_run_id}/gold",
        f"{base}/{run_id}/gold",
    )


def assert_output_prefix_absent(
    runtime: CloudRuntime,
    *,
    run_id: str,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    _, _, output_root = run_roots(
        runtime,
        source_silver_run_id="source-silver-check",
        source_core_gold_run_id="source-gold-check",
        run_id=run_id,
    )
    completed = runner(
        ["gcloud", "storage", "ls", f"{output_root}/**"],
        check=False, text=True, capture_output=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        raise ValueError(
            f"Feature Gold output prefix already exists and is immutable: {output_root}"
        )
    missing = completed.returncode == 1 and "matched no objects" in completed.stderr.lower()
    if completed.returncode != 0 and not missing:
        raise RuntimeError(
            f"Could not verify feature Gold output prefix: {completed.stderr.strip()}"
        )


def build_submission_command(
    runtime: CloudRuntime,
    *,
    dataset_version: str,
    source_silver_run_id: str,
    source_core_gold_run_id: str,
    run_id: str,
    script_path: Path = FEATURE_SCRIPT,
    archive_path: Path = DEFAULT_ARCHIVE,
    runtime_profile_path: Path = GCP_RUNTIME_PROFILE,
    jar_paths: Sequence[Path] | None = None,
    ttl: str = "2h",
    performance_profile: str = DEFAULT_PERFORMANCE_PROFILE,
    spark_profile: str | None = None,
    benchmark_id: str | None = None,
    git_sha: str | None = None,
    pair_manifest: Mapping[str, object] | None = None,
) -> tuple[str, ...]:
    dataset_version = _validate_id(dataset_version, "dataset_version")
    ttl = _validate_ttl(ttl)
    execution_profile = resolve_submission_profile(
        spark_profile=spark_profile,
        performance_profile=(
            performance_profile
            if performance_profile != DEFAULT_PERFORMANCE_PROFILE
            else None
        ),
    )
    require_benchmark_pair(
        spark_profile=spark_profile, benchmark_id=benchmark_id
    )
    if spark_profile is not None and ttl != "8h":
        raise ValueError("full-scale benchmark submissions require ttl=8h")
    if benchmark_id is not None:
        require_profile_binding(
            run_id,
            benchmark_id=benchmark_id,
            profile=spark_profile,
            stage="feat",
        )
        if git_sha is None:
            raise ValueError("benchmark submissions require --git-sha")
        git_sha = validate_git_sha(git_sha)
    silver_root, core_gold_root, feature_gold_root = run_roots(
        runtime,
        source_silver_run_id=source_silver_run_id,
        source_core_gold_run_id=source_core_gold_run_id,
        run_id=run_id,
    )
    if benchmark_id is not None:
        if pair_manifest is None:
            raise ValueError("benchmark submissions require --pair-manifest")
        validate_pair_member(
            pair_manifest,
            spark_profile=str(spark_profile),
            benchmark_id=benchmark_id,
            stage="dp3-features",
            dataset_version=dataset_version,
            git_sha=str(git_sha),
            upstream_run_ids=(source_silver_run_id, source_core_gold_run_id),
            output_run_id=run_id,
            output_root=feature_gold_root,
        )
    jars = tuple(jar_paths or dependency_jar_paths())
    property_items = list(
        build_dataproc_batch_properties(
            performance_tier=runtime.performance_tier,
            data_bucket=runtime.data_bucket,
            profile=execution_profile,
            stage="dp3-features",
        )
    )
    if benchmark_id is not None:
        property_items.extend(
            f"{key}={value}"
            for key, value in method2_runtime_properties(
                pair_manifest, str(spark_profile)
            ).items()
        )
    properties = ",".join(property_items)
    labels = "component=historical-features,phase=s1b"
    if benchmark_id is not None:
        labels += (
            f",benchmark_id={benchmark_id},spark_profile={spark_profile},"
            f"git_sha={git_sha}"
        )
    return (
        "gcloud", "dataproc", "batches", "submit", "pyspark", str(script_path),
        f"--batch={run_id}", f"--project={runtime.project_id}",
        f"--region={runtime.region}", "--version=2.2",
        f"--service-account={runtime.service_account_email}",
        f"--subnet={runtime.subnet}",
        "--jars=" + ",".join(str(path) for path in jars),
        f"--py-files={archive_path}", f"--files={runtime_profile_path}",
        f"--deps-bucket=gs://{runtime.temporary_bucket}",
        f"--staging-bucket={runtime.temporary_bucket}", f"--ttl={ttl}",
        f"--labels={labels}",
        f"--properties={properties}", "--", "--environment=gcp",
        f"--mode={spark_profile or 'optimized'}",
        f"--expected-table-count={len(HISTORICAL_FEATURE_TABLES)}",
        f"--dataset-version={dataset_version}",
        f"--source-silver-run-id={source_silver_run_id}",
        f"--source-core-gold-run-id={source_core_gold_run_id}",
        f"--run-id={run_id}", f"--project-id={runtime.project_id}",
        f"--region={runtime.region}", f"--data-bucket={runtime.data_bucket}",
        f"--runtime-config-file={runtime_profile_path.name}",
        f"--silver-root={silver_root}",
        f"--source-core-gold-root={core_gold_root}",
        f"--feature-gold-root={feature_gold_root}",
        *(
            (
                f"--benchmark-id={benchmark_id}",
                f"--spark-profile={spark_profile}",
                f"--git-sha={git_sha}",
            )
            if benchmark_id is not None
            else ()
        ),
    )


def _load_outputs(path: Path | None, runner: Callable[..., Any]) -> Mapping[str, Any]:
    if path is not None:
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        completed = runner(
            ["terraform", f"-chdir={TERRAFORM_DEV_ROOT}", "output", "-json"],
            check=True, text=True, capture_output=True,
        )
        value = json.loads(completed.stdout)
    if not isinstance(value, Mapping):
        raise ValueError("Terraform output JSON must be an object")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-silver-run-id", required=True)
    parser.add_argument("--source-core-gold-run-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--ttl", default="2h")
    add_performance_profile_argument(parser)
    add_spark_profile_argument(parser)
    parser.add_argument("--benchmark-id")
    parser.add_argument("--git-sha")
    parser.add_argument("--pair-manifest", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    args = parse_args(argv)
    runtime = load_cloud_runtime(_load_outputs(args.terraform_output_json, runner))
    jars = dependency_jar_paths()
    archive = DEFAULT_ARCHIVE
    if args.execute:
        assert_output_prefix_absent(runtime, run_id=args.run_id, runner=runner)
        jars = ensure_delta_dependencies()
        archive = build_python_archive(REPOSITORY_ROOT, DEFAULT_ARCHIVE)
    command = build_submission_command(
        runtime,
        dataset_version=args.dataset_version,
        source_silver_run_id=args.source_silver_run_id,
        source_core_gold_run_id=args.source_core_gold_run_id,
        run_id=args.run_id,
        ttl=args.ttl,
        performance_profile=args.performance_profile or DEFAULT_PERFORMANCE_PROFILE,
        spark_profile=args.spark_profile,
        benchmark_id=args.benchmark_id,
        git_sha=args.git_sha,
        pair_manifest=(
            load_pair_manifest(args.pair_manifest)
            if args.pair_manifest is not None
            else None
        ),
        archive_path=archive,
        jar_paths=jars,
    )
    roots = run_roots(
        runtime,
        source_silver_run_id=args.source_silver_run_id,
        source_core_gold_run_id=args.source_core_gold_run_id,
        run_id=args.run_id,
    )
    if not args.execute:
        print("DRY RUN — no Dataproc batch was submitted")
        if args.spark_profile:
            print("\n".join(profile_summary(args.spark_profile, stage="dp3-features")))
        if args.benchmark_id:
            print(f"BENCHMARK_ID: {args.benchmark_id}")
            print(
                "PAIR_MANIFEST_DIGEST: "
                f"{load_pair_manifest(args.pair_manifest)['pair_manifest_digest']}"
            )
        print(f"Silver input root: {roots[0]}")
        print(f"Core Gold lineage root: {roots[1]}")
        print(f"Feature Gold output root: {roots[2]}")
        print(
            f"Tables ({len(HISTORICAL_FEATURE_TABLES)}): "
            f"{', '.join(HISTORICAL_FEATURE_TABLES)}"
        )
        print(shlex.join(command))
        return 0
    runner(command, check=True)
    print(f"Bounded historical feature batch submitted: {args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
