"""Inspect 5GB campaign Delta tables and write the immutable storage benchmark plan.

Read-only. The plan is bound to one verified campaign profile:

1. The committed campaign evidence must pass ``ops.edai1_verifier`` (checksum chain,
   identity, 260 rules, semantic comparison) for the workload specification.
2. Every table URI is derived from that profile's verified ``campaign_manifest.json``;
   no URI is typed by hand.
3. Each table's layout is summarized from Delta ``AddFile`` metadata (``deltalake``),
   and its current Delta version is pinned in the plan.
4. Query predicates come from the data: the last seven daily partitions, and inside
   them a typical merchant (median per-merchant row count) and the ``failed`` status
   with their exact row counts. Only the ``merchant_id`` and ``status`` columns of
   those seven partition files are read.

Outputs ``layout_before.json`` and ``plan.json``. Nothing is written to GCS.
Run in the ``ai_platform/features/feast`` uv project, which locks ``deltalake``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from data_platform.config.dataset_identity import resolve_storage_context
from data_platform.config.spark_profiles import validate_git_sha
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
from ops.edai1_verifier.report import fingerprint, verify
from ops.edai1_verifier.spec import load_spec

PLAN_SCHEMA = 2
PROFILE = "optimized"
WINDOW_DAYS = 7
# Benchmark stage -> (campaign stage, Delta zone folder)
STAGES: Mapping[str, tuple[str, str]] = {
    "dp2-silver": ("silver", "silver"),
    "dp2-gold": ("gold", "gold"),
    "dp3-features": ("features", "gold"),
}
SUPPORTED_TABLES = {"dp2-gold": ("fact_transactions", "obt_transaction_enriched")}


@dataclass(frozen=True)
class CampaignBinding:
    """The verified campaign profile the benchmark reads from."""

    benchmark_id: str
    dataset_version: str
    source_spark_git_sha: str
    campaign_manifest_sha256: str
    verification_report_sha256: str
    run_ids: Mapping[str, str]
    output_roots: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "benchmark_id": self.benchmark_id,
            "spark_profile": PROFILE,
            "dataset_version": self.dataset_version,
            "source_spark_git_sha": self.source_spark_git_sha,
            "campaign_manifest_sha256": self.campaign_manifest_sha256,
            "verification_report_sha256": self.verification_report_sha256,
            "run_ids": dict(self.run_ids),
        }


def parse_table_spec(value: str) -> tuple[str, str]:
    """``STAGE.TABLE``, for example ``dp2-gold.fact_transactions``."""

    try:
        stage, table = value.split(".", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("table must be STAGE.TABLE") from exc
    if table not in SUPPORTED_TABLES.get(stage, ()):
        raise argparse.ArgumentTypeError(f"unsupported benchmark table {value!r}; supported: {SUPPORTED_TABLES}")
    return stage, table


def load_verified_campaign(evidence_dir: Path, spec_path: Path, *, dataset_version: str,
                           source_spark_git_sha: str) -> CampaignBinding:
    """Verify the committed campaign evidence and return the optimized profile's runs.

    Raises:
        ValueError: The evidence does not pass verification or does not match the
            requested dataset version and source Spark Git SHA.
    """

    spec = load_spec(spec_path)
    if spec.dataset_version != dataset_version or spec.source_git_sha != source_spark_git_sha:
        raise ValueError("workload spec does not name the requested dataset version and source Spark Git SHA")
    report = verify(evidence_dir, spec)
    if report["verdict"] != "PASS":
        raise ValueError(f"campaign evidence does not verify: {report['failed_checks'][:5]}")
    manifest_path = evidence_dir / "manifests" / PROFILE / "campaign_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stages = manifest["stages"]
    return CampaignBinding(
        benchmark_id=spec.benchmark_id,
        dataset_version=spec.dataset_version,
        source_spark_git_sha=spec.source_git_sha,
        campaign_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        verification_report_sha256=fingerprint(report),
        run_ids={stage: str(stages[campaign]["run_id"]) for stage, (campaign, _) in STAGES.items()},
        output_roots={stage: str(stages[campaign]["output_root"]).rstrip("/") for stage, (campaign, _) in STAGES.items()},
    )


def table_uri(binding: CampaignBinding, stage: str, table: str) -> str:
    """Source URI of one table inside the verified run root."""

    return f"{binding.output_roots[stage]}/{table}"


def _action_rows(actions: Any) -> list[dict[str, object]]:
    """Normalize delta-rs flattened AddFile metadata into plain dictionaries."""

    names = list(getattr(actions, "column_names", None) or actions.schema.names)
    values = {name: actions.column(name).to_pylist() for name in names}
    size_name = "size_bytes" if "size_bytes" in names else "size"
    if size_name not in names or "path" not in names:
        raise ValueError("Delta AddFile metadata lacks path/size columns")
    rows: list[dict[str, object]] = []
    for index in range(len(values[size_name])):
        def prefixed(prefix: str) -> dict[str, object]:
            return {name.split(".", 1)[1]: values[name][index] for name in names
                    if name.startswith(prefix) and values[name][index] is not None}
        rows.append({
            "path": values["path"][index],
            "size_bytes": values[size_name][index],
            "num_records": values["num_records"][index] if "num_records" in names else None,
            "partition_values": prefixed("partition."),
            "min_values": prefixed("min."),
            "max_values": prefixed("max."),
        })
    return rows


def inspect_delta_table(stage: str, table_name: str, uri: str) -> dict[str, object]:
    """Summarize one table's active files from its Delta log (no data read)."""

    from deltalake import DeltaTable  # lazy: plan tests need no Delta library

    table = DeltaTable(uri)
    metadata = table.metadata()
    return summarize_layout(
        table=table_name,
        stage=stage,
        uri=uri,
        delta_version=table.version(),
        partition_columns=tuple(metadata.partition_columns),
        properties=getattr(metadata, "configuration", None) or {},
        actions=_action_rows(table.get_add_actions(flatten=True)),
    )


def query_window(layout: Mapping[str, Any]) -> tuple[str, str]:
    """The last ``WINDOW_DAYS`` daily partitions, from partition metadata only."""

    bounds = (layout.get("global_statistics") or {}).get("event_date") or {}
    if bounds.get("maximum") is None:
        raise ValueError(f"{layout.get('table')} has no event_date partition bounds")
    end = date.fromisoformat(str(bounds["maximum"])[:10])
    return str(end - timedelta(days=WINDOW_DAYS - 1)), str(end)


def choose_parameters(window: tuple[str, str], merchant_counts: Mapping[str, int],
                      status_counts: Mapping[str, int], window_rows: int) -> dict[str, object]:
    """Pick the workload predicates from observed counts (deterministic).

    The merchant is the one whose row count in the window is the median of all
    per-merchant counts (smallest id among ties): a typical merchant, neither the
    busiest nor an outlier.

    Raises:
        ValueError: The window has no merchant rows or no ``failed`` rows.
    """

    if not merchant_counts:
        raise ValueError("the query window contains no merchant transactions")
    if int(status_counts.get("failed", 0)) <= 0:
        raise ValueError("the query window contains no failed transactions")
    target = median(sorted(merchant_counts.values()))
    merchant = min(merchant_counts, key=lambda key: (abs(merchant_counts[key] - target), key))
    return {
        "window_start": window[0],
        "window_end": window[1],
        "window_rows": int(window_rows),
        "merchant_id": merchant,
        "merchant_rows_in_window": int(merchant_counts[merchant]),
        "merchants_in_window": len(merchant_counts),
        "merchant_selection": "median per-merchant row count in the window, smallest id on ties",
        "status_counts_in_window": dict(sorted((str(k), int(v)) for k, v in status_counts.items())),
        "failed_rows_in_window": int(status_counts["failed"]),
    }


def derive_parameters(uri: str, layout: Mapping[str, Any]) -> dict[str, object]:
    """Read ``merchant_id`` and ``status`` of the window partitions and choose predicates."""

    import pyarrow.compute as pc
    from deltalake import DeltaTable

    window = query_window(layout)
    days = [str(date.fromisoformat(window[0]) + timedelta(days=offset)) for offset in range(WINDOW_DAYS)]
    dataset = DeltaTable(uri, version=int(layout["delta_version"])).to_pyarrow_dataset(
        partitions=[("event_date", "in", days)])
    columns = dataset.to_table(columns=["merchant_id", "status"])
    merchants = pc.value_counts(pc.drop_null(columns["merchant_id"])).to_pylist()
    statuses = pc.value_counts(columns["status"]).to_pylist()
    return {
        **choose_parameters(window, {item["values"]: item["counts"] for item in merchants},
                            {item["values"]: item["counts"] for item in statuses}, columns.num_rows),
        "source_delta_version": int(layout["delta_version"]),
        "derived_from": "merchant_id,status columns of the window partitions (read-only)",
    }


def query_workload(table: str) -> list[dict[str, object]]:
    """Representative queries; ``partition_pruning`` pairs run on the baseline arm only."""

    return [
        {"name": "window_aggregation", "class": "date_range", "predicate_columns": ["event_date"],
         "require_non_empty": True, "description": "seven-day count, sum and average"},
        {"name": "window_merchant_lookup", "class": "date_entity_lookup",
         "predicate_columns": ["event_date", "merchant_id"], "equality_predicate_columns": ["merchant_id"],
         "require_non_empty": True, "description": "seven-day aggregate for one typical merchant"},
        {"name": "window_failed_by_channel", "class": "date_status_aggregation",
         "predicate_columns": ["event_date", "status"], "equality_predicate_columns": ["status"],
         "require_non_empty": True, "description": "seven-day failed transactions per channel"},
    ] if table in {"fact_transactions", "obt_transaction_enriched"} else []


def build_plan(*, benchmark_id: str, binding: CampaignBinding, tool_git_sha: str, data_bucket: str,
               layouts: Sequence[Mapping[str, Any]], parameters: Mapping[str, Mapping[str, Any]]) -> dict[str, object]:
    """Assemble the immutable plan; every output path is validated against the source."""

    resolve_storage_context(binding.dataset_version, data_bucket)
    root = f"gs://{data_bucket}/lakehouse/benchmarks/storage/{benchmark_id}"
    tables: list[dict[str, object]] = []
    for layout in layouts:
        table, stage, source_uri = str(layout["table"]), str(layout["stage"]), str(layout["uri"])
        if source_uri != table_uri(binding, stage, table):
            raise ValueError(f"{stage}.{table} is not the verified campaign table: {source_uri}")
        access = ACCESS_PATTERNS.get(table, TableAccessPattern("none"))
        queries = query_workload(table)
        equality = sorted({str(column) for query in queries for column in query.get("equality_predicate_columns", [])})
        prediction = decide_zorder(access=access, partition_columns=tuple(layout["partition_columns"]),
                                   statistics_columns=tuple(layout["statistics_columns"]),
                                   statistics_profiles=layout.get("statistics_profiles") or {},
                                   equality_predicate_columns=equality)
        zorder_columns = [column for column in access.zorder_candidates if column in equality][:1]
        if not zorder_columns:
            raise ValueError(f"{table} has no queried Z-order candidate")
        baseline_uri, compacted_uri, optimized_uri = (f"{root}/{arm}/{stage}/{table}"
                                                      for arm in ("baseline", "compacted", "optimized"))
        validate_benchmark_paths(source_uri=source_uri, benchmark_root=root, baseline_uri=baseline_uri,
                                 compacted_uri=compacted_uri, optimized_uri=optimized_uri)
        tables.append({
            **layout,
            "selection": select_table(layout, query_importance=access.query_importance),
            "selected": True,
            "query_importance": access.query_importance,
            "partition_decision": decide_partitioning(
                partition_columns=tuple(layout["partition_columns"]), partition_count=int(layout["partition_count"]),
                total_bytes=int(layout["total_bytes"]), files_per_partition_p95=int(layout["files_per_partition_p95"]),
                partition_size_bytes=layout.get("partition_size_bytes"),
                small_partition_count=int(layout.get("small_partition_count", 0)), access=access),
            "optimization": {
                "action": "ZORDER",
                "columns": zorder_columns,
                "reason": "every arm is exercised: compaction-only, then Z-order from the same snapshot",
                "metadata_prediction": prediction,
            },
            "workload_parameters": dict(parameters[f"{stage}.{table}"]),
            "queries": queries,
            "partition_pruning": {
                "query": "window_aggregation on the baseline arm",
                "sides": {"pruned": "event_date BETWEEN window (partition filter)",
                          "stats_only": "timestamp half-open range (Delta file statistics, no partition filter)",
                          "full_scan": "date_format(timestamp) BETWEEN window (no pruning, no statistics skipping)"},
            },
            "baseline_uri": baseline_uri,
            "compacted_uri": compacted_uri,
            "optimized_uri": optimized_uri,
            "estimated_rewrite_bytes": {"compacted_arm_maximum": int(layout["total_bytes"]),
                                        "optimized_arm": int(layout["total_bytes"])},
        })
    return {
        "schema_version": PLAN_SCHEMA,
        "benchmark_id": benchmark_id,
        "dataset_version": binding.dataset_version,
        "git_sha": tool_git_sha,
        "source_spark_git_sha": binding.source_spark_git_sha,
        "source_campaign": binding.to_dict(),
        "source_run_ids": sorted(binding.run_ids[str(table["stage"])] for table in tables),
        "spark_benchmark_id": binding.benchmark_id,
        "benchmark_root": root,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "capabilities": PINNED_CAPABILITIES,
        "tables": tables,
        "safety": {"dry_run_default": True, "canonical_mutation_allowed": False, "vacuum_allowed": False,
                   "clone": "SHALLOW CLONE ... VERSION AS OF <pinned>; rewrites write only below benchmark_root"},
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--benchmark-id", required=True, help="new storage benchmark id, e.g. storage-5gb-20261112a")
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-spark-git-sha", required=True)
    parser.add_argument("--tool-git-sha", required=True)
    parser.add_argument("--data-bucket", required=True)
    parser.add_argument("--campaign-evidence-dir", type=Path, required=True)
    parser.add_argument("--workload-spec", type=Path, required=True)
    parser.add_argument("--table", type=parse_table_spec, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    validate_git_sha(args.tool_git_sha)
    validate_git_sha(args.source_spark_git_sha)
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise ValueError(f"{args.output_dir} is not empty; plans are immutable")
    binding = load_verified_campaign(args.campaign_evidence_dir, args.workload_spec,
                                     dataset_version=args.dataset_version,
                                     source_spark_git_sha=args.source_spark_git_sha)
    layouts, parameters = [], {}
    for stage, table in args.table:
        uri = table_uri(binding, stage, table)
        layout = inspect_delta_table(stage, table, uri)
        layouts.append(layout)
        parameters[f"{stage}.{table}"] = derive_parameters(uri, layout)
    plan = build_plan(benchmark_id=args.benchmark_id, binding=binding, tool_git_sha=args.tool_git_sha,
                      data_bucket=args.data_bucket, layouts=layouts, parameters=parameters)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "layout_before.json").write_text(
        json.dumps({"tables": layouts}, indent=2, default=str) + "\n", encoding="utf-8")
    (args.output_dir / "plan.json").write_text(json.dumps(plan, indent=2, default=str) + "\n", encoding="utf-8")
    for table in plan["tables"]:
        print(json.dumps({"table": table["table"], "delta_version": table["delta_version"],
                          "files": table["file_count"], "bytes": table["total_bytes"],
                          "parameters": table["workload_parameters"]}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
