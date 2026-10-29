"""Deterministic DataHub operational-lineage plan for cloud DP1/DP2/DP3.

This module has no DataHub SDK import so the plan can be validated in CI.  A
publisher can translate the returned identifiers to DataFlow/DataJob and
DataJobInputOutput aspects only after the GMS endpoint passes readback checks.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


PLATFORM = "airflow"
ENV = "PROD"
CLUSTER = "ewallet-gcp"
BUCKET = "ewallet-data-ai-platform-hg26-data"
SOURCE_GIT_SHA = "bbae48caef7f65fda0c9375a7a49afb2eca5eef4"


def dataset_urn(gcs_uri: str) -> str:
    if not gcs_uri.startswith(f"gs://{BUCKET}/"):
        raise ValueError("dataset URI must be inside the canonical data bucket")
    return f"urn:li:dataset:(urn:li:dataPlatform:gcs,{gcs_uri},{ENV})"


def flow_urn(dag_id: str) -> str:
    return f"urn:li:dataFlow:({PLATFORM},{dag_id},{CLUSTER})"


def job_urn(dag_id: str, task_id: str) -> str:
    return f"urn:li:dataJob:({flow_urn(dag_id)},{task_id})"


@dataclass(frozen=True)
class JobPlan:
    dag_id: str
    task_id: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]

    @property
    def flow_urn(self) -> str:
        return flow_urn(self.dag_id)

    @property
    def job_urn(self) -> str:
        return job_urn(self.dag_id, self.task_id)


@dataclass(frozen=True)
class RunLineage:
    bronze_run_id: str
    silver_run_id: str
    gold_run_id: str
    feature_run_id: str


HISTORICAL_OPTIMIZED = RunLineage(
    bronze_run_id="ab-20261025134342-optimized-bronze-r261025174525",
    silver_run_id="ab-20261025134342-optimized-silver-r261025181923",
    gold_run_id="ab-20261025134342-optimized-gold-r261025181923",
    feature_run_id="ab-20261025134342-optimized-feat-r261026025955",
)


SOURCE_TABLES = ("users", "accounts", "merchants", "devices", "transactions", "balance_snapshots", "login_events", "fraud_labels")
BRONZE_TABLES = SOURCE_TABLES
SILVER_TABLES = SOURCE_TABLES
CORE_GOLD_TABLES = (
    "dim_user", "dim_account", "dim_merchant", "dim_device", "dim_date",
    "fact_transactions", "fact_login_events", "fact_balance_snapshot",
    "obt_transaction_enriched", "feat_user_90d", "opt_merchant_performance",
)
FEATURE_TABLES = ("feat_user_behavior", "feat_account_behavior", "feat_device_behavior", "feat_merchant_behavior")


def _run_table_urn(run_id: str, layer: str, table: str) -> str:
    return dataset_urn(f"gs://{BUCKET}/lakehouse/runs/{run_id}/{layer}/{table}")


def jobs_for_runs(runs: RunLineage) -> tuple[JobPlan, ...]:
    source = (dataset_urn(f"gs://{BUCKET}/source/canonical-100gb-v1/_control/dataset_manifest.json"),)
    bronze = tuple(_run_table_urn(runs.bronze_run_id, "bronze", table) for table in BRONZE_TABLES)
    silver = tuple(_run_table_urn(runs.silver_run_id, "silver", table) for table in SILVER_TABLES)
    gold = tuple(_run_table_urn(runs.gold_run_id, "gold", table) for table in CORE_GOLD_TABLES)
    features = tuple(_run_table_urn(runs.feature_run_id, "gold", table) for table in FEATURE_TABLES)
    return (
        JobPlan("dp1_source_to_bronze", "submit_bronze_dataproc", source, bronze),
        JobPlan("dp2_bronze_to_curated", "submit_silver_dataproc", bronze, silver),
        JobPlan("dp2_bronze_to_curated", "submit_core_gold_dataproc", silver, gold),
        JobPlan("dp3_historical_features", "submit_historical_features_dataproc", silver + gold, features),
    )


def build_plan(runs: RunLineage = HISTORICAL_OPTIMIZED) -> dict[str, object]:
    job_plans = jobs_for_runs(runs)
    jobs = []
    for job in job_plans:
        item = asdict(job)
        item.update(flow_urn=job.flow_urn, job_urn=job.job_urn)
        jobs.append(item)
    datasets = sorted({urn for job in job_plans for urn in job.inputs + job.outputs})
    return {
        "mode": "historical optimized runs; plan-only until GMS health, authentication and readback gates pass",
        "source_git_sha": SOURCE_GIT_SHA,
        "run_lineage": asdict(runs),
        "platform": PLATFORM,
        "environment": ENV,
        "cluster": CLUSTER,
        "flow_count": len({job.dag_id for job in job_plans}),
        "job_count": len(job_plans),
        "dataset_count": len(datasets),
        "datasets": datasets,
        "jobs": jobs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = build_plan()
    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
