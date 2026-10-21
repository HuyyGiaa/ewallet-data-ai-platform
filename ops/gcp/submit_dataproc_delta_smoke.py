"""Render or execute the bounded R3 Dataproc Delta-on-GCS smoke batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM_DEV_ROOT = REPOSITORY_ROOT / "infra/terraform/gcp/environments/dev"
SMOKE_SCRIPT = REPOSITORY_ROOT / "ops/gcp/dataproc_delta_smoke.py"
DEFAULT_EVIDENCE_PATH = (
    REPOSITORY_ROOT / "docs/evidence/cloud/r3_dataproc_delta_smoke.json"
)
DEFAULT_DEPENDENCY_DIR = REPOSITORY_ROOT / "tmp/dataproc-delta-dependencies"
_RUN_ID = re.compile(r"^[a-z][a-z0-9-]{2,48}[a-z0-9]$")


@dataclass(frozen=True)
class PinnedDependency:
    filename: str
    url: str
    sha256: str


DELTA_DEPENDENCIES = (
    PinnedDependency(
        filename="delta-spark_2.13-3.2.0.jar",
        url=(
            "https://repo.maven.apache.org/maven2/io/delta/"
            "delta-spark_2.13/3.2.0/delta-spark_2.13-3.2.0.jar"
        ),
        sha256="45f0dda976e1023187bbf027539b276971ff7dd30d272e1078017d1b9344c5f3",
    ),
    PinnedDependency(
        filename="delta-storage-3.2.0.jar",
        url=(
            "https://repo.maven.apache.org/maven2/io/delta/"
            "delta-storage/3.2.0/delta-storage-3.2.0.jar"
        ),
        sha256="58aab63eba7736fea9e03eafb0dde6704a34a70f570c1a69ab8e4012c25a95d4",
    ),
    PinnedDependency(
        filename="antlr4-runtime-4.9.3.jar",
        url=(
            "https://repo.maven.apache.org/maven2/org/antlr/"
            "antlr4-runtime/4.9.3/antlr4-runtime-4.9.3.jar"
        ),
        sha256="131a6594969bc4f321d652ea2a33bc0e378ca312685ef87791b2c60b29d01ea5",
    ),
)


@dataclass(frozen=True)
class DataprocRuntime:
    project_id: str
    region: str
    subnet: str
    service_account_email: str
    staging_bucket: str
    performance_tier: str


def dependency_jar_paths(
    directory: Path = DEFAULT_DEPENDENCY_DIR,
) -> tuple[Path, ...]:
    """Return deterministic local paths for the pinned Delta dependencies."""

    return tuple(directory / item.filename for item in DELTA_DEPENDENCIES)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_delta_dependencies(
    directory: Path = DEFAULT_DEPENDENCY_DIR,
    *,
    dependencies: Sequence[PinnedDependency] = DELTA_DEPENDENCIES,
    opener: Callable[..., Any] = urlopen,
) -> tuple[Path, ...]:
    """Download pinned JARs locally and reject any checksum mismatch."""

    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for dependency in dependencies:
        destination = directory / dependency.filename
        if destination.is_file() and _sha256(destination) == dependency.sha256:
            paths.append(destination)
            continue

        with opener(dependency.url, timeout=60) as response:
            payload = response.read()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != dependency.sha256:
            raise ValueError(
                f"dependency checksum mismatch for {dependency.filename}: "
                f"expected {dependency.sha256}, got {actual}"
            )
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(payload)
        temporary.replace(destination)
        paths.append(destination)
    return tuple(paths)


def _output_value(outputs: Mapping[str, Any], name: str) -> Any:
    entry = outputs.get(name)
    if not isinstance(entry, Mapping) or "value" not in entry:
        raise ValueError(f"Terraform output {name!r} is missing")
    return entry["value"]


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Terraform output {name!r} must be a non-empty string")
    return value.strip()


def load_runtime_from_terraform_output(
    outputs: Mapping[str, Any],
) -> DataprocRuntime:
    """Extract and cross-check the batch runtime from Terraform output JSON."""

    project_id = _required_string(_output_value(outputs, "project_id"), "project_id")
    region = _required_string(_output_value(outputs, "region"), "region")
    temporary_bucket = _required_string(
        _output_value(outputs, "temporary_bucket_name"),
        "temporary_bucket_name",
    )
    service_accounts = _output_value(outputs, "service_account_emails")
    batch_runtime = _output_value(outputs, "dataproc_batch_runtime")
    if not isinstance(service_accounts, Mapping):
        raise ValueError("Terraform output 'service_account_emails' must be an object")
    if not isinstance(batch_runtime, Mapping):
        raise ValueError("Terraform output 'dataproc_batch_runtime' must be an object")

    spark_email = _required_string(
        service_accounts.get("spark"),
        "service_account_emails.spark",
    )
    cross_checks = {
        "project_id": project_id,
        "region": region,
        "service_account_email": spark_email,
        "staging_bucket": temporary_bucket,
    }
    for name, expected in cross_checks.items():
        actual = batch_runtime.get(name)
        if actual != expected:
            raise ValueError(
                f"Terraform Dataproc {name} does not match foundation output"
            )
    if batch_runtime.get("execution_mode") != "serverless-batch":
        raise ValueError("Terraform Dataproc execution_mode must be serverless-batch")
    tier = _required_string(
        batch_runtime.get("performance_tier"),
        "dataproc_batch_runtime.performance_tier",
    )
    if tier != "standard":
        raise ValueError("R3 smoke requires the standard Dataproc tier")

    return DataprocRuntime(
        project_id=project_id,
        region=region,
        subnet=_required_string(
            batch_runtime.get("subnet"),
            "dataproc_batch_runtime.subnet",
        ),
        service_account_email=spark_email,
        staging_bucket=temporary_bucket,
        performance_tier=tier,
    )


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError(
            "run ID must be 4-50 lowercase letters, digits, or hyphens; "
            "it must start with a letter and end with a letter or digit"
        )
    return run_id


def run_uri(runtime: DataprocRuntime, run_id: str) -> str:
    return (
        f"gs://{runtime.staging_bucket}/dataproc-smoke/delta/"
        f"{validate_run_id(run_id)}"
    )


def build_submission_command(
    runtime: DataprocRuntime,
    run_id: str,
    *,
    script_path: Path = SMOKE_SCRIPT,
    jar_paths: Sequence[Path] | None = None,
) -> tuple[str, ...]:
    """Build one argument-safe gcloud submission command."""

    validated_run_id = validate_run_id(run_id)
    output_uri = run_uri(runtime, validated_run_id)
    resolved_jar_paths = tuple(jar_paths or dependency_jar_paths())
    properties = ",".join(
        (
            f"dataproc.tier={runtime.performance_tier}",
            "spark.sql.extensions=io.delta.sql.DeltaSparkSessionExtension",
            "spark.sql.catalog.spark_catalog="
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
            "spark.sql.session.timeZone=UTC",
        )
    )
    return (
        "gcloud",
        "dataproc",
        "batches",
        "submit",
        "pyspark",
        str(script_path),
        f"--batch={validated_run_id}",
        f"--project={runtime.project_id}",
        f"--region={runtime.region}",
        "--version=2.2",
        f"--service-account={runtime.service_account_email}",
        f"--subnet={runtime.subnet}",
        "--jars=" + ",".join(str(path) for path in resolved_jar_paths),
        f"--deps-bucket=gs://{runtime.staging_bucket}",
        f"--staging-bucket={runtime.staging_bucket}",
        "--ttl=20m",
        "--labels=component=delta-smoke,phase=r3",
        f"--properties={properties}",
        "--",
        f"--run-uri={output_uri}",
        f"--batch-id={validated_run_id}",
        "--runtime-version=2.2",
    )


def validate_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Validate remote evidence before it is persisted in the repository."""

    expected = {
        "status": "PASS",
        "runtime_version": "2.2",
        "initial_rows": 4,
        "final_rows": 6,
        "distinct_transaction_ids": 6,
        "amount_sum": 210.75,
        "initial_delta_version": 0,
        "final_delta_version": 1,
        "microsecond_timestamp": "2026-01-01T00:00:00.123456+00:00",
        "timestamp_precision_pass": True,
        "schema": [
            {"name": "transaction_id", "type": "string"},
            {"name": "user_id", "type": "string"},
            {"name": "amount", "type": "double"},
            {"name": "event_timestamp", "type": "timestamp"},
        ],
    }
    for name, expected_value in expected.items():
        actual = evidence.get(name)
        if actual != expected_value:
            raise ValueError(
                f"remote evidence field {name!r} expected "
                f"{expected_value!r}, got {actual!r}"
            )
    return dict(evidence)


def _load_terraform_outputs(
    output_path: Path | None,
    runner: Callable[..., Any],
) -> Mapping[str, Any]:
    if output_path is not None:
        parsed = json.loads(output_path.read_text(encoding="utf-8"))
    else:
        result = runner(
            [
                "terraform",
                f"-chdir={TERRAFORM_DEV_ROOT}",
                "output",
                "-json",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        parsed = json.loads(result.stdout)
    if not isinstance(parsed, Mapping):
        raise ValueError("Terraform output JSON must be an object")
    return parsed


def _execute_and_record(
    command: tuple[str, ...],
    runtime: DataprocRuntime,
    run_id: str,
    evidence_path: Path,
    runner: Callable[..., Any],
) -> dict[str, Any]:
    runner(command, check=True)
    evidence_uri = f"{run_uri(runtime, run_id)}/evidence.json"
    result = runner(
        [
            "gcloud",
            "storage",
            "cat",
            evidence_uri,
            f"--project={runtime.project_id}",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    evidence = validate_evidence(json.loads(result.stdout))
    evidence.update(
        {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "project_id": runtime.project_id,
            "region": runtime.region,
            "staging_bucket": runtime.staging_bucket,
            "service_account_email": runtime.service_account_email,
        }
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--evidence-output", type=Path, default=DEFAULT_EVIDENCE_PATH)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    args = parse_args(argv)
    outputs = _load_terraform_outputs(args.terraform_output_json, runner)
    runtime = load_runtime_from_terraform_output(outputs)
    jar_paths = dependency_jar_paths()
    if args.execute:
        print("Preparing pinned Delta 3.2.0 / Scala 2.13 dependencies")
        jar_paths = ensure_delta_dependencies()
    command = build_submission_command(runtime, args.run_id, jar_paths=jar_paths)

    if not args.execute:
        print("DRY RUN — no Dataproc batch was submitted")
        print(shlex.join(command))
        print(f"Expected evidence: {run_uri(runtime, args.run_id)}/evidence.json")
        return 0

    evidence = _execute_and_record(
        command,
        runtime,
        args.run_id,
        args.evidence_output,
        runner,
    )
    print(f"R3 DATAPROC DELTA SMOKE {evidence['status']}")
    print(f"Evidence written: {args.evidence_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
