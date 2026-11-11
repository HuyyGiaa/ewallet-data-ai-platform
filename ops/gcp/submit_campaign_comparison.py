"""Render or execute the read-only semantic comparison of one benchmark's two campaigns.

``compare_campaign_outputs.py`` needs distributed Spark with Delta and the GCS connector,
so it runs as one Dataproc Serverless batch in the environment every campaign job used:
runtime 2.2, the pinned Delta JARs, the repository Python archive and the common
resource contract. It reads the two ``campaign_manifest.json`` files and the Delta
outputs they name, and writes only ``campaigns/<benchmark>/comparison.json`` (create,
never overwrite).

The comparison is not a benchmark leg, so it uses no Baseline or Optimized SQL profile:
Spark's own SQL defaults apply, with the shared resource shape.

Without ``--execute`` nothing is called: the command is printed. With ``--execute``, read-only
checks run first (both manifests comparable and pinned to ``--git-sha``, the comparison
code unchanged since that commit, batch ID and report absent); only then is the batch
submitted.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from data_platform.config.dataset_identity import refuse_misplaced_dataset
from data_platform.config.spark_profiles import COMMON_RESOURCE_CONTRACT, validate_benchmark_id, validate_git_sha
from ops.gcp.compare_campaign_outputs import manifest_problems
from ops.gcp.dataproc_batch_properties import build_dataproc_batch_properties, serialize_dataproc_batch_properties
from ops.gcp.package_python_job import build_python_archive
from ops.gcp.submit_dataproc_delta_smoke import dependency_jar_paths, ensure_delta_dependencies
from ops.gcp.submit_silver_to_gold import _load_outputs, assert_batch_id_absent
from ops.gcp.submit_source_to_bronze import (
    DEFAULT_ARCHIVE,
    CloudRuntime,
    _validate_ttl,
    apply_dataset_bucket,
    load_cloud_runtime,
)
from ops.gcp.submit_stage_validation import MAX_BATCH_ID_LENGTH, assert_report_absent


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "ops/gcp/compare_campaign_outputs.py"
# The comparison's own code path; it must be the pinned commit's, so the verdict is reproducible.
COMPARISON_SOURCES = (
    SCRIPT,
    "ops/gcp/summarize_spark_output.py",
    "ops/gcp/validate_spark_output_equivalence.py",
    "data_platform/quality/reporting.py",
    "data_platform/config",
)


def campaign_root(data_bucket: str, benchmark_id: str) -> str:
    return f"gs://{data_bucket}/lakehouse/campaigns/{benchmark_id}"


def manifest_uri(data_bucket: str, benchmark_id: str, profile: str) -> str:
    return f"{campaign_root(data_bucket, benchmark_id)}/{profile}/campaign_manifest.json"


def report_uri(data_bucket: str, benchmark_id: str) -> str:
    return f"{campaign_root(data_bucket, benchmark_id)}/comparison.json"


def comparison_batch_id(benchmark_id: str, attempt: int) -> str:
    """``c<attempt>-<benchmark without "ab-">``; a retry needs a new batch, the report URI stays.

    The report is written once at the end, so an attempt that failed before writing it
    leaves the URI free, and an attempt that wrote it is final.
    """

    if attempt < 1:
        raise ValueError("attempt must be positive")
    batch_id = f"c{attempt}-{validate_benchmark_id(benchmark_id).removeprefix('ab-')}"
    if len(batch_id) > MAX_BATCH_ID_LENGTH:
        raise ValueError("comparison batch ID exceeds the 50-character contract")
    return batch_id


def batch_properties(runtime: CloudRuntime) -> str:
    """Delta, UTC and event logs as for every campaign job, plus the common resource shape."""

    return serialize_dataproc_batch_properties([
        *build_dataproc_batch_properties(performance_tier=runtime.performance_tier, data_bucket=runtime.data_bucket),
        *COMMON_RESOURCE_CONTRACT.spark_properties,
    ])


def build_submission_command(
    runtime: CloudRuntime,
    *,
    benchmark_id: str,
    dataset_version: str,
    git_sha: str,
    attempt: int = 1,
    ttl: str = "2h",
    archive_path: Path = DEFAULT_ARCHIVE,
    jar_paths: Sequence[Path] | None = None,
) -> tuple[str, ...]:
    refuse_misplaced_dataset(dataset_version, runtime.data_bucket)
    validate_git_sha(git_sha)
    _validate_ttl(ttl)
    bucket = runtime.data_bucket
    labels = (f"component=campaign-comparison,benchmark_id={benchmark_id},"
              f"dataset_version={dataset_version},git_sha={git_sha}")
    jars = tuple(jar_paths or dependency_jar_paths())
    return (
        "gcloud", "dataproc", "batches", "submit", "pyspark", str(REPOSITORY_ROOT / SCRIPT),
        f"--batch={comparison_batch_id(benchmark_id, attempt)}",
        f"--project={runtime.project_id}", f"--region={runtime.region}", "--version=2.2",
        f"--service-account={runtime.service_account_email}", f"--subnet={runtime.subnet}",
        "--jars=" + ",".join(str(path) for path in jars),
        f"--py-files={archive_path}",
        f"--deps-bucket=gs://{runtime.temporary_bucket}",
        f"--staging-bucket={runtime.temporary_bucket}", f"--ttl={ttl}",
        f"--labels={labels}", f"--properties={batch_properties(runtime)}", "--",
        f"--baseline-manifest-uri={manifest_uri(bucket, benchmark_id, 'baseline')}",
        f"--optimized-manifest-uri={manifest_uri(bucket, benchmark_id, 'optimized')}",
        f"--report-uri={report_uri(bucket, benchmark_id)}",
    )


def read_manifest(uri: str, *, runner: Callable[..., Any]) -> Mapping[str, Any]:
    completed = runner(["gcloud", "storage", "cat", uri], check=True, text=True, capture_output=True)
    value = json.loads(completed.stdout)
    if not isinstance(value, Mapping):
        raise ValueError(f"campaign manifest is not an object: {uri}")
    return value


def require_comparable(
    baseline: Mapping[str, Any], optimized: Mapping[str, Any], *,
    benchmark_id: str, dataset_version: str, git_sha: str,
) -> None:
    """The comparator's own rules, plus the identity this submission names."""

    problems = list(manifest_problems(baseline, optimized))
    for name, manifest in (("baseline", baseline), ("optimized", optimized)):
        for field, expected in (("benchmark_id", benchmark_id), ("dataset_version", dataset_version),
                                ("git_sha", git_sha)):
            if manifest.get(field) != expected:
                problems.append(f"{name} {field} is {manifest.get(field)!r}, expected {expected!r}")
    if problems:
        raise ValueError("campaigns are not comparable: " + "; ".join(problems))


def require_pinned_comparison_code(git_sha: str, *, runner: Callable[..., Any]) -> None:
    """The archive is built from the checkout, so its comparison code must equal the pinned commit."""

    completed = runner(["git", "-C", str(REPOSITORY_ROOT), "diff", "--quiet", git_sha, "--", *COMPARISON_SOURCES],
                       check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise ValueError(f"comparison code differs from the pinned commit {git_sha} "
                         f"(or the commit is unknown): {', '.join(COMPARISON_SOURCES)}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--git-sha", required=True, help="the benchmark's pinned commit (both manifests)")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--ttl", default="2h", help="hard runtime cap; bounds the paid batch")
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--execute", action="store_true", help="submit one paid Dataproc batch")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, runner: Callable[..., Any] = subprocess.run) -> int:
    args = parse_args(argv)
    runtime = apply_dataset_bucket(
        load_cloud_runtime(_load_outputs(args.terraform_output_json, runner)), args.dataset_version
    )
    batch_id = comparison_batch_id(args.benchmark_id, args.attempt)
    uri = report_uri(runtime.data_bucket, args.benchmark_id)
    archive, jars = DEFAULT_ARCHIVE, dependency_jar_paths()
    if args.execute:
        require_comparable(
            read_manifest(manifest_uri(runtime.data_bucket, args.benchmark_id, "baseline"), runner=runner),
            read_manifest(manifest_uri(runtime.data_bucket, args.benchmark_id, "optimized"), runner=runner),
            benchmark_id=args.benchmark_id, dataset_version=args.dataset_version, git_sha=args.git_sha,
        )
        require_pinned_comparison_code(args.git_sha, runner=runner)
        assert_batch_id_absent(runtime, batch_id=batch_id, runner=runner)
        assert_report_absent(uri, runner=runner)
        jars = ensure_delta_dependencies()
        archive = build_python_archive(REPOSITORY_ROOT, DEFAULT_ARCHIVE)
    command = build_submission_command(
        runtime, benchmark_id=args.benchmark_id, dataset_version=args.dataset_version, git_sha=args.git_sha,
        attempt=args.attempt, ttl=args.ttl, archive_path=archive, jar_paths=jars,
    )
    if not args.execute:
        print("DRY RUN — no Dataproc batch was submitted")
        print(f"Comparison report: {uri}")
        print(shlex.join(command))
        return 0
    # The comparator exits 1 for SEMANTIC_MISMATCH or COMPARISON_INCOMPLETE after writing its
    # report, so a failed batch is read back rather than treated as missing evidence.
    completed = runner(command, check=False)
    report = json.loads(runner(["gcloud", "storage", "cat", uri], check=True, text=True,
                               capture_output=True).stdout)
    print(json.dumps({"verdict": report.get("verdict"), "batch": batch_id, "report": uri,
                      "batch_exit_code": completed.returncode}))
    return 0 if report.get("verdict") == "SEMANTIC_EQUIVALENT" and completed.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
