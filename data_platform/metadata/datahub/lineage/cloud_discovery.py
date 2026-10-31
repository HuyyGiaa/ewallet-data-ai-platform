"""Discover cloud datasets and lineage from immutable GCS/Dataproc evidence.

This command is read-only.  It opens the canonical source Parquet schemas and
the Delta transaction logs referenced by successful Dataproc batch arguments.
No dataset is emitted unless its physical metadata can be read successfully.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ENV = "PROD"
BUCKET = "ewallet-data-ai-platform-hg26-data"
SOURCE_ROOT = f"gs://{BUCKET}/source/canonical-100gb-v1"
PHYSICAL_SOURCE = (
    "users", "accounts", "merchants", "devices", "transactions_v1",
    "transactions_v2", "balance_snapshots", "login_events", "fraud_labels",
)
BRONZE_SILVER = (
    "users", "accounts", "merchants", "devices", "transactions",
    "balance_snapshots", "login_events", "fraud_labels",
)
CORE_GOLD = (
    "dim_user", "dim_account", "dim_merchant", "dim_device", "dim_date",
    "fact_transactions", "fact_login_events", "fact_balance_snapshot",
    "obt_transaction_enriched", "feat_user_90d", "opt_merchant_performance",
)
FEATURES = (
    "feat_user_behavior", "feat_account_behavior", "feat_device_behavior",
    "feat_merchant_behavior",
)


def dataset_urn(uri: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:gcs,{uri},{ENV})"


def _args(batch: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in batch.get("args", []):
        if item.startswith("--") and "=" in item:
            key, value = item[2:].split("=", 1)
            result[key] = value
    return result


def _select_batches(values: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for item in values:
        labels = item.get("labels") or {}
        component = labels.get("component")
        if (
            item.get("state") == "SUCCEEDED"
            and labels.get("spark_profile") == "optimized"
            and component
        ):
            selected[str(component)] = item
    expected = {"source-bronze", "bronze-silver", "core-gold", "historical-features"}
    if set(selected) != expected:
        raise ValueError(f"optimized SUCCEEDED batch inventory mismatch: {set(selected)}")
    git_shas = {(_args(item).get("git-sha")) for item in selected.values()}
    if len(git_shas) != 1 or None in git_shas:
        raise ValueError(f"Dataproc Git SHA mismatch: {git_shas}")
    return selected


def _arrow_fields(schema: Any) -> list[dict[str, Any]]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]


def _source_dataset(table: str, manifest: dict[str, Any]) -> dict[str, Any]:
    import pyarrow.fs as fs
    import pyarrow.parquet as pq

    first = manifest["tables"][table]["files"][0]
    object_path = f"{BUCKET}/source/canonical-100gb-v1/{first['path']}"
    with fs.GcsFileSystem().open_input_file(object_path) as stream:
        schema = pq.ParquetFile(stream).schema_arrow
    uri = f"{SOURCE_ROOT}/{table}"
    metadata = manifest["tables"][table]
    return {
        "urn": dataset_urn(uri), "uri": uri, "zone": "source", "table": table,
        "format": "PARQUET", "physical_exists": True,
        "schema_fields": _arrow_fields(schema), "partition_columns": [],
        "file_count": len(metadata["files"]), "rows": int(metadata["rows"]),
        "bytes": int(metadata["bytes"]),
        "schema_sha256": list(metadata["schema_sha256"]),
        "evidence": {
            "manifest_file": first["path"], "manifest_file_sha256": first["sha256"],
            "manifest_status": manifest["status"],
        },
    }


def _delta_dataset(uri: str, zone: str, table: str) -> dict[str, Any]:
    from deltalake import DeltaTable

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            value = DeltaTable(uri)
            break
        except OSError as exc:
            last_error = exc
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    else:  # pragma: no cover - defensive, the loop either breaks or raises.
        raise RuntimeError(f"cannot open Delta table {uri}") from last_error
    metadata = value.metadata()
    return {
        "urn": dataset_urn(uri), "uri": uri, "zone": zone, "table": table,
        "format": "DELTA", "physical_exists": True,
        "schema_fields": _arrow_fields(value.schema().to_pyarrow()),
        "partition_columns": list(metadata.partition_columns),
        "delta_version": int(value.version()), "delta_table_id": metadata.id,
        "file_count": len(value.file_uris()),
        "evidence": {"delta_log_read": True, "delta_table_id": metadata.id},
    }


def _root(batch: dict[str, Any], key: str) -> str:
    value = _args(batch).get(key)
    if not value or not value.startswith(f"gs://{BUCKET}/"):
        raise ValueError(f"missing canonical --{key} in {batch.get('batch_id')}")
    return value.rstrip("/")


def _edge(downstream: dict[str, Any], upstreams: list[dict[str, Any]], proof: str) -> dict[str, Any]:
    return {
        "downstream": downstream["urn"],
        "upstreams": [item["urn"] for item in upstreams],
        "evidence": proof,
    }


def discover(manifest: dict[str, Any], batches: list[dict[str, Any]]) -> dict[str, Any]:
    selected = _select_batches(batches)
    roots = {
        "bronze": _root(selected["source-bronze"], "bronze-root"),
        "silver": _root(selected["bronze-silver"], "silver-root"),
        "gold": _root(selected["core-gold"], "gold-root"),
        "feature": _root(selected["historical-features"], "feature-gold-root"),
    }
    if manifest.get("status") != "COMPLETE" or set(manifest.get("tables", {})) != set(PHYSICAL_SOURCE):
        raise ValueError("canonical source manifest is incomplete or has an unexpected inventory")

    datasets = [_source_dataset(table, manifest) for table in PHYSICAL_SOURCE]
    delta_specs = (
        [(f"{roots['bronze']}/{table}", "bronze", table) for table in BRONZE_SILVER]
        + [(f"{roots['silver']}/{table}", "silver", table) for table in BRONZE_SILVER]
        + [(f"{roots['gold']}/{table}", "gold", table) for table in CORE_GOLD]
        + [(f"{roots['feature']}/{table}", "feature", table) for table in FEATURES]
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        datasets += list(executor.map(lambda spec: _delta_dataset(*spec), delta_specs))
    by_key = {(item["zone"], item["table"]): item for item in datasets}

    edges: list[dict[str, Any]] = []
    for table in BRONZE_SILVER:
        source_names = [table] if table != "transactions" else ["transactions_v1", "transactions_v2"]
        edges.append(_edge(
            by_key[("bronze", table)], [by_key[("source", name)] for name in source_names],
            "source_to_bronze.py:PHYSICAL_TABLES/BRONZE_TABLES and successful Dataproc --bronze-root",
        ))
        edges.append(_edge(
            by_key[("silver", table)], [by_key[("bronze", table)]],
            "silver_pipeline.py table-preserving Bronze→Silver transformation and successful Dataproc roots",
        ))

    dependencies = {
        "dim_user": [("silver", "users")],
        "dim_account": [("silver", "accounts"), ("silver", "users")],
        "dim_merchant": [("silver", "merchants")],
        "dim_device": [("silver", "devices"), ("silver", "users")],
        "dim_date": [("silver", "transactions")],
        "fact_transactions": [("silver", "transactions")],
        "fact_login_events": [("silver", "login_events")],
        "fact_balance_snapshot": [("silver", "balance_snapshots")],
        "obt_transaction_enriched": [("gold", name) for name in (
            "fact_transactions", "dim_user", "dim_account", "dim_device", "dim_merchant", "dim_date")],
        "feat_user_90d": [("gold", "fact_transactions"), ("gold", "dim_user")],
        "opt_merchant_performance": [("gold", "fact_transactions"), ("gold", "dim_merchant")],
    }
    for table, refs in dependencies.items():
        edges.append(_edge(by_key[("gold", table)], [by_key[ref] for ref in refs],
                           "gold_pipeline.py transformation dependency and successful Dataproc roots"))
    feature_dependencies = {
        "feat_user_behavior": [("silver", "transactions")],
        "feat_account_behavior": [("silver", "transactions")],
        "feat_device_behavior": [("silver", "transactions"), ("silver", "devices")],
        "feat_merchant_behavior": [("silver", "transactions")],
    }
    for table, refs in feature_dependencies.items():
        edges.append(_edge(by_key[("feature", table)], [by_key[ref] for ref in refs],
                           "historical_feature_pipeline.py dependency and successful Dataproc roots"))

    dag_jobs = (
        ("dp1_source_to_bronze", "submit_bronze_dataproc", "source-bronze", "source", PHYSICAL_SOURCE, "bronze", BRONZE_SILVER),
        ("dp2_bronze_to_curated", "submit_silver_dataproc", "bronze-silver", "bronze", BRONZE_SILVER, "silver", BRONZE_SILVER),
        ("dp2_bronze_to_curated", "submit_core_gold_dataproc", "core-gold", "silver", BRONZE_SILVER, "gold", CORE_GOLD),
        ("dp3_historical_features", "submit_historical_features_dataproc", "historical-features", "silver", BRONZE_SILVER, "feature", FEATURES),
    )
    jobs = []
    for dag, task, component, input_zone, inputs, output_zone, outputs in dag_jobs:
        batch = selected[component]
        input_items = [by_key[(input_zone, name)] for name in inputs]
        if component == "historical-features":
            input_items += [by_key[("gold", name)] for name in CORE_GOLD]
        jobs.append({
            "dag_id": dag, "task_id": task, "batch_id": batch["batch_id"],
            "batch_state": batch["state"], "batch_create_time": batch.get("create_time"),
            "batch_state_time": batch.get("state_time"),
            "git_sha": _args(batch)["git-sha"],
            "inputs": [item["urn"] for item in input_items],
            "outputs": [by_key[(output_zone, name)]["urn"] for name in outputs],
            "dataproc_args": batch.get("args", []),
        })

    all_urns = {item["urn"] for item in datasets}
    referenced = {item["downstream"] for item in edges} | {
        urn for item in edges for urn in item["upstreams"]
    }
    if not referenced <= all_urns:
        raise ValueError(f"lineage references undiscovered datasets: {referenced - all_urns}")
    return {
        "mode": "READ_ONLY_REAL_CLOUD_DISCOVERY",
        "source_manifest": {
            "dataset_version": manifest["dataset_version"], "status": manifest["status"],
            "total_bytes": manifest["total_bytes"], "shard_count": manifest["shard_count"],
            "plan_digest": manifest["plan_digest"],
        },
        "source_git_sha": jobs[0]["git_sha"], "roots": roots,
        "dataset_count": len(datasets), "lineage_count": len(edges),
        "datasets": datasets, "jobs": jobs, "lineage": edges,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataproc-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = discover(
        json.loads(args.manifest.read_text(encoding="utf-8")),
        json.loads(args.dataproc_evidence.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
