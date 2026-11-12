"""Local Spark 3.5 + Delta 3.2 run of the storage benchmark job on a real Delta table.

Exercises the production ``execute_plan`` on a small partitioned table shaped like
Gold ``fact_transactions`` (one file per daily partition, as in the 5GB campaign):

* shallow clones pinned to the source version leave every source file and the
  source Delta log byte-identical, even after OPTIMIZE, Z-order and a VACUUM with
  zero retention on a clone;
* bin-packing compaction of one-file partitions is detected as a no-op;
* Z-order rewrites files and keeps the data logically equivalent;
* query results match across arms, Spark scan metrics are read from the executed
  plan, and the partition-pruned query reads fewer files than the unpruned one.

The GCS-only path guards are relaxed to ``file://`` inside this test only. Skipped
unless PySpark 3.5 and the Delta 3.2 jars are available::

    EDAI1_DELTA_JARS=/path/delta-spark_2.12-3.2.0.jar,/path/delta-storage-3.2.0.jar \\
        <python with pyspark 3.5.x and delta-spark 3.2.x> -m pytest -q tests/integration/test_storage_benchmark_delta_local.py
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

pyspark = pytest.importorskip("pyspark")
JARS = os.environ.get("EDAI1_DELTA_JARS", "")
pytestmark = pytest.mark.skipif(
    not JARS or not pyspark.__version__.startswith("3.5.") or not all(Path(p).is_file() for p in JARS.split(",")),
    reason="needs PySpark 3.5.x and EDAI1_DELTA_JARS pointing at the Delta 3.2 jars",
)

from data_platform.storage import cloud_optimization  # noqa: E402
from ops.gcp import storage_benchmark_job as job  # noqa: E402

DAYS = 20
ROWS_PER_DAY = 1500
MERCHANTS = 300


def _tree_digest(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.fixture(scope="module")
def spark():
    from pyspark.sql import SparkSession

    session = (SparkSession.builder.master("local[2]").appName("storage-benchmark-local")
               .config("spark.jars", JARS)
               .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
               .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
               .config("spark.sql.session.timeZone", "UTC")
               .config("spark.sql.shuffle.partitions", "4")
               .config("spark.ui.enabled", "false")
               .getOrCreate())
    yield session
    session.stop()


def _write_source(spark, path: Path) -> None:
    from pyspark.sql import functions as F

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows = []
    for day in range(DAYS):
        for index in range(ROWS_PER_DAY):
            stamp = start + timedelta(days=day, seconds=index * 50)
            rows.append((f"t{day:03d}{index:05d}", f"u{index % 97}", f"a{index % 89}", f"m{(index * 7) % MERCHANTS:04d}",
                         f"d{index % 83}", None, int(stamp.strftime("%Y%m%d")), "payment",
                         "failed" if index % 13 == 0 else "success", "app", "VND", float(index % 500 + 1),
                         1000.0, 900.0, int(stamp.timestamp()), int(stamp.timestamp())))
    columns = ["transaction_id", "user_id", "account_id", "merchant_id", "device_id", "counterparty_account_id",
               "date_key", "type", "status", "channel", "currency", "amount", "old_balance", "new_balance",
               "timestamp", "ingested_at"]
    schema = ", ".join(f"{name} {kind}" for name, kind in zip(columns, (
        "string", "string", "string", "string", "string", "string", "int", "string", "string", "string",
        "string", "double", "double", "double", "long", "long")))
    # Epoch seconds converted inside Spark: naive Python datetimes would use the host time zone.
    frame = (spark.createDataFrame(rows, schema)
             .withColumn("timestamp", F.timestamp_seconds("timestamp"))
             .withColumn("ingested_at", F.timestamp_seconds("ingested_at"))
             .withColumn("event_date", F.to_date("timestamp")))
    (frame.repartition("event_date").write.format("delta").partitionBy("event_date")
     .save(str(path)))


def _layout(path: Path) -> dict[str, object]:
    actions = []
    for line in (path / "_delta_log" / "00000000000000000000.json").read_text().splitlines():
        item = json.loads(line)
        if "add" in item:
            add = item["add"]
            stats = json.loads(add.get("stats") or "{}")
            actions.append({"path": add["path"], "size_bytes": add["size"], "num_records": stats.get("numRecords"),
                            "partition_values": {k: v for k, v in add["partitionValues"].items()},
                            "min_values": stats.get("minValues") or {}, "max_values": stats.get("maxValues") or {}})
    return cloud_optimization.summarize_layout(table="fact_transactions", stage="dp2-gold", uri=path.as_uri(),
                                               delta_version=0, partition_columns=("event_date",), properties={},
                                               actions=actions)


def test_benchmark_job_is_safe_and_measures_every_arm(spark, tmp_path, monkeypatch):
    from delta.tables import DeltaTable

    source = tmp_path / "lakehouse/runs/ab-20261110225456-optimized-gold-r1/gold/fact_transactions"
    root = tmp_path / "lakehouse/benchmarks/storage/storage-local-test"
    _write_source(spark, source)
    before = _tree_digest(source)
    layout = _layout(source)
    assert layout["file_count"] == DAYS and layout["partition_count"] == DAYS

    end = date(2026, 5, 1) + timedelta(days=DAYS - 1)
    table = {
        **layout,
        "selected": True,
        "optimization": {"action": "ZORDER", "columns": ["merchant_id"]},
        "workload_parameters": {"window_start": str(end - timedelta(days=6)), "window_end": str(end),
                                "merchant_id": "m0007"},
        "queries": [
            {"name": "window_aggregation", "class": "date_range", "require_non_empty": True},
            {"name": "window_merchant_lookup", "class": "date_entity_lookup", "require_non_empty": True},
            {"name": "window_failed_by_channel", "class": "date_status_aggregation", "require_non_empty": True},
        ],
        "baseline_uri": (root / "baseline/dp2-gold/fact_transactions").as_uri(),
        "compacted_uri": (root / "compacted/dp2-gold/fact_transactions").as_uri(),
        "optimized_uri": (root / "optimized/dp2-gold/fact_transactions").as_uri(),
    }
    plan = {"benchmark_root": root.as_uri(), "dataset_version": "rehearsal-5gb-v1", "git_sha": "a" * 40,
            "source_spark_git_sha": "b" * 40, "benchmark_id": "storage-local-test", "source_run_ids": ["r"],
            "tables": [table]}

    def local_paths(**kwargs):
        values = [value for value in kwargs.values() if value]
        assert all(value.startswith("file://") for value in values)
        assert all(value.startswith(kwargs["benchmark_root"]) for key, value in kwargs.items()
                   if key.endswith("_uri") and key != "source_uri" and value)
        assert not kwargs["source_uri"].startswith(kwargs["benchmark_root"])

    monkeypatch.setattr(job, "validate_benchmark_paths", local_paths)
    monkeypatch.setattr(job, "_safe_delta_uri", lambda uri: uri.rstrip("/"))
    monkeypatch.setattr(spark, "stop", lambda: None)  # execute_plan stops its session; keep the fixture's

    evidence = job.execute_plan(plan=plan, benchmark_root=root.as_uri(), dataset_version="rehearsal-5gb-v1",
                                git_sha="a" * 40)
    result = evidence["tables"][0]

    # Compaction of one-file partitions rewrites nothing; Z-order rewrites every partition.
    assert result["rewrite"]["compaction"]["outcome"]["no_op"] is True
    assert result["rewrite"]["zorder"]["outcome"]["files_removed"] == DAYS
    assert result["recommendation"]["compaction"].startswith("NOT_NEEDED")
    assert all(item["equivalent"] for item in result["equivalence"].values())
    for arm in result["comparisons"].values():
        assert all(item["output_equivalent"] for item in arm)
        assert all(item["verdict"] in cloud_optimization.VERDICTS for item in arm)
    lookup = next(item for item in result["queries_before"] if item["name"] == "window_aggregation")
    assert lookup["scan_metrics"]["available"], lookup["scan_metrics"]
    assert len(lookup["runtime_samples_seconds"]) == job.QUERY_MEASURED_ITERATIONS

    pruning = result["partition_pruning"]["sides"]
    files = {side: sum(scan["files_read"] for scan in pruning[side]["scan_metrics"]["scans"]) for side in pruning}
    # Partition pruning and per-file timestamp statistics both select the 7 window
    # files on a one-day-per-file layout; only the full-scan control reads them all.
    assert files == {"pruned": 7, "stats_only": 7, "full_scan": DAYS}
    assert len({pruning[side]["result_json"] for side in pruning}) == 1

    written = job.write_evidence(evidence, job.local_writer(tmp_path / "evidence-out"))
    assert {"comparison.json", "comparison.csv", "rewrite.json", "partition_pruning.json"} <= set(written)

    # Source immutability, including after a zero-retention VACUUM of a rewritten clone.
    spark.conf.set("spark.databricks.delta.retentionDurationCheck.enabled", "false")
    DeltaTable.forPath(spark, table["optimized_uri"]).vacuum(0.0)
    assert _tree_digest(source) == before
    assert DeltaTable.forPath(spark, source.as_uri()).toDF().count() == DAYS * ROWS_PER_DAY
