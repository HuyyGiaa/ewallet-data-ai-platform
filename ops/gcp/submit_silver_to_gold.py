"""Render or execute the bounded Silver-to-core-Gold Dataproc batch."""

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
from data_platform.storage.scripts.config import CORE_GOLD_TABLES
from ops.gcp.dataproc_batch_properties import (
    DEFAULT_PERFORMANCE_PROFILE,
    add_performance_profile_argument,
    add_spark_profile_argument,
    build_dataproc_batch_properties,
    profile_summary,
    resolve_submission_profile,
    serialize_dataproc_batch_properties,
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


GOLD_SCRIPT = REPOSITORY_ROOT / "data_platform/processing/spark/gold/gold_pipeline.py"
DEFAULT_ARCHIVE = REPOSITORY_ROOT / "tmp/dataproc/ewallet-python.zip"
GCP_RUNTIME_PROFILE = REPOSITORY_ROOT / "configs/environments/gcp/platform.yaml"


def run_roots(
    runtime: CloudRuntime, *, source_silver_run_id: str, run_id: str
) -> tuple[str, str]:
    source_silver_run_id = _validate_id(source_silver_run_id, "source_silver_run_id")
    run_id = _validate_id(run_id, "run_id")
    if run_id == source_silver_run_id:
        raise ValueError("Gold run_id must differ from source Silver run")
    base = f"gs://{runtime.data_bucket}/lakehouse/runs"
    return f"{base}/{source_silver_run_id}/silver", f"{base}/{run_id}/gold"


def assert_output_prefix_absent(
    runtime: CloudRuntime,
    *,
    run_id: str,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    _, output_root = run_roots(
        runtime, source_silver_run_id="source-check", run_id=run_id
    )
    completed = runner(
        ["gcloud", "storage", "ls", f"{output_root}/**"],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        raise ValueError(f"Gold output prefix already exists and is immutable: {output_root}")
    missing = completed.returncode == 1 and "matched no objects" in completed.stderr.lower()
    if completed.returncode != 0 and not missing:
        raise RuntimeError(f"Could not verify Gold output prefix: {completed.stderr.strip()}")


def assert_batch_id_absent(
    runtime: CloudRuntime,
    *,
    batch_id: str,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    batch_id = _validate_id(batch_id, "dataproc_batch_id")
    completed = runner(
        ["gcloud", "dataproc", "batches", "describe", batch_id,
         f"--project={runtime.project_id}", f"--region={runtime.region}",
         "--format=json"],
        check=False, text=True, capture_output=True,
    )
    if completed.returncode == 0:
        raise ValueError(f"Dataproc batch attempt already exists: {batch_id}")
    if "not_found" not in (completed.stdout + completed.stderr).lower():
        raise RuntimeError(f"Could not verify Dataproc batch absence: {batch_id}")


def require_clean_repository(
    repository: Path,
    expected_sha: str,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    expected_sha = validate_git_sha(expected_sha)
    repository = repository.resolve()
    head = runner(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    status = runner(
        ["git", "status", "--porcelain"], cwd=repository, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    if head != expected_sha or status:
        raise ValueError(f"Repository is not the declared clean Git source: {repository}")


def build_submission_command(
    runtime: CloudRuntime,
    *,
    dataset_version: str,
    source_silver_run_id: str,
    run_id: str,
    script_path: Path = GOLD_SCRIPT,
    archive_path: Path = DEFAULT_ARCHIVE,
    runtime_profile_path: Path = GCP_RUNTIME_PROFILE,
    jar_paths: Sequence[Path] | None = None,
    ttl: str = "2h",
    performance_profile: str = DEFAULT_PERFORMANCE_PROFILE,
    spark_profile: str | None = None,
    benchmark_id: str | None = None,
    git_sha: str | None = None,
    pair_manifest: Mapping[str, object] | None = None,
    dataproc_batch_id: str | None = None,
    recovery_git_sha: str | None = None,
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
            stage="gold",
        )
        if git_sha is None:
            raise ValueError("benchmark submissions require --git-sha")
        git_sha = validate_git_sha(git_sha)
    batch_id = _validate_id(dataproc_batch_id or run_id, "dataproc_batch_id")
    recovery_attempt = batch_id != run_id
    if recovery_attempt:
        if benchmark_id is None:
            raise ValueError("distinct Dataproc attempts require a Method-2 benchmark")
        recovery_git_sha = validate_git_sha(str(recovery_git_sha or ""))
        if script_path.resolve() == GOLD_SCRIPT.resolve():
            raise ValueError(
                "distinct Dataproc attempts require the original compute script path"
            )
    silver_root, gold_root = run_roots(
        runtime, source_silver_run_id=source_silver_run_id, run_id=run_id
    )
    if benchmark_id is not None:
        if pair_manifest is None:
            raise ValueError("benchmark submissions require --pair-manifest")
        validate_pair_member(
            pair_manifest,
            spark_profile=str(spark_profile),
            benchmark_id=benchmark_id,
            stage="dp2-gold",
            dataset_version=dataset_version,
            git_sha=str(git_sha),
            upstream_run_ids=(source_silver_run_id,),
            output_run_id=run_id,
            output_root=gold_root,
        )
    jars = tuple(jar_paths or dependency_jar_paths())
    property_items = list(
        build_dataproc_batch_properties(
            performance_tier=runtime.performance_tier,
            data_bucket=runtime.data_bucket,
            profile=execution_profile,
            stage="dp2-gold",
        )
    )
    if benchmark_id is not None:
        property_items.extend(
            f"{key}={value}"
            for key, value in method2_runtime_properties(
                pair_manifest, str(spark_profile)
            ).items()
        )
    properties = serialize_dataproc_batch_properties(property_items)
    labels = "component=core-gold,phase=s1b"
    if benchmark_id is not None:
        labels += (
            f",benchmark_id={benchmark_id},spark_profile={spark_profile},"
            f"git_sha={git_sha}"
        )
    if recovery_attempt:
        labels += (
            f",output_run_id={run_id},recovery_git_sha={recovery_git_sha}"
        )
    return (
        "gcloud", "dataproc", "batches", "submit", "pyspark", str(script_path),
        f"--batch={batch_id}", f"--project={runtime.project_id}",
        f"--region={runtime.region}", "--version=2.2",
        f"--service-account={runtime.service_account_email}",
        f"--subnet={runtime.subnet}",
        "--jars=" + ",".join(str(path) for path in jars),
        f"--py-files={archive_path}", f"--files={runtime_profile_path}",
        f"--deps-bucket=gs://{runtime.temporary_bucket}",
        f"--staging-bucket={runtime.temporary_bucket}", f"--ttl={ttl}",
        f"--labels={labels}", f"--properties={properties}", "--",
        "--environment=gcp", f"--mode={spark_profile or 'optimized'}",
        f"--expected-table-count={len(CORE_GOLD_TABLES)}",
        f"--dataset-version={dataset_version}",
        f"--source-silver-run-id={source_silver_run_id}", f"--run-id={run_id}",
        f"--project-id={runtime.project_id}", f"--region={runtime.region}",
        f"--data-bucket={runtime.data_bucket}",
        f"--runtime-config-file={runtime_profile_path.name}",
        f"--silver-root={silver_root}", f"--gold-root={gold_root}",
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


def _load_outputs(
    path: Path | None,
    runner: Callable[..., Any],
    *,
    terraform_root: Path = TERRAFORM_DEV_ROOT,
) -> Mapping[str, Any]:
    if path is not None:
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        completed = runner(
            ["terraform", f"-chdir={terraform_root}", "output", "-json"],
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
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dataproc-batch-id")
    parser.add_argument("--recovery-git-sha")
    parser.add_argument("--compute-repository", type=Path)
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
    batch_id = args.dataproc_batch_id or args.run_id
    recovery_attempt = batch_id != args.run_id
    terraform_root = (
        args.compute_repository.resolve()
        / "infra/terraform/gcp/environments/dev"
        if recovery_attempt and args.compute_repository is not None
        else TERRAFORM_DEV_ROOT
    )
    runtime = load_cloud_runtime(_load_outputs(
        args.terraform_output_json, runner, terraform_root=terraform_root
    ))
    jars = dependency_jar_paths()
    archive = DEFAULT_ARCHIVE
    script_path = GOLD_SCRIPT
    runtime_profile_path = GCP_RUNTIME_PROFILE
    if recovery_attempt:
        if args.compute_repository is None:
            raise ValueError("distinct Dataproc attempts require --compute-repository")
        if args.recovery_git_sha is None or args.git_sha is None:
            raise ValueError("distinct Dataproc attempts require both Git identities")
        require_clean_repository(args.compute_repository, args.git_sha, runner)
        require_clean_repository(REPOSITORY_ROOT, args.recovery_git_sha, runner)
        script_path = (
            args.compute_repository.resolve()
            / "data_platform/processing/spark/gold/gold_pipeline.py"
        )
        runtime_profile_path = (
            args.compute_repository.resolve()
            / "configs/environments/gcp/platform.yaml"
        )
    if recovery_attempt:
        assert_output_prefix_absent(runtime, run_id=args.run_id, runner=runner)
        assert_batch_id_absent(runtime, batch_id=batch_id, runner=runner)
    if args.execute:
        if not recovery_attempt:
            assert_output_prefix_absent(runtime, run_id=args.run_id, runner=runner)
            assert_batch_id_absent(runtime, batch_id=batch_id, runner=runner)
        jars = ensure_delta_dependencies()
        archive = build_python_archive(
            args.compute_repository.resolve() if recovery_attempt else REPOSITORY_ROOT,
            DEFAULT_ARCHIVE,
        )
    command = build_submission_command(
        runtime,
        dataset_version=args.dataset_version,
        source_silver_run_id=args.source_silver_run_id,
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
        script_path=script_path,
        runtime_profile_path=runtime_profile_path,
        dataproc_batch_id=batch_id,
        recovery_git_sha=args.recovery_git_sha,
    )
    silver_root, gold_root = run_roots(
        runtime, source_silver_run_id=args.source_silver_run_id, run_id=args.run_id
    )
    if not args.execute:
        print("DRY RUN — no Dataproc batch was submitted")
        if args.spark_profile:
            print("\n".join(profile_summary(args.spark_profile, stage="dp2-gold")))
        if args.benchmark_id:
            print(f"BENCHMARK_ID: {args.benchmark_id}")
            print(
                "PAIR_MANIFEST_DIGEST: "
                f"{load_pair_manifest(args.pair_manifest)['pair_manifest_digest']}"
            )
        print(f"Silver input root: {silver_root}")
        print(f"Core Gold output root: {gold_root}")
        print(f"Tables ({len(CORE_GOLD_TABLES)}): {', '.join(CORE_GOLD_TABLES)}")
        print(shlex.join(command))
        return 0
    runner(command, check=True)
    print(
        "Bounded Silver-to-core-Gold batch submitted: "
        f"batch={batch_id} output_run={args.run_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
