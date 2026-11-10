"""Render or execute one read-only Dataproc validator batch for a campaign stage.

The validators read explicit immutable run roots and write only one JSON report
(``runs/<run>/evidence/validation-<scope>-a<attempt>.json``). A retry is a new
attempt with a new batch ID and a new report; existing reports are never replaced.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_platform.config.dataset_identity import dataset_has_merchant_history, refuse_misplaced_dataset
from data_platform.config.spark_profiles import SPARK_PROFILES, require_campaign_lineage, validate_git_sha
from ops.gcp.dataproc_batch_properties import (
    build_dataproc_batch_properties,
    serialize_dataproc_batch_properties,
)
from ops.gcp.package_python_job import build_python_archive
from ops.gcp.submit_dataproc_delta_smoke import dependency_jar_paths, ensure_delta_dependencies
from ops.gcp.submit_silver_to_gold import GCP_RUNTIME_PROFILE, _load_outputs, assert_batch_id_absent
from ops.gcp.submit_source_to_bronze import (
    DEFAULT_ARCHIVE,
    CloudRuntime,
    _validate_ttl,
    apply_dataset_bucket,
    execution_record_uri,
    load_cloud_runtime,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PIT_SAMPLE_MODULUS = 1000
MAX_BATCH_ID_LENGTH = 50  # the repository's batch ID contract (ops.gcp submitters)


@dataclass(frozen=True)
class ValidationScope:
    script: str
    spark_stage: str
    run_stage: str  # stage alias of the run this scope validates


SCOPES: dict[str, ValidationScope] = {
    "bronze": ValidationScope("data_platform/quality/validate_bronze.py", "dp1-bronze", "bronze"),
    "silver": ValidationScope("data_platform/quality/validate_silver.py", "dp2-silver", "silver"),
    "core-gold": ValidationScope("data_platform/quality/validate_gold.py", "dp2-gold", "gold"),
    "features": ValidationScope("data_platform/quality/validate_gold.py", "dp3-features", "feat"),
}


@dataclass(frozen=True)
class ValidationRuns:
    """The run IDs of one campaign profile; a scope uses the ones it reads."""

    bronze: str | None = None
    silver: str | None = None
    gold: str | None = None
    features: str | None = None

    def required(self, name: str) -> str:
        value = getattr(self, name)
        if not value:
            raise ValueError(f"{name} run ID is required for this validation scope")
        return value


def run_root(data_bucket: str, run_id: str, zone: str) -> str:
    return f"gs://{data_bucket}/lakehouse/runs/{run_id}/{zone}"


def validated_run_id(scope: str, runs: ValidationRuns) -> str:
    return runs.required({"bronze": "bronze", "silver": "silver", "core-gold": "gold", "features": "features"}[scope])


def validation_batch_id(run_id: str, attempt: int) -> str:
    """``v<attempt>-<run ID without its "ab-" prefix>``.

    A campaign run ID is up to 48 characters, so suffixing it (``<run>-v1``) exceeded
    the 50-character batch contract for Optimized Bronze and Silver. Replacing the
    constant ``ab-`` prefix keeps every validation batch ID within the contract,
    unique per run and attempt, and still readable.
    """

    if attempt < 1:
        raise ValueError("attempt must be positive")
    batch_id = f"v{attempt}-{run_id.removeprefix('ab-')}"
    if len(batch_id) > MAX_BATCH_ID_LENGTH:
        raise ValueError("validation batch ID exceeds the 50-character contract; use new run IDs")
    return batch_id


def report_uri(data_bucket: str, run_id: str, scope: str, attempt: int) -> str:
    return f"gs://{data_bucket}/lakehouse/runs/{run_id}/evidence/validation-{scope}-a{attempt}.json"


def application_arguments(
    runtime: CloudRuntime,
    *,
    scope: str,
    dataset_version: str,
    runs: ValidationRuns,
    attempt: int,
    runtime_profile_name: str,
) -> tuple[str, ...]:
    """Validator arguments: explicit roots only, never a default storage location."""

    bucket = runtime.data_bucket
    run_id = validated_run_id(scope, runs)
    history = dataset_has_merchant_history(dataset_version)
    common = (
        "--environment=gcp",
        f"--project-id={runtime.project_id}",
        f"--region={runtime.region}",
        f"--data-bucket={bucket}",
        f"--runtime-config-file={runtime_profile_name}",
        f"--dataset-version={dataset_version}",
        f"--run-id={run_id}",
        f"--report-uri={report_uri(bucket, run_id, scope, attempt)}",
        f"--execution-record-uri={execution_record_uri(bucket, run_id, validation_batch_id(run_id, attempt))}",
    )
    if scope == "bronze":
        specific = ("--include-fraud-labels", *(("--include-merchant-history",) if history else ()),
                    f"--bronze-root={run_root(bucket, runs.required('bronze'), 'bronze')}")
    elif scope == "silver":
        specific = ("--include-fraud-labels", *(("--include-merchant-history",) if history else ()),
                    f"--bronze-root={run_root(bucket, runs.required('bronze'), 'bronze')}",
                    f"--silver-root={run_root(bucket, runs.required('silver'), 'silver')}")
    elif scope == "core-gold":
        specific = ("--scope=core-gold", *(("--include-merchant-scd2",) if history else ()),
                    f"--silver-root={run_root(bucket, runs.required('silver'), 'silver')}",
                    f"--gold-root={run_root(bucket, runs.required('gold'), 'gold')}")
    elif scope == "features":
        specific = ("--scope=features", f"--pit-sample-modulus={PIT_SAMPLE_MODULUS}",
                    f"--silver-root={run_root(bucket, runs.required('silver'), 'silver')}",
                    f"--gold-root={run_root(bucket, runs.required('gold'), 'gold')}",
                    f"--feature-gold-root={run_root(bucket, runs.required('features'), 'gold')}")
    else:
        raise ValueError(f"unknown validation scope: {scope}")
    return (*common, *specific)


def build_submission_command(
    runtime: CloudRuntime,
    *,
    scope: str,
    dataset_version: str,
    benchmark_id: str,
    spark_profile: str,
    git_sha: str,
    runs: ValidationRuns,
    attempt: int = 1,
    ttl: str = "8h",
    archive_path: Path = DEFAULT_ARCHIVE,
    jar_paths: Sequence[Path] | None = None,
    runtime_profile_path: Path = GCP_RUNTIME_PROFILE,
) -> tuple[str, ...]:
    if scope not in SCOPES:
        raise ValueError(f"unknown validation scope: {scope}")
    spec = SCOPES[scope]
    refuse_misplaced_dataset(dataset_version, runtime.data_bucket)
    _validate_ttl(ttl)
    validate_git_sha(git_sha)
    run_id = validated_run_id(scope, runs)
    upstream = {
        "bronze": {},
        "silver": {"bronze": runs.required("bronze")} if scope == "silver" else {},
        "core-gold": {"silver": runs.required("silver")} if scope == "core-gold" else {},
        "features": ({"silver": runs.required("silver"), "gold": runs.required("gold")}
                     if scope == "features" else {}),
    }[scope]
    require_campaign_lineage(run_id, benchmark_id=benchmark_id, profile=spark_profile,
                             stage=spec.run_stage, upstream=upstream)
    properties = serialize_dataproc_batch_properties(list(build_dataproc_batch_properties(
        performance_tier=runtime.performance_tier,
        data_bucket=runtime.data_bucket,
        profile=spark_profile,
        stage=spec.spark_stage,
    )))
    labels = (f"component=stage-validation,benchmark_id={benchmark_id},"
              f"spark_profile={spark_profile},git_sha={git_sha},scope={scope}")
    jars = tuple(jar_paths or dependency_jar_paths())
    return (
        "gcloud", "dataproc", "batches", "submit", "pyspark", str(REPOSITORY_ROOT / spec.script),
        f"--batch={validation_batch_id(run_id, attempt)}",
        f"--project={runtime.project_id}", f"--region={runtime.region}", "--version=2.2",
        f"--service-account={runtime.service_account_email}", f"--subnet={runtime.subnet}",
        "--jars=" + ",".join(str(path) for path in jars),
        f"--py-files={archive_path}", f"--files={runtime_profile_path}",
        f"--deps-bucket=gs://{runtime.temporary_bucket}",
        f"--staging-bucket={runtime.temporary_bucket}", f"--ttl={ttl}",
        f"--labels={labels}", f"--properties={properties}", "--",
        *application_arguments(
            runtime, scope=scope, dataset_version=dataset_version, runs=runs, attempt=attempt,
            runtime_profile_name=runtime_profile_path.name,
        ),
    )


def assert_report_absent(uri: str, *, runner: Callable[..., Any]) -> None:
    completed = runner(["gcloud", "storage", "ls", uri], check=False, text=True, capture_output=True)
    if completed.returncode == 0 and completed.stdout.strip():
        raise ValueError(f"validation report already exists and is immutable: {uri}")
    if completed.returncode != 0 and "matched no objects" not in completed.stderr.lower():
        raise RuntimeError(f"could not verify validation report: {completed.stderr.strip()}")


def read_report(uri: str, *, runner: Callable[..., Any]) -> Mapping[str, Any]:
    completed = runner(["gcloud", "storage", "cat", uri], check=True, text=True, capture_output=True)
    value = json.loads(completed.stdout)
    if not isinstance(value, Mapping):
        raise ValueError(f"validation report is not an object: {uri}")
    return value


def require_passing_report(
    report: Mapping[str, Any], *, scope: str, run_id: str, dataset_version: str
) -> None:
    """A batch that exited zero still has to prove every rule passed for this run."""

    expected = {"scope": scope, "run_id": run_id, "dataset_version": dataset_version}
    for field, value in expected.items():
        if report.get(field) != value:
            raise ValueError(f"validation report {field} is {report.get(field)!r}, expected {value!r}")
    if report.get("status") != "PASS":
        raise ValueError(f"validation {scope} failed: {report.get('failed_rules')}")
    if not report.get("results"):
        raise ValueError(f"validation {scope} report contains no rules")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True, choices=tuple(SCOPES))
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--spark-profile", required=True, choices=SPARK_PROFILES)
    parser.add_argument("--git-sha", required=True)
    for name in ("bronze", "silver", "gold", "features"):
        parser.add_argument(f"--{name}-run-id")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--ttl", default="8h")
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, runner: Callable[..., Any] = subprocess.run) -> int:
    args = parse_args(argv)
    runtime = apply_dataset_bucket(
        load_cloud_runtime(_load_outputs(args.terraform_output_json, runner)), args.dataset_version
    )
    runs = ValidationRuns(args.bronze_run_id, args.silver_run_id, args.gold_run_id, args.features_run_id)
    run_id = validated_run_id(args.scope, runs)
    batch_id = validation_batch_id(run_id, args.attempt)
    uri = report_uri(runtime.data_bucket, run_id, args.scope, args.attempt)
    archive, jars = DEFAULT_ARCHIVE, dependency_jar_paths()
    if args.execute:
        assert_batch_id_absent(runtime, batch_id=batch_id, runner=runner)
        assert_report_absent(uri, runner=runner)
        jars = ensure_delta_dependencies()
        archive = build_python_archive(REPOSITORY_ROOT, DEFAULT_ARCHIVE)
    command = build_submission_command(
        runtime, scope=args.scope, dataset_version=args.dataset_version, benchmark_id=args.benchmark_id,
        spark_profile=args.spark_profile, git_sha=args.git_sha, runs=runs, attempt=args.attempt, ttl=args.ttl,
        archive_path=archive, jar_paths=jars,
    )
    if not args.execute:
        print("DRY RUN — no Dataproc batch was submitted")
        print(f"Validation report: {uri}")
        print(shlex.join(command))
        return 0
    runner(command, check=True)
    require_passing_report(read_report(uri, runner=runner), scope=args.scope, run_id=run_id,
                           dataset_version=args.dataset_version)
    print(f"PASS: validation {args.scope} batch={batch_id} report={uri}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
