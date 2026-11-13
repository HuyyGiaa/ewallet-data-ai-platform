"""DataHub lineage plan for one verified DP1/DP2/DP3 batch campaign profile.

The plan holds three things, each from a different source of truth:

* **Datasets** - every expected table of every zone (``schema_inventory``) at the
  exact GCS location of this campaign's run roots (``campaign_evidence``). When an
  observed-schema snapshot of the *same* campaign profile is supplied, each dataset
  also carries its physical columns.
* **DataFlows and DataJobs** - one DataFlow per Airflow DAG and, per stage, the
  ``<stage>.submit_transformation`` and ``<stage>.semantic_validation`` tasks that
  actually ran, with their DagRun, Dataproc batch and Spark application ids.
* **Dataset-to-dataset edges** - ``TABLE_DEPENDENCIES``: the inputs each builder
  function reads, as written in the Spark pipelines. Nothing is inferred from
  column names, and no ``every input -> every output`` shortcut is used. A
  transformation job's inputs are the union of its outputs' upstreams; a
  validation job's inputs are the datasets its rules check.

The plan is pure data. ``main`` is dry-run by default; ``--publish`` sends the
records through the existing ``verified_cloud_publisher.DataHubAdapter`` and reads
every aspect back. Nothing is published on import.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from data_platform.contracts.schema_inventory import ZoneInventory, logical_source_table, zone_inventory
from data_platform.metadata.datahub.assertions.campaign_validation import outcomes_by_stage
from data_platform.metadata.datahub.assertions.validation_artifact import EXECUTED_STATUSES
from data_platform.metadata.datahub.campaign_evidence import CampaignEvidence, StageEvidence, load_campaign
from data_platform.metadata.datahub.lineage import verified_cloud_publisher
from data_platform.metadata.datahub.urns import dataset_urn
from data_platform.storage.scripts.config import BRONZE_SCHEMA, GOLD_SCHEMA, SILVER_SCHEMA


PLAN_MODE = verified_cloud_publisher.CAMPAIGN_MODE
TRINO_SCHEMA = {"source": "source_zone", "bronze": BRONZE_SCHEMA, "silver": SILVER_SCHEMA,
                "gold": GOLD_SCHEMA, "features": GOLD_SCHEMA}
# zone.table -> upstream zone.table, read from the builder calls. Bronze and Silver
# are table-preserving (source_to_bronze.py, silver_pipeline.py: one cleaner per
# table), so only the Gold and feature builders are listed explicitly.
TABLE_DEPENDENCIES: Mapping[str, tuple[str, ...]] = {
    # data_platform/processing/spark/gold/gold_pipeline.py: run_gold_pipeline
    "gold.dim_user": ("silver.users",),
    "gold.dim_account": ("silver.accounts",),
    "gold.dim_merchant": ("silver.merchants",),
    "gold.dim_device": ("silver.devices",),
    "gold.dim_date": ("silver.transactions", "silver.login_events", "silver.balance_snapshots"),
    "gold.dim_merchant_scd2": ("silver.merchant_history",),
    "gold.fact_transactions": ("silver.transactions",),
    "gold.fact_login_events": ("silver.login_events",),
    "gold.fact_balance_snapshot": ("silver.balance_snapshots",),
    "gold.obt_transaction_enriched": ("gold.fact_transactions", "gold.dim_user", "gold.dim_account",
                                      "gold.dim_device", "gold.dim_merchant", "gold.dim_date"),
    "gold.feat_user_90d": ("gold.fact_transactions", "gold.dim_user"),
    "gold.opt_merchant_performance": ("gold.fact_transactions", "gold.dim_merchant"),
    # data_platform/processing/spark/gold/historical_feature_pipeline.py
    "features.feat_user_behavior": ("silver.transactions",),
    "features.feat_account_behavior": ("silver.transactions",),
    "features.feat_device_behavior": ("silver.transactions", "silver.devices"),
    "features.feat_merchant_behavior": ("silver.transactions",),
}


def table_dependencies(inventory: ZoneInventory) -> dict[str, tuple[str, ...]]:
    """Return ``zone.table -> upstreams`` for every non-Source table of the inventory.

    Raises:
        ValueError: A Gold or feature table has no declared dependency, or a
            dependency names a table outside the inventory.
    """

    edges: dict[str, tuple[str, ...]] = {}
    sources: dict[str, list[str]] = {}
    for physical in inventory.source_physical:
        sources.setdefault(logical_source_table(physical), []).append(f"source.{physical}")
    for table in inventory.bronze:
        edges[f"bronze.{table}"] = tuple(sources[table])
    for table in inventory.silver:
        edges[f"silver.{table}"] = (f"bronze.{table}",)
    for zone in ("gold", "features"):
        for table in inventory.tables(zone):
            key = f"{zone}.{table}"
            if key not in TABLE_DEPENDENCIES:
                raise ValueError(f"{key} has no declared builder dependency")
            edges[key] = TABLE_DEPENDENCIES[key]
    known = {f"{zone}.{table}" for zone in ("source", "bronze", "silver", "gold", "features")
             for table in inventory.tables(zone)}
    for downstream, upstreams in edges.items():
        missing = set(upstreams) - known
        if missing:
            raise ValueError(f"{downstream} depends on tables outside the inventory: {sorted(missing)}")
    return edges


def _locations(campaign: CampaignEvidence, inventory: ZoneInventory) -> dict[str, str]:
    stage_by_zone = {stage.zone: stage for stage in campaign.stages.values()}
    locations = {f"source.{name}": campaign.source_uri(name) for name in inventory.source_physical}
    for zone in ("bronze", "silver", "gold", "features"):
        for table in inventory.tables(zone):
            locations[f"{zone}.{table}"] = stage_by_zone[zone].table_uri(table)
    return locations


def _producer(key: str, campaign: CampaignEvidence) -> StageEvidence | None:
    zone = key.split(".", 1)[0]
    return next((stage for stage in campaign.stages.values() if stage.zone == zone), None)


def _schema(observed: Mapping[str, Any] | None, key: str) -> dict[str, Any] | None:
    if observed is None:
        return None
    zone, table = key.split(".", 1)
    if zone == "source":
        entry = (observed.get("source") or {}).get("tables", {}).get(table)
        partitions: list[str] = []
    else:
        entry = observed["zones"][zone]["tables"].get(table)
        partitions = list((entry or {}).get("partition_columns") or [])
    if entry is None:
        raise ValueError(f"observed schema has no {key}")
    return {"fields": list(entry["columns"]), "partition_columns": partitions,
            "version": int(entry.get("delta_version", 0))}


def require_observed_identity(observed: Mapping[str, Any], campaign: CampaignEvidence) -> None:
    """Refuse an observed-schema snapshot that was not exported from this campaign profile.

    Raises:
        ValueError: The snapshot names another dataset, benchmark, profile or source.
    """

    provenance = observed.get("provenance") or {}
    expected = {
        "dataset_version": campaign.dataset_version,
        "benchmark_id": campaign.benchmark_id,
        "spark_profile": campaign.spark_profile,
        "source_manifest_sha256": campaign.source_manifest_sha256,
        "campaign_manifest_sha256": campaign.manifest_sha256,
    }
    for key, value in expected.items():
        if provenance.get(key) != value:
            raise ValueError(f"observed schemas {key} is {provenance.get(key)!r}, not {value!r}")


def _job(stage: StageEvidence, task: str, inputs: Sequence[str], outputs: Sequence[str],
         campaign: CampaignEvidence, properties: Mapping[str, str]) -> dict[str, Any]:
    return {
        "dag_id": stage.dag_id,
        "task_id": f"{stage.stage}.{task}",
        "inputs": sorted(set(inputs)),
        "outputs": sorted(set(outputs)),
        "flow_custom_properties": {
            "orchestrator": "airflow",
            "pipeline": stage.pipeline,
            "dag_run_id": stage.dag_run_id,
            **campaign.identity(),
        },
        "job_custom_properties": {
            "dag_run_id": stage.dag_run_id,
            "stage": stage.stage,
            "run_id": stage.run_id,
            **{key: str(value) for key, value in properties.items()},
            **campaign.identity(),
        },
    }


def build_plan(
    campaign: CampaignEvidence,
    *,
    validated_datasets: Mapping[str, Sequence[str]] | None = None,
    observed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the verified-metadata document for one campaign profile.

    Args:
        campaign: Verified campaign profile.
        validated_datasets: Optional ``stage -> dataset URNs`` its validation rules
            check; adds one ``<stage>.semantic_validation`` job per stage.
        observed: Optional observed-schema snapshot of the same campaign profile.

    Raises:
        ValueError: The inventory and dependencies disagree, or ``observed`` belongs
            to another campaign.
    """

    inventory = zone_inventory(campaign.dataset_version)
    if observed is not None:
        require_observed_identity(observed, campaign)
    edges = table_dependencies(inventory)
    location = _locations(campaign, inventory)
    urn = {key: dataset_urn(uri) for key, uri in location.items()}

    datasets = []
    for key in sorted(location):
        zone, table = key.split(".", 1)
        producer = _producer(key, campaign)
        properties = {
            "zone": zone,
            "table": table,
            "gcs_uri": location[key],
            **campaign.identity(),
        }
        if producer is None:
            properties["role"] = "immutable source dataset"
            properties["logical_table"] = logical_source_table(table)
            description = f"Source dataset {campaign.dataset_version}/{table} (Parquet), input of DP1."
        else:
            properties.update(producer_dag=producer.dag_id, producer_task=f"{producer.stage}.submit_transformation",
                              dag_run_id=producer.dag_run_id, run_id=producer.run_id)
            description = (f"{TRINO_SCHEMA[zone]}.{table} (Delta) written by {producer.dag_id} "
                           f"task {producer.stage}.submit_transformation in DagRun {producer.dag_run_id}.")
        datasets.append({
            "urn": urn[key],
            "name": f"{TRINO_SCHEMA[zone]}.{table}",
            "qualified_name": location[key],
            "description": description,
            "custom_properties": properties,
            "schema": _schema(observed, key),
        })

    jobs = []
    for stage in campaign.stages.values():
        outputs = [key for key in edges if key.split(".", 1)[0] == stage.zone]
        inputs = {upstream for key in outputs for upstream in edges[key]} - set(outputs)
        jobs.append(_job(stage, "submit_transformation", [urn[key] for key in inputs],
                         [urn[key] for key in outputs], campaign, {
                             "dataproc_batch_id": stage.transformation_batch_id,
                             "spark_application_id": stage.spark_application_id,
                             "job_start": stage.job_start,
                             "job_end": stage.job_end,
                         }))
        if validated_datasets is not None and stage.validation_executed:
            checked = list(validated_datasets.get(stage.stage, ()))
            unknown = set(checked) - set(urn.values())
            if not checked or unknown:
                raise ValueError(f"{stage.stage} validation datasets are empty or unknown: {sorted(unknown)}")
            jobs.append(_job(stage, "semantic_validation", checked, [], campaign, {
                "dataproc_batch_id": str(stage.validation_batch_id),
                "spark_application_id": str(stage.validation_spark_application_id),
                "validation_status": str(stage.validation_status),
                "validation_report_uri": str(stage.validation_report_uri),
            }))

    lineage = [{"downstream": urn[key], "upstreams": sorted(urn[item] for item in upstreams)}
               for key, upstreams in sorted(edges.items())]
    return {
        "mode": PLAN_MODE,
        "campaign": campaign.identity(),
        "dataset_count": len(datasets),
        "job_count": len(jobs),
        "flow_count": len({job["dag_id"] for job in jobs}),
        "lineage_count": len(lineage),
        "schema_source": "observed" if observed is not None else "none (OWNER_METADATA_EXPORT_PENDING)",
        "datasets": datasets,
        "jobs": jobs,
        "lineage": lineage,
    }


def validated_datasets_by_stage(campaign: CampaignEvidence, reports_directory: Path) -> dict[str, list[str]]:
    """Return ``stage -> dataset URNs`` that the stage's executed rules check."""

    grouped = outcomes_by_stage(campaign, reports_directory)
    return {stage: sorted({item.dataset_urn for item in outcomes if item.status in EXECUTED_STATUSES})
            for stage, outcomes in grouped.items()}


def main(argv: Sequence[str] | None = None) -> int:
    """Write the dry-run records, or publish them with ``--publish``."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--spark-profile", required=True, choices=("baseline", "optimized"))
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--source-manifest-sha256")
    parser.add_argument("--observed-schemas", type=Path,
                        help="observed_schemas.json exported from the same campaign profile")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true", help="publish to DataHub (owner only)")
    parser.add_argument("--gms-url")
    args = parser.parse_args(argv)
    campaign = load_campaign(args.manifest_dir, benchmark_id=args.benchmark_id,
                             spark_profile=args.spark_profile, dataset_version=args.dataset_version,
                             source_manifest_sha256=args.source_manifest_sha256)
    observed = (json.loads(args.observed_schemas.read_text(encoding="utf-8"))
                if args.observed_schemas else None)
    plan = build_plan(campaign, validated_datasets=validated_datasets_by_stage(campaign, args.reports_dir),
                      observed=observed)
    records = verified_cloud_publisher.records(plan)
    if args.publish:
        if not args.gms_url:
            parser.error("--gms-url is required with --publish")
        adapter = verified_cloud_publisher.DataHubAdapter(args.gms_url.rstrip("/"), os.getenv("DATAHUB_GMS_TOKEN"))
        payload = {"mode": "PUBLISH_WITH_READBACK", "campaign": plan["campaign"],
                   **verified_cloud_publisher.publish_with_readback(plan, adapter)}
    else:
        payload = {"mode": "DRY_RUN", "campaign": plan["campaign"],
                   "counts": {key: plan[key] for key in ("dataset_count", "flow_count", "job_count", "lineage_count")},
                   "schema_source": plan["schema_source"], "record_count": len(records),
                   "records": [{"urn": urn, "aspect": aspect, "payload": value} for urn, aspect, value in records]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"mode": payload["mode"], "output": str(args.output),
                      "records": payload.get("record_count", payload.get("published"))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
