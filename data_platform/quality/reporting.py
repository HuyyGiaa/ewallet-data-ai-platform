"""Machine-readable validator reports and the shared cloud-run arguments.

A stage validator writes one JSON report next to the run it validated
(``.../runs/<run>/evidence/``). Writing refuses to replace an existing object, so
a report is immutable, and the validated Delta tables are never written.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from data_platform.quality.results import ValidationResult


REPORT_SCHEMA = "campaign-validation-report-v1"


def add_cloud_arguments(parser: argparse.ArgumentParser) -> None:
    """Arguments a Dataproc validator batch needs to resolve its runtime profile."""

    parser.add_argument("--project-id")
    parser.add_argument("--region")
    parser.add_argument("--data-bucket")
    parser.add_argument("--dataset-version", help="Recorded in the validation report.")
    parser.add_argument("--run-id", help="Recorded in the validation report.")
    parser.add_argument("--report-uri", help="Write a JSON report here; refuses to overwrite.")


def cloud_environ(args: argparse.Namespace) -> Mapping[str, str]:
    """Environment that resolves the gcp profile placeholders on a Dataproc driver."""

    if args.environment != "gcp" or not args.project_id:
        return os.environ
    return {
        **os.environ,
        "GCP_PROJECT_ID": args.project_id,
        "GCP_REGION": args.region or "",
        "EWALLET_DATA_BUCKET": args.data_bucket or "",
        "DATAHUB_GMS_URL": os.environ.get("DATAHUB_GMS_URL", "http://datahub-gms.invalid:8080"),
    }


def build_report(
    scope: str,
    results: Sequence[ValidationResult],
    *,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    failed = [result.rule_name for result in results if not result.passed]
    return {
        "schema_version": REPORT_SCHEMA,
        "scope": scope,
        "status": "PASS" if results and not failed else "FAIL",
        "passed_rules": len(results) - len(failed),
        "failed_rules": failed,
        "results": [
            {
                "rule_name": result.rule_name,
                "passed": bool(result.passed),
                "actual_value": str(result.actual_value),
                "expected": result.expected,
            }
            for result in results
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **dict(context or {}),
    }


def write_report(spark: Any, uri: str, report: Mapping[str, Any]) -> None:
    """Create the report through the Hadoop filesystem (gs:// and file://)."""

    jvm = spark.sparkContext._jvm
    conf = spark.sparkContext._jsc.hadoopConfiguration()
    path = jvm.org.apache.hadoop.fs.Path(uri)
    filesystem = path.getFileSystem(conf)
    stream = filesystem.create(path, False)
    try:
        stream.write(bytearray((json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")))
    finally:
        stream.close()


def report_context(args: argparse.Namespace, **extra: Any) -> dict[str, Any]:
    return {"dataset_version": args.dataset_version, "run_id": args.run_id, **extra}
