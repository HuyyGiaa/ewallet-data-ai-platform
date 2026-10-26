"""Ingest one immutable source manifest into run-scoped Bronze Delta tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from pyspark.sql import DataFrame, SparkSession, functions as F
from data_platform.config.spark_profiles import validate_git_sha


PHYSICAL_TABLES = (
    "users",
    "accounts",
    "merchants",
    "devices",
    "transactions_v1",
    "transactions_v2",
    "balance_snapshots",
    "login_events",
    "fraud_labels",
)
BRONZE_TABLES = (
    "users",
    "accounts",
    "merchants",
    "devices",
    "transactions",
    "balance_snapshots",
    "login_events",
    "fraud_labels",
)
CANONICAL_MINIMUM_BYTES = 100_000_000_000


def _dataset_root(manifest_uri: str) -> str:
    suffix = "/_control/dataset_manifest.json"
    if not manifest_uri.startswith("gs://") or not manifest_uri.endswith(suffix):
        raise ValueError(
            "source manifest URI must end with /_control/dataset_manifest.json"
        )
    return manifest_uri[: -len(suffix)]


def validate_source_manifest(
    value: Mapping[str, Any], *, expected_dataset_version: str
) -> dict[str, Any]:
    if value.get("status") != "COMPLETE":
        raise ValueError("source dataset manifest must have COMPLETE status")
    if value.get("dataset_version") != expected_dataset_version:
        raise ValueError("source dataset version does not match submission")
    tables = value.get("tables")
    if not isinstance(tables, Mapping) or set(tables) != set(PHYSICAL_TABLES):
        raise ValueError("source physical table inventory is not exactly nine tables")
    if int(value.get("shard_count", 0)) <= 0:
        raise ValueError("source shard_count must be positive")

    total_bytes = 0
    for table in PHYSICAL_TABLES:
        metadata = tables[table]
        if not isinstance(metadata, Mapping):
            raise ValueError(f"source table metadata must be an object: {table}")
        files = metadata.get("files")
        schema_hashes = metadata.get("schema_sha256")
        if not isinstance(files, list) or len(files) != value["shard_count"]:
            raise ValueError(f"source table shard files are incomplete: {table}")
        if not isinstance(schema_hashes, list) or not schema_hashes:
            raise ValueError(f"source table schema hashes are missing: {table}")
        file_rows = sum(int(item["rows"]) for item in files)
        file_bytes = sum(int(item["bytes"]) for item in files)
        if file_rows != int(metadata["rows"]) or file_bytes != int(metadata["bytes"]):
            raise ValueError(f"source table row/byte totals differ from files: {table}")
        for item in files:
            if not item.get("sha256") or not item.get("md5_base64"):
                raise ValueError(f"source checksums are missing: {table}")
        total_bytes += file_bytes

    physical_transactions = (
        int(tables["transactions_v1"]["rows"])
        + int(tables["transactions_v2"]["rows"])
    )
    if physical_transactions != int(value["physical_transactions"]):
        raise ValueError("physical transaction total differs from source tables")
    if int(value["fraud_label_rows"]) != int(value["logical_transactions"]):
        raise ValueError("fraud label coverage must equal logical transactions")
    scenario_total = sum(int(item) for item in value["fraud_scenario_counts"].values())
    if scenario_total != int(value["fraud_rows"]):
        raise ValueError("fraud scenario total differs from fraud_rows")
    if total_bytes != int(value["total_bytes"]):
        raise ValueError("source total_bytes differs from table totals")
    if (
        expected_dataset_version == "canonical-100gb-v1"
        and total_bytes < CANONICAL_MINIMUM_BYTES
    ):
        raise ValueError("canonical source is below the strict 100 GB gate")
    return dict(value)


def source_file_uris(
    manifest_uri: str,
    manifest: Mapping[str, Any],
    table: str,
) -> tuple[str, ...]:
    if table not in PHYSICAL_TABLES:
        raise ValueError(f"unknown physical source table: {table}")
    root = _dataset_root(manifest_uri)
    result: list[str] = []
    for item in manifest["tables"][table]["files"]:
        path = PurePosixPath(str(item["path"]))
        if path.is_absolute() or ".." in path.parts or path.parts[0] != table:
            raise ValueError(f"unsafe source object path for {table}: {path}")
        result.append(f"{root}/{path.as_posix()}")
    return tuple(result)


def combine_transaction_versions(v1: DataFrame, v2: DataFrame) -> DataFrame:
    v1_with_channel = v1.withColumn("channel", F.lit(None).cast("string"))
    return v1_with_channel.unionByName(v2, allowMissingColumns=False)


def _download_json(uri: str) -> dict[str, Any]:
    from google.cloud import storage

    parsed = urlsplit(uri)
    value = json.loads(
        storage.Client().bucket(parsed.netloc).blob(
            parsed.path.lstrip("/")
        ).download_as_text()
    )
    if not isinstance(value, dict):
        raise ValueError("source manifest JSON must be an object")
    return value


def _expected_object_metadata(
    *, prefix: str, manifest: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    expected: dict[str, Mapping[str, Any]] = {}
    for table in PHYSICAL_TABLES:
        for item in manifest["tables"][table]["files"]:
            name = f"{prefix}/{item['path']}"
            if name in expected:
                raise ValueError(f"duplicate source object in manifest: {name}")
            expected[name] = item
    return expected


def _verify_gcs_objects(
    manifest_uri: str,
    manifest: Mapping[str, Any],
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Verify one paginated object inventory, reloading only absent MD5 values."""

    if client is None:
        from google.cloud import storage

        client = storage.Client()

    root = urlsplit(_dataset_root(manifest_uri))
    bucket = client.bucket(root.netloc)
    prefix = root.path.strip("/")
    expected = _expected_object_metadata(prefix=prefix, manifest=manifest)
    listed: dict[str, Any] = {}
    duplicates: set[str] = set()
    unexpected: set[str] = set()

    # ``list_blobs`` is paginated by the client. Control JSON is intentionally
    # outside the physical Parquet inventory represented by the manifest.
    for blob in client.list_blobs(root.netloc, prefix=f"{prefix}/"):
        name = str(blob.name)
        if name in listed:
            duplicates.add(name)
            continue
        listed[name] = blob
        if name not in expected and "/_control/" not in f"/{name}":
            unexpected.add(name)

    if duplicates:
        raise ValueError(
            "duplicate GCS source objects in listing: "
            + ", ".join(sorted(duplicates))
        )
    missing = set(expected) - set(listed)
    if missing:
        raise ValueError(
            "missing GCS source objects: " + ", ".join(sorted(missing))
        )
    if unexpected:
        raise ValueError(
            "unexpected GCS source objects: " + ", ".join(sorted(unexpected))
        )

    md5_fallback_reloads = 0
    generations: dict[str, int | None] = {}
    listed_bytes = 0
    for name in sorted(expected):
        item = expected[name]
        blob = listed[name]
        if getattr(blob, "md5_hash", None) is None:
            blob = bucket.blob(name)
            blob.reload()
            md5_fallback_reloads += 1
        size = int(blob.size)
        listed_bytes += size
        generations[name] = (
            int(blob.generation) if getattr(blob, "generation", None) else None
        )
        if size != int(item["bytes"]) or blob.md5_hash != item["md5_base64"]:
            raise ValueError(
                f"GCS source object metadata mismatch: gs://{root.netloc}/{name}"
            )
    if listed_bytes != int(manifest["total_bytes"]):
        raise ValueError("listed GCS source bytes differ from dataset manifest")
    return {
        "listed_object_count": len(expected),
        "listed_total_bytes": listed_bytes,
        "md5_fallback_reloads": md5_fallback_reloads,
        "generation_count": sum(value is not None for value in generations.values()),
    }


def _schema_sha256(dataframe: DataFrame) -> str:
    return hashlib.sha256(dataframe.schema.json().encode("utf-8")).hexdigest()


def _delta_version(table_uri: str) -> int:
    from delta.tables import DeltaTable

    return int(
        DeltaTable.forPath(SparkSession.getActiveSession(), table_uri)
        .history(1)
        .select("version")
        .first()["version"]
    )


def _gcs_path_stats(spark: SparkSession, uri: str) -> dict[str, int]:
    path = spark._jvm.org.apache.hadoop.fs.Path(uri)
    filesystem = path.getFileSystem(spark._jsc.hadoopConfiguration())
    summary = filesystem.getContentSummary(path)
    return {"bytes": int(summary.getLength()), "files": int(summary.getFileCount())}


def _upload_evidence(uri: str, evidence: Mapping[str, Any]) -> None:
    from google.cloud import storage

    parsed = urlsplit(uri)
    storage.Client().bucket(parsed.netloc).blob(
        parsed.path.lstrip("/")
    ).upload_from_string(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        content_type="application/json",
        if_generation_match=0,
    )


def run_ingestion(
    *,
    source_manifest_uri: str,
    dataset_version: str,
    bronze_root: str,
    run_id: str,
    batch_id: str,
    runtime_version: str,
    benchmark_id: str | None = None,
    spark_profile: str | None = None,
    git_sha: str | None = None,
) -> dict[str, Any]:
    manifest = validate_source_manifest(
        _download_json(source_manifest_uri),
        expected_dataset_version=dataset_version,
    )
    source_inventory = _verify_gcs_objects(source_manifest_uri, manifest)
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    spark = (
        SparkSession.builder.appName("ewallet-r4-source-to-bronze")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    physical = {
        table: spark.read.parquet(
            *source_file_uris(source_manifest_uri, manifest, table)
        )
        for table in PHYSICAL_TABLES
    }
    logical: dict[str, DataFrame] = {
        table: physical[table]
        for table in BRONZE_TABLES
        if table != "transactions"
    }
    logical["transactions"] = combine_transaction_versions(
        physical["transactions_v1"], physical["transactions_v2"]
    )

    metrics: dict[str, Any] = {}
    for table in BRONZE_TABLES:
        dataframe = logical[table]
        expected_rows = (
            int(manifest["physical_transactions"])
            if table == "transactions"
            else int(manifest["tables"][table]["rows"])
        )
        table_uri = f"{bronze_root.rstrip('/')}/{table}"
        dataframe.write.format("delta").mode("errorifexists").save(table_uri)
        read_back = spark.read.format("delta").load(table_uri)
        output_rows = read_back.count()
        if output_rows != expected_rows:
            raise RuntimeError(
                f"Bronze read-back mismatch for {table}: expected {expected_rows}, got {output_rows}"
            )
        metrics[table] = {
            # The immutable manifest is the source count contract; read-back
            # proves the materialized output contains that exact count without
            # an additional pre-write full scan.
            "source_rows": expected_rows,
            "bronze_rows": output_rows,
            "schema_sha256": _schema_sha256(read_back),
            "delta_version": _delta_version(table_uri),
            **_gcs_path_stats(spark, table_uri),
        }

    transactions = spark.read.format("delta").load(
        f"{bronze_root.rstrip('/')}/transactions"
    )
    transaction_summary = transactions.agg(
        F.count("*").alias("physical_rows"),
        F.countDistinct("transaction_id").alias("logical_rows"),
    ).first()
    duplicate_rows = int(transaction_summary["physical_rows"] - transaction_summary["logical_rows"])
    if int(transaction_summary["logical_rows"]) != int(manifest["logical_transactions"]):
        raise RuntimeError("Bronze logical transaction count mismatch")
    labels = spark.read.format("delta").load(
        f"{bronze_root.rstrip('/')}/fraud_labels"
    )
    label_summary = labels.agg(
        F.count("*").alias("rows"),
        F.countDistinct("transaction_id").alias("distinct_transactions"),
        F.sum(F.col("label").cast("long")).alias("fraud_rows"),
    ).first()
    if int(label_summary["rows"]) != int(manifest["fraud_label_rows"]) or int(
        label_summary["distinct_transactions"]
    ) != int(manifest["logical_transactions"]):
        raise RuntimeError("Bronze fraud label coverage mismatch")
    if int(label_summary["fraud_rows"]) != int(manifest["fraud_rows"]):
        raise RuntimeError("Bronze fraud row count mismatch")
    scenario_counts = {
        row["fraud_type"]: int(row["count"])
        for row in labels.where(F.col("label") == 1)
        .groupBy("fraud_type")
        .count()
        .collect()
    }
    expected_scenarios = {
        str(name): int(count)
        for name, count in manifest["fraud_scenario_counts"].items()
    }
    if scenario_counts != expected_scenarios:
        raise RuntimeError("Bronze fraud scenario counts mismatch")

    evidence_uri = f"{bronze_root.rsplit('/bronze', 1)[0]}/evidence/source_to_bronze.json"
    evidence = {
        "schema_version": "r4-source-to-bronze-v1",
        "status": "PASS",
        "run_id": run_id,
        "batch_id": batch_id,
        "runtime_version": runtime_version,
        "spark_version": spark.version,
        "spark_application_id": spark.sparkContext.applicationId,
        "dataset_version": dataset_version,
        "benchmark_id": benchmark_id,
        "spark_profile": spark_profile,
        "git_sha": git_sha,
        "source_manifest_uri": source_manifest_uri,
        "bronze_root": bronze_root.rstrip("/"),
        "source_total_bytes": int(manifest["total_bytes"]),
        "source_file_count": sum(
            len(manifest["tables"][table]["files"]) for table in PHYSICAL_TABLES
        ),
        "source_inventory": source_inventory,
        "logical_transactions": int(transaction_summary["logical_rows"]),
        "physical_transactions": int(transaction_summary["physical_rows"]),
        "duplicate_rows": duplicate_rows,
        "fraud_label_rows": int(manifest["fraud_label_rows"]),
        "fraud_rows": int(manifest["fraud_rows"]),
        "fraud_scenario_counts": scenario_counts,
        "tables": metrics,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "evidence_uri": evidence_uri,
    }
    _upload_evidence(evidence_uri, evidence)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    spark.stop()
    return evidence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest-uri", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--bronze-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--runtime-version", default="2.2")
    parser.add_argument("--benchmark-id")
    parser.add_argument("--spark-profile", choices=("baseline", "optimized"))
    parser.add_argument("--git-sha")
    args = parser.parse_args(argv)
    if len(
        [value for value in (args.benchmark_id, args.spark_profile, args.git_sha) if value]
    ) not in (0, 3):
        parser.error("benchmark-id, spark-profile and git-sha must be supplied together")
    if args.git_sha:
        validate_git_sha(args.git_sha)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_ingestion(
        source_manifest_uri=args.source_manifest_uri,
        dataset_version=args.dataset_version,
        bronze_root=args.bronze_root,
        run_id=args.run_id,
        batch_id=args.batch_id,
        runtime_version=args.runtime_version,
        benchmark_id=args.benchmark_id,
        spark_profile=args.spark_profile,
        git_sha=args.git_sha,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
