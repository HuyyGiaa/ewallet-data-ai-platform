"""Export a memory-safe Silver source for chunked F7 materialization."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ai_platform.ml import ML_DATA_ROOT
from ai_platform.storage import join_uri, normalize_uri, require_absent, write_json
from ai_platform.ml.training.source_contract import SOURCE_OUTPUT_COLUMNS
from ai_platform.ml.training.temporal import TEST_START, VALIDATION_START
from data_platform.config import (
    RuntimeConfig,
    load_runtime_config,
    load_runtime_config_file,
)
from data_platform.config.spark_profiles import SILVER_STAGE, validate_git_sha
from data_platform.processing.spark.common.runtime_contract import (
    validate_gcp_spark_runtime,
)


DEFAULT_OUTPUT = ML_DATA_ROOT / "fraud_training_v1" / "trainable_source"
OUTPUT_COLUMNS = SOURCE_OUTPUT_COLUMNS


def bounded_sampling_sql(sample_modulus: int) -> str:
    """Return the frozen, label-independent Spark sampling predicate."""

    if sample_modulus <= 0:
        raise ValueError("sample_modulus must be positive")
    return f"pmod(xxhash64(event_timestamp_us), {sample_modulus}) = 0"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--environment",
        choices=("local", "dev", "gcp"),
        default="local",
    )
    parser.add_argument("--output", "--output-root", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--silver-root")
    parser.add_argument("--dataset-version")
    parser.add_argument("--source-silver-run-id")
    parser.add_argument("--source-feature-run-id")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--source-git-sha")
    parser.add_argument("--execution-git-sha")
    parser.add_argument("--feature-selection-digest")
    parser.add_argument("--runtime-config-file", type=Path)
    parser.add_argument("--project-id")
    parser.add_argument("--region")
    parser.add_argument("--data-bucket")
    parser.add_argument(
        "--sample-modulus",
        type=int,
        default=1,
        help="1 exports the full canonical population; N>1 keeps hash remainder 0/N.",
    )
    return parser.parse_args(argv)


def apply_silver_root(config: RuntimeConfig, silver_root: str) -> RuntimeConfig:
    """Bind bounded source export to one immutable canonical Silver run."""
    if config.environment != "gcp" or config.storage.scheme != "gs":
        raise ValueError("run-scoped training source requires the gcp profile")
    if (
        not silver_root.startswith("gs://")
        or "/lakehouse/runs/" not in silver_root
        or not silver_root.endswith("/silver")
    ):
        raise ValueError("Silver root must be a run-scoped gs://.../silver path")
    return replace(
        config,
        storage=replace(config.storage, silver_root=silver_root),
    )


def request_profile(frame) -> dict:
    from pyspark.sql import functions as F

    overall = frame.agg(
        F.count("*").alias("rows"),
        F.sum(F.col("amount").isNull().cast("long")).alias("amount_null"),
        F.countDistinct("amount").alias("amount_distinct"),
        F.min("amount").alias("amount_min"),
        F.max("amount").alias("amount_max"),
        F.sum(F.col("type").isNull().cast("long")).alias("type_null"),
        F.countDistinct("type").alias("type_distinct"),
        F.sort_array(F.collect_set("type")).alias("type_values"),
        F.sum(F.col("channel").isNull().cast("long")).alias("channel_null"),
        F.countDistinct("channel").alias("channel_distinct"),
        F.sort_array(F.collect_set("channel")).alias("channel_values"),
        F.sum(F.col("merchant_present").isNull().cast("long")).alias(
            "merchant_present_null"
        ),
        F.countDistinct("merchant_present").alias("merchant_present_distinct"),
        F.sort_array(F.collect_set("merchant_present")).alias(
            "merchant_present_values"
        ),
    ).first()

    def grouped(column: str) -> dict[str, int]:
        return {
            f"{row.split}:{getattr(row, column)}": row["count"]
            for row in frame.groupBy("split", column)
            .count()
            .orderBy("split", column)
            .collect()
        }

    return {
        "rows": overall.rows,
        "amount": {
            "null_count": overall.amount_null,
            "distinct_count": overall.amount_distinct,
            "min": overall.amount_min,
            "max": overall.amount_max,
        },
        "type": {
            "null_count": overall.type_null,
            "distinct_count": overall.type_distinct,
            "values": overall.type_values,
            "split_distribution": grouped("type"),
        },
        "channel": {
            "null_count": overall.channel_null,
            "distinct_count": overall.channel_distinct,
            "values": overall.channel_values,
            "split_distribution": grouped("channel"),
        },
        "merchant_present": {
            "null_count": overall.merchant_present_null,
            "distinct_count": overall.merchant_present_distinct,
            "values": overall.merchant_present_values,
            "split_distribution": grouped("merchant_present"),
        },
    }


def split_profile(frame) -> dict[str, dict[str, object]]:
    from pyspark.sql import functions as F

    rows = frame.groupBy("split").agg(
        F.count("*").alias("rows"),
        F.sum(F.col("label")).alias("fraud"),
        F.sum((F.col("label") == 0).cast("long")).alias("normal"),
        F.min("event_timestamp_us").alias("min_timestamp_us"),
        F.max("event_timestamp_us").alias("max_timestamp_us"),
    )
    scenarios = {
        split: {}
        for split in ("train", "validation", "test")
    }
    for row in (
        frame.filter(F.col("label") == 1)
        .groupBy("split", "fraud_type")
        .count()
        .collect()
    ):
        scenarios[row.split][row.fraud_type] = row["count"]
    return {
        row.split: {
            "rows": row.rows,
            "normal": row.normal,
            "fraud": row.fraud,
            "fraud_rate": row.fraud / row.rows,
            "min_timestamp_us": row.min_timestamp_us,
            "max_timestamp_us": row.max_timestamp_us,
            "fraud_types": scenarios[row.split],
        }
        for row in rows.collect()
    }


def export_training_source(
    output: str | Path,
    sample_modulus: int,
    runtime_config: RuntimeConfig,
    *,
    dataset_version: str | None = None,
    source_silver_run_id: str | None = None,
    source_feature_run_id: str | None = None,
    snapshot_id: str | None = None,
    source_git_sha: str | None = None,
    execution_git_sha: str | None = None,
    feature_selection_digest: str | None = None,
) -> dict:
    from data_platform.processing.spark.common.spark_session import (
        create_spark_session,
    )

    if sample_modulus <= 0:
        raise ValueError("sample_modulus must be positive")
    spark = create_spark_session(
        "F7-Training-Source-Export",
        stage=SILVER_STAGE,
        runtime_config=runtime_config,
    )
    try:
        from pyspark.sql import functions as F

        from ai_platform.ml.training.export_source_sample import (
            audit_label_integrity,
            read_delta,
        )

        output_uri = normalize_uri(output)
        validate_gcp_spark_runtime(
            spark,
            runtime_config,
            output_uri=output_uri,
        )
        transactions = read_delta(spark, runtime_config, "transactions").select(
            "transaction_id",
            "user_id",
            "account_id",
            "device_id",
            "merchant_id",
            "amount",
            "type",
            "channel",
            "timestamp",
        )
        labels = read_delta(spark, runtime_config, "fraud_labels").select(
            "transaction_id", "label", "fraud_type"
        )
        label_integrity = audit_label_integrity(transactions, labels)
        validation_us = int(VALIDATION_START.timestamp() * 1_000_000)
        test_us = int(TEST_START.timestamp() * 1_000_000)
        canonical = (
            transactions.join(labels, "transaction_id", "inner")
            .withColumn("event_timestamp_us", F.expr("unix_micros(timestamp)"))
            .withColumn(
                "split",
                F.when(F.col("event_timestamp_us") < validation_us, "train")
                .when(F.col("event_timestamp_us") < test_us, "validation")
                .otherwise("test"),
            )
            .withColumn("merchant_present", F.col("merchant_id").isNotNull())
            .select(*OUTPUT_COLUMNS)
        )
        canonical_request_profile = request_profile(canonical)
        selected = canonical
        if sample_modulus > 1:
            selected = canonical.filter(F.expr(bounded_sampling_sql(sample_modulus)))
        selected_profile = split_profile(selected)
        if set(selected_profile) != {"train", "validation", "test"}:
            raise ValueError("Bounded source does not contain all chronological splits")

        is_gcp = runtime_config.environment == "gcp"
        if is_gcp:
            require_absent(output_uri)
        else:
            output_path = Path(output_uri)
            shutil.rmtree(output_path, ignore_errors=True)
        (
            selected.repartition("split")
            .write.mode("errorifexists" if is_gcp else "overwrite")
            .partitionBy("split")
            .parquet(output_uri)
        )
        written_rows = spark.read.parquet(output_uri).count()
        expected_rows = sum(item["rows"] for item in selected_profile.values())
        if written_rows != expected_rows:
            raise ValueError(
                f"Training source write mismatch: {written_rows} != {expected_rows}"
            )
        manifest = {
            "schema_version": "f7-training-source-v1",
            "status": "COMPLETE",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "source_git_head": execution_git_sha
            or subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "source": "active Silver Delta transactions + fraud_labels",
            "scope": "full_canonical" if sample_modulus == 1 else "hash_bounded",
            "sample_modulus": sample_modulus,
            "sampling_key": "event_timestamp_us",
            "sampling_reason": (
                "Label-independent deterministic selection that retains all "
                "transactions sharing an event timestamp."
            ),
            "label_integrity": label_integrity,
            "canonical_request_profile": canonical_request_profile,
            "selected_split_profile": selected_profile,
            "timestamp_storage": "signed UTC epoch microseconds",
            "output_columns": list(OUTPUT_COLUMNS),
            "written_rows": written_rows,
            "lineage": {
                "dataset_version": dataset_version,
                "source_silver_run_id": source_silver_run_id,
                "source_feature_run_id": source_feature_run_id,
                "source_silver_root": runtime_config.storage.silver_root,
                "snapshot_id": snapshot_id,
                "source_git_sha": source_git_sha,
                "feature_selection_digest": feature_selection_digest,
            },
        }
        write_json(join_uri(output_uri, "manifest.json"), manifest)
        print(f"PASS | training source export | rows={written_rows}")
        return manifest
    finally:
        spark.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        runtime_environ = None
        if args.environment == "gcp":
            if not all((args.project_id, args.region, args.data_bucket)):
                raise ValueError("GCP export requires project, region and data bucket")
            if args.silver_root and not args.silver_root.startswith(
                f"gs://{args.data_bucket}/"
            ):
                raise ValueError("GCP Silver root and data bucket mismatch")
            runtime_environ = {
                **os.environ,
                "GCP_PROJECT_ID": args.project_id,
                "GCP_REGION": args.region,
                "EWALLET_DATA_BUCKET": args.data_bucket,
                "DATAHUB_GMS_URL": (
                    os.environ.get("DATAHUB_GMS_URL")
                    or "http://datahub-gms.invalid:8080"
                ),
            }
        config = (
            load_runtime_config_file(
                args.runtime_config_file, args.environment,
                environ=runtime_environ,
            )
            if args.runtime_config_file is not None
            else load_runtime_config(args.environment, environ=runtime_environ)
        )
        cloud_lineage = (
            args.silver_root,
            args.dataset_version,
            args.source_silver_run_id,
            args.source_feature_run_id,
            args.snapshot_id,
            args.source_git_sha,
            args.execution_git_sha,
            args.feature_selection_digest,
        )
        if args.environment == "gcp":
            if any(value is None for value in cloud_lineage):
                raise ValueError(
                    "gcp source export requires --silver-root, --dataset-version, "
                    "--source-silver-run-id, --snapshot-id, and --source-git-sha"
                )
            validate_git_sha(args.source_git_sha)
            validate_git_sha(args.execution_git_sha)
            bucket = args.silver_root.removeprefix("gs://").split("/", 1)[0]
            expected_silver = (
                f"gs://{bucket}/lakehouse/runs/{args.source_silver_run_id}/silver"
            )
            if args.silver_root != expected_silver:
                raise ValueError("gcp training source Silver lineage is inconsistent")
            config = apply_silver_root(config, args.silver_root)
            expected_output = (
                f"gs://{bucket}/"
                f"ml/training-staging/{args.dataset_version}/{args.snapshot_id}/source"
            )
            if normalize_uri(args.output) != expected_output:
                raise ValueError("gcp training source output root is not canonical")
        export_training_source(
            args.output,
            args.sample_modulus,
            config,
            dataset_version=args.dataset_version,
            source_silver_run_id=args.source_silver_run_id,
            source_feature_run_id=args.source_feature_run_id,
            snapshot_id=args.snapshot_id,
            source_git_sha=args.source_git_sha,
            execution_git_sha=args.execution_git_sha,
            feature_selection_digest=args.feature_selection_digest,
        )
    except Exception as exc:
        print(f"ERROR | {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
