"""Render or execute the R4 source-to-Bronze Dataproc Serverless batch."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ops.gcp.package_python_job import build_python_archive
from ops.gcp.submit_dataproc_delta_smoke import (
    TERRAFORM_DEV_ROOT,
    dependency_jar_paths,
    ensure_delta_dependencies,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_TO_BRONZE_SCRIPT = (
    REPOSITORY_ROOT / "data_platform/ingestion/spark/source_to_bronze.py"
)
DEFAULT_ARCHIVE = REPOSITORY_ROOT / "tmp/dataproc/ewallet-python.zip"
_SAFE_ID = re.compile(r"^[a-z][a-z0-9-]{2,48}[a-z0-9]$")


@dataclass(frozen=True)
class CloudRuntime:
    project_id: str
    region: str
    data_bucket: str
    temporary_bucket: str
    subnet: str
    service_account_email: str
    performance_tier: str


def _value(outputs: Mapping[str, Any], name: str) -> Any:
    entry = outputs.get(name)
    if not isinstance(entry, Mapping) or "value" not in entry:
        raise ValueError(f"Terraform output {name!r} is missing")
    return entry["value"]


def load_cloud_runtime(outputs: Mapping[str, Any]) -> CloudRuntime:
    project = str(_value(outputs, "project_id"))
    region = str(_value(outputs, "region"))
    data_bucket = str(_value(outputs, "data_bucket_name"))
    temp_bucket = str(_value(outputs, "temporary_bucket_name"))
    accounts = _value(outputs, "service_account_emails")
    batch = _value(outputs, "dataproc_batch_runtime")
    if not isinstance(accounts, Mapping) or not isinstance(batch, Mapping):
        raise ValueError("Terraform identity and Dataproc outputs must be objects")
    spark_account = str(accounts.get("spark", ""))
    expected = {
        "project_id": project,
        "region": region,
        "staging_bucket": temp_bucket,
        "service_account_email": spark_account,
        "execution_mode": "serverless-batch",
        "performance_tier": "standard",
    }
    for name, expected_value in expected.items():
        if batch.get(name) != expected_value:
            raise ValueError(f"Terraform Dataproc {name} is inconsistent")
    values = [project, region, data_bucket, temp_bucket, spark_account, batch.get("subnet")]
    if any(not isinstance(item, str) or not item for item in values):
        raise ValueError("Terraform cloud runtime contains an empty value")
    return CloudRuntime(
        project_id=project,
        region=region,
        data_bucket=data_bucket,
        temporary_bucket=temp_bucket,
        subnet=str(batch["subnet"]),
        service_account_email=spark_account,
        performance_tier="standard",
    )


def _validate_id(value: str, name: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{name} is not a safe GCP identifier")
    return value


def build_submission_command(
    runtime: CloudRuntime,
    *,
    dataset_version: str,
    run_id: str,
    script_path: Path = SOURCE_TO_BRONZE_SCRIPT,
    archive_path: Path = DEFAULT_ARCHIVE,
    jar_paths: Sequence[Path] | None = None,
) -> tuple[str, ...]:
    run_id = _validate_id(run_id, "run_id")
    dataset_version = _validate_id(dataset_version, "dataset_version")
    source_manifest = (
        f"gs://{runtime.data_bucket}/source/{dataset_version}/"
        "_control/dataset_manifest.json"
    )
    bronze_root = (
        f"gs://{runtime.data_bucket}/lakehouse/runs/{run_id}/bronze"
    )
    jars = tuple(jar_paths or dependency_jar_paths())
    properties = ",".join(
        (
            f"dataproc.tier={runtime.performance_tier}",
            "spark.sql.extensions=io.delta.sql.DeltaSparkSessionExtension",
            "spark.sql.catalog.spark_catalog=org.apache.spark.sql.delta.catalog.DeltaCatalog",
            "spark.sql.session.timeZone=UTC",
            "spark.eventLog.enabled=true",
            f"spark.eventLog.dir=gs://{runtime.data_bucket}/spark-events",
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
        f"--deps-bucket=gs://{runtime.temporary_bucket}",
        f"--staging-bucket={runtime.temporary_bucket}",
        "--ttl=2h",
        "--labels=component=source-bronze,phase=r4",
        f"--properties={properties}",
        "--",
        f"--source-manifest-uri={source_manifest}",
        f"--dataset-version={dataset_version}",
        f"--bronze-root={bronze_root}",
        f"--run-id={run_id}",
        f"--batch-id={run_id}",
        "--runtime-version=2.2",
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
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--terraform-output-json", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


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
        jar_paths = ensure_delta_dependencies()
        archive = build_python_archive(REPOSITORY_ROOT, DEFAULT_ARCHIVE)
    command = build_submission_command(
        runtime,
        dataset_version=args.dataset_version,
        run_id=args.run_id,
        archive_path=archive,
        jar_paths=jar_paths,
    )
    if not args.execute:
        print("DRY RUN — no Dataproc batch was submitted")
        print(shlex.join(command))
        return 0
    runner(command, check=True)
    print(f"R4 source-to-Bronze batch submitted: {args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
