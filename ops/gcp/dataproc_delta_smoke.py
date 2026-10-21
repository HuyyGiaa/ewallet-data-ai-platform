"""Bounded Delta Lake write/append/read smoke for Dataproc Serverless."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Sequence
from urllib.parse import urlsplit


def fixture_rows() -> tuple[dict[str, object], ...]:
    """Return the fixed six-row fixture used by every cloud smoke run."""

    return (
        {
            "transaction_id": "txn-0001",
            "user_id": "user-001",
            "amount": 10.00,
            "event_timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
        {
            "transaction_id": "txn-0002",
            "user_id": "user-001",
            "amount": 20.25,
            "event_timestamp": datetime(
                2026,
                1,
                1,
                0,
                0,
                0,
                123456,
                tzinfo=timezone.utc,
            ),
        },
        {
            "transaction_id": "txn-0003",
            "user_id": "user-002",
            "amount": 30.50,
            "event_timestamp": datetime(
                2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc
            ),
        },
        {
            "transaction_id": "txn-0004",
            "user_id": "user-002",
            "amount": 40.00,
            "event_timestamp": datetime(
                2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc
            ),
        },
        {
            "transaction_id": "txn-0005",
            "user_id": "user-003",
            "amount": 50.00,
            "event_timestamp": datetime(
                2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc
            ),
        },
        {
            "transaction_id": "txn-0006",
            "user_id": "user-003",
            "amount": 60.00,
            "event_timestamp": datetime(
                2026, 1, 1, 0, 0, 4, tzinfo=timezone.utc
            ),
        },
    )


def expected_summary() -> dict[str, object]:
    """Return the independently reviewable smoke acceptance contract."""

    return {
        "initial_rows": 4,
        "final_rows": 6,
        "distinct_transaction_ids": 6,
        "amount_sum": 210.75,
        "initial_delta_version": 0,
        "final_delta_version": 1,
        "microsecond_timestamp": "2026-01-01T00:00:00.123456+00:00",
    }


def validate_run_uri(run_uri: str) -> str:
    """Validate and normalize a non-root GCS prefix for one smoke run."""

    if not isinstance(run_uri, str):
        raise ValueError("run URI must be a string")
    normalized = run_uri.rstrip("/")
    parsed = urlsplit(normalized)
    path = PurePosixPath(parsed.path)
    if (
        parsed.scheme != "gs"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or str(path) in (".", "/")
        or ".." in path.parts
    ):
        raise ValueError(
            "run URI must be a non-root gs://bucket/prefix without query, "
            "fragment, or parent traversal"
        )
    return normalized


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _upload_json(evidence_uri: str, evidence: dict[str, Any]) -> None:
    from google.cloud import storage

    parsed = urlsplit(evidence_uri)
    blob_name = parsed.path.lstrip("/")
    client = storage.Client()
    client.bucket(parsed.netloc).blob(blob_name).upload_from_string(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        content_type="application/json",
        if_generation_match=0,
    )


def run_smoke(
    run_uri: str,
    *,
    batch_id: str,
    runtime_version: str,
) -> dict[str, Any]:
    """Execute the bounded Spark smoke and return verified evidence."""

    from delta.tables import DeltaTable
    from pyspark.sql import SparkSession, functions as F
    from pyspark.sql.types import (
        DoubleType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    normalized_run_uri = validate_run_uri(run_uri)
    table_uri = f"{normalized_run_uri}/table"
    evidence_uri = f"{normalized_run_uri}/evidence.json"
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()

    spark = (
        SparkSession.builder.appName("ewallet-r3-dataproc-delta-smoke")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    schema = StructType(
        [
            StructField("transaction_id", StringType(), nullable=False),
            StructField("user_id", StringType(), nullable=False),
            StructField("amount", DoubleType(), nullable=False),
            StructField("event_timestamp", TimestampType(), nullable=False),
        ]
    )
    rows = fixture_rows()
    initial = spark.createDataFrame(rows[:4], schema=schema)
    appended = spark.createDataFrame(rows[4:], schema=schema)

    initial.write.format("delta").mode("errorifexists").save(table_uri)
    initial_version = DeltaTable.forPath(spark, table_uri).history(1).select(
        "version"
    ).first()["version"]

    appended.write.format("delta").mode("append").save(table_uri)
    final_version = DeltaTable.forPath(spark, table_uri).history(1).select(
        "version"
    ).first()["version"]

    read_back = spark.read.format("delta").load(table_uri)
    aggregates = read_back.agg(
        F.count("*").alias("final_rows"),
        F.countDistinct("transaction_id").alias("distinct_transaction_ids"),
        F.sum("amount").alias("amount_sum"),
    ).first()
    precision_timestamp = (
        read_back.where(F.col("transaction_id") == "txn-0002")
        .select("event_timestamp")
        .first()["event_timestamp"]
    )
    precision_iso = _utc_iso(precision_timestamp)

    actual = {
        "initial_rows": initial.count(),
        "final_rows": aggregates["final_rows"],
        "distinct_transaction_ids": aggregates["distinct_transaction_ids"],
        "amount_sum": round(float(aggregates["amount_sum"]), 2),
        "initial_delta_version": int(initial_version),
        "final_delta_version": int(final_version),
        "microsecond_timestamp": precision_iso,
    }
    expected = expected_summary()
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise RuntimeError(
            "Delta-on-GCS smoke read-back mismatch: "
            + json.dumps(mismatches, sort_keys=True)
        )

    finished_at = datetime.now(timezone.utc)
    evidence: dict[str, Any] = {
        "schema_version": "r3-dataproc-delta-smoke-v1",
        "status": "PASS",
        "batch_id": batch_id,
        "runtime_version": runtime_version,
        "spark_version": spark.version,
        "spark_application_id": spark.sparkContext.applicationId,
        "run_uri": normalized_run_uri,
        "table_uri": table_uri,
        "evidence_uri": evidence_uri,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(time.monotonic() - started_monotonic, 3),
        **actual,
        "timestamp_precision_pass": precision_timestamp.microsecond % 1000 != 0,
        "schema": [
            {"name": field.name, "type": field.dataType.simpleString()}
            for field in read_back.schema.fields
        ],
    }
    _upload_json(evidence_uri, evidence)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    spark.stop()
    return evidence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-uri", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--runtime-version", default="2.2")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_smoke(
        args.run_uri,
        batch_id=args.batch_id,
        runtime_version=args.runtime_version,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
