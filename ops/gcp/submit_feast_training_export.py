"""Render or submit bounded Feast validation/training-source Spark exports."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ai_platform.features.feast.cloud_boundary import (
    CANONICAL_TRAINING_SAMPLE_MODULUS,
    CloudFeastTrainingBoundary,
)
from ai_platform.features.feast.provenance import load_feature_selection_evidence
from data_platform.config.spark_profiles import FEATURES_STAGE, SILVER_STAGE
from ops.gcp.dataproc_batch_properties import build_dataproc_batch_properties
from ops.gcp.package_python_job import build_python_archive
from ops.gcp.publish_training_snapshot import (
    TrustedRegistryIdentity,
    require_pit_pass_evidence,
)
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


VALIDATION_SAMPLES_JOB = "validation-samples"
TRAINING_SOURCE_JOB = "training-source"
JOBS = (VALIDATION_SAMPLES_JOB, TRAINING_SOURCE_JOB)
JOB_SPECS = {
    VALIDATION_SAMPLES_JOB: {
        "script": REPOSITORY_ROOT
        / "ai_platform/features/feast/sources/export_validation_samples.py",
        "stage": FEATURES_STAGE,
        "component": "feast-validation",
    },
    TRAINING_SOURCE_JOB: {
        "script": REPOSITORY_ROOT
        / "ai_platform/ml/training/export_training_source.py",
        "stage": SILVER_STAGE,
        "component": "training-source",
    },
}
DEFAULT_ARCHIVE = REPOSITORY_ROOT / "tmp/dataproc/ewallet-ai-python.zip"
GCP_RUNTIME_PROFILE = REPOSITORY_ROOT / "configs/environments/gcp/platform.yaml"


def output_root(job: str, boundary: CloudFeastTrainingBoundary) -> str:
    if job == VALIDATION_SAMPLES_JOB:
        return boundary.validation_staging_root
    if job == TRAINING_SOURCE_JOB:
        return boundary.training_source_staging_root
    raise ValueError(f"unsupported Feast training export job: {job!r}")


def require_job_pit_evidence(
    job: str,
    pit_evidence: Path | None,
    boundary: CloudFeastTrainingBoundary,
    registry_identity: TrustedRegistryIdentity | None = None,
) -> None:
    if job == TRAINING_SOURCE_JOB:
        if pit_evidence is None:
            raise ValueError("training-source export requires PIT PASS evidence")
        if registry_identity is None:
            raise ValueError("training-source export requires trusted Registry identity")
        require_pit_pass_evidence(
            pit_evidence, boundary, registry_identity=registry_identity
        )
    elif pit_evidence is not None or registry_identity is not None:
        raise ValueError(
            "PIT evidence and Registry identity are valid only for training-source export"
        )


def build_submission_command(
    runtime: CloudRuntime,
    *,
    job: str,
    batch_id: str,
    boundary: CloudFeastTrainingBoundary,
    execution_git_sha: str = "0" * 40,
    script_path: Path | None = None,
    archive_path: Path = DEFAULT_ARCHIVE,
    runtime_profile_path: Path = GCP_RUNTIME_PROFILE,
    jar_paths: Sequence[Path] | None = None,
    ttl: str = "8h",
) -> tuple[str, ...]:
    batch_id = _validate_id(batch_id, "batch_id")
    ttl = _validate_ttl(ttl)
    if len(execution_git_sha) != 40 or any(
        character not in "0123456789abcdef" for character in execution_git_sha
    ):
        raise ValueError("execution_git_sha must be a full lowercase Git SHA")
    try:
        spec = JOB_SPECS[job]
    except KeyError as exc:
        raise ValueError(f"unsupported Feast training export job: {job!r}") from exc

    stage = str(spec["stage"])
    script = script_path or Path(spec["script"])
    jars = tuple(jar_paths or dependency_jar_paths())
    properties = ",".join(
        build_dataproc_batch_properties(
            performance_tier=runtime.performance_tier,
            data_bucket=runtime.data_bucket,
            profile=boundary.spark_profile,
            stage=stage,
        )
    )
    common_application_args = (
        "--environment=gcp",
        f"--project-id={runtime.project_id}",
        f"--region={runtime.region}",
        f"--data-bucket={runtime.data_bucket}",
        f"--output-root={output_root(job, boundary)}",
        f"--silver-root={boundary.silver_root}",
        f"--dataset-version={boundary.dataset_version}",
        f"--source-silver-run-id={boundary.silver_run_id}",
        f"--source-feature-run-id={boundary.feature_run_id}",
        f"--snapshot-id={boundary.snapshot_id}",
        f"--source-git-sha={boundary.source_git_sha}",
        f"--execution-git-sha={execution_git_sha}",
        f"--feature-selection-digest={boundary.feature_selection_digest}",
        f"--runtime-config-file={runtime_profile_path.name}",
    )
    if job == VALIDATION_SAMPLES_JOB:
        application_args = (
            *common_application_args,
            f"--feature-gold-root={boundary.feature_gold_root}",
            "--benchmark-rows=100",
        )
    else:
        application_args = (
            *common_application_args,
            f"--sample-modulus={CANONICAL_TRAINING_SAMPLE_MODULUS}",
        )

    return (
        "gcloud",
        "dataproc",
        "batches",
        "submit",
        "pyspark",
        str(script),
        f"--batch={batch_id}",
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
        "--labels="
        f"component={spec['component']},phase=feast-boundary,spark_stage={stage}",
        f"--properties={properties}",
        "--",
        *application_args,
    )


def assert_output_prefix_absent(
    runtime: CloudRuntime,
    *,
    job: str,
    boundary: CloudFeastTrainingBoundary,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    root = output_root(job, boundary)
    completed = runner(
        ["gcloud", "storage", "ls", f"{root}/**"],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        raise FileExistsError(f"immutable export prefix already exists: {root}")
    missing = (
        completed.returncode == 1
        and "matched no objects" in completed.stderr.lower()
    )
    if completed.returncode != 0 and not missing:
        raise RuntimeError(
            f"Could not verify immutable export prefix: {completed.stderr.strip()}"
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
    parser.add_argument("--job", choices=JOBS, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--silver-run-id", required=True)
    parser.add_argument("--feature-run-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--feast-snapshot-id", required=True)
    parser.add_argument("--feature-selection-evidence", type=Path, required=True)
    parser.add_argument("--pit-evidence", type=Path)
    parser.add_argument("--trusted-registry-readback-evidence", type=Path)
    parser.add_argument("--expected-registry-uri")
    parser.add_argument("--expected-registry-generation", type=int)
    parser.add_argument("--expected-registry-sha256")
    parser.add_argument("--expected-registry-execution-git-sha")
    parser.add_argument(
        "--sample-modulus",
        type=int,
        default=CANONICAL_TRAINING_SAMPLE_MODULUS,
    )
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--ttl", default="8h")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def _clean_repository_git_sha() -> str:
    status = subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        text=True,
    )
    if status.strip():
        raise ValueError("Feast training export requires a clean Git worktree")
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        text=True,
    ).strip()


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    args = parse_args(argv)
    if (
        args.job == TRAINING_SOURCE_JOB
        and args.sample_modulus != CANONICAL_TRAINING_SAMPLE_MODULUS
    ):
        raise ValueError("canonical training source requires sample modulus 10000")
    selection = load_feature_selection_evidence(args.feature_selection_evidence)
    expected = (
        args.dataset_version,
        args.silver_run_id,
        args.feature_run_id,
        f"gs://{selection.feature_root.removeprefix('gs://').split('/', 1)[0]}/"
        f"lakehouse/runs/{args.feature_run_id}/gold",
    )
    actual = (
        selection.dataset_version,
        selection.silver_run_id,
        selection.feature_run_id,
        selection.feature_root,
    )
    if expected != actual:
        raise ValueError("export identifiers do not match feature selection evidence")
    bucket = selection.feature_root.removeprefix("gs://").split("/", 1)[0]
    boundary = CloudFeastTrainingBoundary(
        bucket=bucket,
        dataset_version=args.dataset_version,
        silver_run_id=args.silver_run_id,
        feature_run_id=args.feature_run_id,
        snapshot_id=args.snapshot_id,
        source_git_sha=selection.git_sha,
        spark_profile=selection.spark_profile,
        feature_selection_digest=selection.evidence_digest,
        feast_snapshot_id=args.feast_snapshot_id,
    )
    identity_values = (
        args.trusted_registry_readback_evidence,
        args.expected_registry_uri,
        args.expected_registry_generation,
        args.expected_registry_sha256,
        args.expected_registry_execution_git_sha,
    )
    if any(value is not None for value in identity_values) and not all(
        value is not None for value in identity_values
    ):
        raise ValueError("trusted Registry identity arguments must be supplied together")
    registry_identity = None
    if all(value is not None for value in identity_values):
        registry_identity = TrustedRegistryIdentity(
            readback_evidence=args.trusted_registry_readback_evidence,
            uri=args.expected_registry_uri,
            generation=args.expected_registry_generation,
            sha256=args.expected_registry_sha256,
            execution_git_sha=args.expected_registry_execution_git_sha,
        )
    require_job_pit_evidence(
        args.job, args.pit_evidence, boundary, registry_identity
    )
    runtime = load_cloud_runtime(_load_outputs(args.terraform_output_json, runner))
    if runtime.data_bucket != boundary.bucket:
        raise ValueError("Terraform data bucket does not match boundary evidence")
    execution_git_sha = _clean_repository_git_sha()

    jars = dependency_jar_paths()
    archive = DEFAULT_ARCHIVE
    if args.execute:
        assert_output_prefix_absent(
            runtime,
            job=args.job,
            boundary=boundary,
            runner=runner,
        )
        jars = ensure_delta_dependencies()
        archive = build_python_archive(
            REPOSITORY_ROOT,
            DEFAULT_ARCHIVE,
            included_roots=("data_platform", "ops", "ai_platform"),
        )
    command = build_submission_command(
        runtime,
        job=args.job,
        batch_id=args.batch_id,
        boundary=boundary,
        execution_git_sha=execution_git_sha,
        archive_path=archive,
        jar_paths=jars,
        ttl=args.ttl,
    )
    if not args.execute:
        print("DRY RUN — no Dataproc batch was submitted")
        print(f"Job: {args.job}")
        print(f"Spark stage: {JOB_SPECS[args.job]['stage']}")
        print(f"Output root: {output_root(args.job, boundary)}")
        print(shlex.join(command))
        return 0
    runner(command, check=True)
    print(f"Submitted Feast training export batch: {args.batch_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
