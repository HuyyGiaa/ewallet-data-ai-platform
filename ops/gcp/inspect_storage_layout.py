"""Read Delta logs on GCS and build a metadata-first storage benchmark plan."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from data_platform.config.spark_profiles import parse_benchmark_run_id, validate_git_sha
from data_platform.storage.cloud_optimization import (
    ACCESS_PATTERNS,
    PINNED_CAPABILITIES,
    TableAccessPattern,
    decide_partitioning,
    decide_zorder,
    select_table,
    summarize_layout,
    validate_benchmark_paths,
)


SPARK_BENCHMARK_ID = re.compile(r"^ab-[0-9]{14}$")
STAGE_EVIDENCE = {
    "dp2-silver": "dp2-silver",
    "dp2-gold": "dp2-gold",
    "dp3-features": "dp3-features",
}
RUN_STAGE = {
    "dp2-silver": "silver",
    "dp2-gold": "gold",
    "dp3-features": "feat",
}
STAGE_LAYER = {
    "dp2-silver": "silver",
    "dp2-gold": "gold",
    "dp3-features": "gold",
}


def parse_table_spec(value: str) -> tuple[str, str, str]:
    try:
        qualified, uri = value.split("=", 1)
        stage, table = qualified.split(".", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "table must be STAGE.TABLE=gs://..."
        ) from exc
    if stage not in {"dp2-silver", "dp2-gold", "dp3-features"}:
        raise argparse.ArgumentTypeError("unsupported storage benchmark stage")
    if not table.replace("_", "").isalnum() or not uri.startswith("gs://"):
        raise argparse.ArgumentTypeError("unsafe table name or non-GCS URI")
    return stage, table, uri.rstrip("/")


def _column(actions: Any, name: str) -> list[Any]:
    return actions.column(name).to_pylist()


def _action_rows(actions: Any) -> list[dict[str, object]]:
    """Normalize delta-rs flattened AddFile metadata into plain dictionaries."""

    names = list(
        getattr(actions, "column_names", None) or actions.schema.names
    )
    values = {name: _column(actions, name) for name in names}
    size_name = "size_bytes" if "size_bytes" in names else "size"
    if size_name not in names or "path" not in names:
        raise ValueError("Delta AddFile metadata lacks path/size columns")
    count = len(values[size_name])
    rows: list[dict[str, object]] = []
    for index in range(count):
        partition_values = {
            name.split(".", 1)[1]: values[name][index]
            for name in names
            if name.startswith("partition.") and values[name][index] is not None
        }
        min_values = {
            name.split(".", 1)[1]: values[name][index]
            for name in names
            if name.startswith("min.") and values[name][index] is not None
        }
        max_values = {
            name.split(".", 1)[1]: values[name][index]
            for name in names
            if name.startswith("max.") and values[name][index] is not None
        }
        rows.append(
            {
                "path": values["path"][index],
                "size_bytes": values[size_name][index],
                "num_records": (
                    values["num_records"][index]
                    if "num_records" in names
                    else None
                ),
                "partition_values": partition_values,
                "min_values": min_values,
                "max_values": max_values,
            }
        )
    return rows


def inspect_delta_table(stage: str, table_name: str, uri: str) -> dict[str, object]:
    # Lazy import keeps plan/parser tests independent of cloud/Delta libraries.
    from deltalake import DeltaTable

    table = DeltaTable(uri)
    metadata = table.metadata()
    configuration = getattr(metadata, "configuration", None) or {}
    return summarize_layout(
        table=table_name,
        stage=stage,
        uri=uri,
        delta_version=table.version(),
        partition_columns=tuple(metadata.partition_columns),
        properties=configuration,
        actions=_action_rows(table.get_add_actions(flatten=True)),
    )


def _query_workload(table: str) -> list[dict[str, object]]:
    if table in {"transactions", "fact_transactions", "obt_transaction_enriched"}:
        return [
            {
                "name": "bounded_date_aggregation",
                "class": "date_range",
                "predicate_columns": ["event_date"],
                "require_non_empty": True,
                "description": "seven-day transaction count/sum/average",
            },
            {
                "name": "bounded_merchant_lookup",
                "class": "date_entity_lookup",
                "predicate_columns": ["event_date", "merchant_id"],
                "equality_predicate_columns": ["merchant_id"],
                "require_non_empty": True,
                "description": "seven-day lookup for one deterministic merchant",
            },
            {
                "name": "bounded_failed_payment_aggregation",
                "class": "date_status_aggregation",
                "predicate_columns": ["event_date", "status"],
                "equality_predicate_columns": ["status"],
                "require_non_empty": True,
                "description": "seven-day failed-payment analytics",
            },
        ]
    entity = {
        "feat_user_behavior": "user_id",
        "feat_account_behavior": "account_id",
        "feat_device_behavior": "device_id",
        "feat_merchant_behavior": "merchant_id",
    }.get(table)
    if entity:
        return [
            {
                "name": "bounded_entity_history",
                "class": "entity_time_range",
                "predicate_columns": [entity, "event_timestamp"],
                "equality_predicate_columns": [entity],
                "require_non_empty": True,
                "description": "point-in-time feature history for one entity",
            }
        ]
    return []


def _load_verified_spark_evidence(
    root: Path,
    *,
    dataset_version: str,
    git_sha: str,
) -> tuple[str, dict[str, str]]:
    """Bind storage inputs to one completed, equivalent optimized Spark A/B."""

    comparison = json.loads((root / "comparison.json").read_text(encoding="utf-8"))
    benchmark_id = str(comparison.get("benchmark_id", ""))
    if not SPARK_BENCHMARK_ID.fullmatch(benchmark_id):
        raise ValueError("Spark comparison has an invalid benchmark_id")
    if comparison.get("dataset_version") != dataset_version:
        raise ValueError("Spark comparison dataset_version does not match")
    if comparison.get("git_sha") != git_sha:
        raise ValueError("Spark comparison Git SHA does not match")
    rows = comparison.get("stages")
    expected = {"dp1-bronze", "dp2-silver", "dp2-gold", "dp3-features"}
    if (
        not isinstance(rows, list)
        or len(rows) != len(expected)
        or {row.get("stage") for row in rows} != expected
    ):
        raise ValueError("Spark comparison is incomplete")
    if any(row.get("output_equivalent") is not True for row in rows):
        raise ValueError("Spark comparison has a failed equivalence gate")

    run_by_stage: dict[str, str] = {}
    for stage in STAGE_EVIDENCE.values():
        evidence = json.loads(
            (root / "optimized" / f"{stage}.json").read_text(encoding="utf-8")
        )
        if any(
            (
                evidence.get("benchmark_id") != benchmark_id,
                evidence.get("dataset_version") != dataset_version,
                evidence.get("git_sha") != git_sha,
                evidence.get("spark_profile") != "optimized",
                evidence.get("stage") != stage,
                evidence.get("batch_state") != "SUCCEEDED",
            )
        ):
            raise ValueError(f"optimized Spark evidence is invalid for {stage}")
        outputs = evidence.get("output_run_ids")
        if not isinstance(outputs, list) or len(outputs) != 1:
            raise ValueError(f"optimized Spark evidence needs one output run for {stage}")
        run_id = str(outputs[0])
        parsed = parse_benchmark_run_id(run_id)
        if any(
            (
                parsed["benchmark_id"] != benchmark_id,
                parsed["spark_profile"] != "optimized",
                parsed["stage"] != RUN_STAGE[stage],
            )
        ):
            raise ValueError(f"optimized output run is misbound for {stage}")
        run_by_stage[stage] = run_id
    return benchmark_id, run_by_stage


def build_plan(
    *,
    benchmark_id: str,
    dataset_version: str,
    source_spark_git_sha: str,
    tool_git_sha: str,
    source_run_ids: Sequence[str],
    spark_benchmark_id: str,
    data_bucket: str,
    layouts: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    if not SPARK_BENCHMARK_ID.fullmatch(spark_benchmark_id):
        raise ValueError("invalid Spark benchmark ID")
    parsed_runs: dict[str, str] = {}
    for run_id in source_run_ids:
        parsed = parse_benchmark_run_id(run_id)
        if (
            parsed["benchmark_id"] != spark_benchmark_id
            or parsed["spark_profile"] != "optimized"
        ):
            raise ValueError("source runs do not share the verified Spark benchmark")
        if parsed["stage"] in parsed_runs:
            raise ValueError(f"duplicate optimized source stage: {parsed['stage']}")
        parsed_runs[parsed["stage"]] = run_id
    root = f"gs://{data_bucket}/lakehouse/benchmarks/storage/{benchmark_id}"
    table_plans: list[dict[str, object]] = []
    for layout in layouts:
        table = str(layout["table"])
        stage = str(layout["stage"])
        source_uri = str(layout["uri"])
        queries = _query_workload(table)
        run_id = parsed_runs.get(RUN_STAGE[stage])
        expected_uri = (
            f"gs://{data_bucket}/lakehouse/runs/{run_id}/"
            f"{STAGE_LAYER[stage]}/{table}"
        )
        if run_id is None or source_uri != expected_uri:
            raise ValueError(
                "table URI is not exactly bound to its approved source stage: "
                f"expected={expected_uri}, actual={source_uri}"
            )
        access = ACCESS_PATTERNS.get(table, TableAccessPattern("none"))
        selection = select_table(layout, query_importance=access.query_importance)
        partition = decide_partitioning(
            partition_columns=tuple(layout["partition_columns"]),
            partition_count=int(layout["partition_count"]),
            total_bytes=int(layout["total_bytes"]),
            files_per_partition_p95=int(layout["files_per_partition_p95"]),
            partition_size_bytes=layout.get("partition_size_bytes"),
            small_partition_count=int(layout.get("small_partition_count", 0)),
            access=access,
        )
        equality_columns = {
            str(column)
            for query in queries
            for column in query.get("equality_predicate_columns", [])
        }
        zorder = decide_zorder(
            access=access,
            partition_columns=tuple(layout["partition_columns"]),
            statistics_columns=tuple(layout["statistics_columns"]),
            statistics_profiles=layout.get("statistics_profiles") or {},
            equality_predicate_columns=tuple(sorted(equality_columns)),
        )
        baseline_uri = f"{root}/baseline/{stage}/{table}"
        compacted_uri = (
            f"{root}/compacted/{stage}/{table}"
            if zorder["action"] == "ZORDER" and selection["selected"]
            else None
        )
        optimized_uri = f"{root}/optimized/{stage}/{table}"
        validate_benchmark_paths(
            source_uri=source_uri,
            benchmark_root=root,
            baseline_uri=baseline_uri,
            compacted_uri=compacted_uri,
            optimized_uri=optimized_uri,
        )
        table_plans.append(
            {
                **layout,
                **selection,
                "query_importance": access.query_importance,
                "partition_decision": partition,
                "optimization": zorder if selection["selected"] else {
                    "action": "NONE",
                    "columns": [],
                    "reason": "table was not evidence-selected",
                },
                "queries": queries,
                "baseline_uri": baseline_uri,
                "compacted_uri": compacted_uri,
                "optimized_uri": optimized_uri,
                "estimated_rewrite_bytes": (
                    int(layout["total_bytes"]) if selection["selected"] else 0
                ),
            }
        )
    return {
        "schema_version": 1,
        "benchmark_id": benchmark_id,
        "dataset_version": dataset_version,
        "git_sha": tool_git_sha,
        "source_spark_git_sha": source_spark_git_sha,
        "source_run_ids": list(source_run_ids),
        "spark_benchmark_id": spark_benchmark_id,
        "benchmark_root": root,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "capabilities": PINNED_CAPABILITIES,
        "tables": table_plans,
        "safety": {
            "dry_run_default": True,
            "canonical_mutation_allowed": False,
            "spark_ab_mutation_allowed": False,
            "vacuum_allowed": False,
        },
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-spark-git-sha", required=True)
    parser.add_argument("--tool-git-sha", required=True)
    parser.add_argument("--source-run-id", action="append", required=True)
    parser.add_argument("--spark-evidence-root", type=Path, required=True)
    parser.add_argument("--data-bucket", required=True)
    parser.add_argument("--table", type=parse_table_spec, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    validate_git_sha(args.source_spark_git_sha)
    validate_git_sha(args.tool_git_sha)
    spark_benchmark_id, approved_runs = _load_verified_spark_evidence(
        args.spark_evidence_root,
        dataset_version=args.dataset_version,
        git_sha=args.source_spark_git_sha,
    )
    supplied_runs = set(args.source_run_id)
    if supplied_runs != set(approved_runs.values()):
        raise ValueError("source run IDs differ from completed optimized Spark evidence")
    layouts = [inspect_delta_table(*spec) for spec in args.table]
    for layout in layouts:
        stage = str(layout["stage"])
        run_id = approved_runs[STAGE_EVIDENCE[stage]]
        expected_uri = (
            f"gs://{args.data_bucket}/lakehouse/runs/{run_id}/"
            f"{STAGE_LAYER[stage]}/{layout['table']}"
        )
        if str(layout["uri"]) != expected_uri:
            raise ValueError(f"{stage} table URI is bound to the wrong optimized run")
    plan = build_plan(
        benchmark_id=args.benchmark_id,
        dataset_version=args.dataset_version,
        source_spark_git_sha=args.source_spark_git_sha,
        tool_git_sha=args.tool_git_sha,
        source_run_ids=args.source_run_id,
        spark_benchmark_id=spark_benchmark_id,
        data_bucket=args.data_bucket,
        layouts=layouts,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "layout_before.json").write_text(
        json.dumps({"tables": layouts}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(plan, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
