"""DP3: verified Silver/core Gold to immutable historical feature Gold."""

from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.sdk import DAG, Param
from airflow.providers.standard.operators.bash import BashOperator


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SILVER_TABLES = (
    "users", "accounts", "merchants", "devices", "transactions",
    "balance_snapshots", "login_events", "fraud_labels",
)
CORE_GOLD_TABLES = (
    "dim_user", "dim_account", "dim_merchant", "dim_device", "dim_date",
    "fact_transactions", "fact_login_events", "fact_balance_snapshot",
    "obt_transaction_enriched", "feat_user_90d", "opt_merchant_performance",
)
FEATURE_TABLES = (
    "feat_user_behavior", "feat_account_behavior", "feat_device_behavior",
    "feat_merchant_behavior",
)


with DAG(
    dag_id="dp3_historical_features",
    description="Submit and verify immutable historical feature Gold run",
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    params={
        "dataset_version": Param("canonical-100gb-v1", type="string"),
        "benchmark_id": Param("ab-20260101000000", type="string"),
        "spark_profile": Param(
            "optimized", type="string", enum=["baseline", "optimized"]
        ),
        "git_sha": Param("REQUIRED_GIT_SHA", type="string"),
        "silver_run_id": Param(
            "ab-20260101000000-optimized-silver-r260101000001", type="string"
        ),
        "gold_run_id": Param(
            "ab-20260101000000-optimized-gold-r260101000002", type="string"
        ),
        "feature_run_id": Param(
            "ab-20260101000000-optimized-feat-r260101000003", type="string"
        ),
        "dataproc_ttl": Param("8h", type="string"),
        "project_id": Param("ewallet-data-ai-platform-hg26", type="string"),
        "region": Param("asia-southeast1", type="string"),
        "data_bucket": Param("ewallet-data-ai-platform-hg26-data", type="string"),
    },
    tags=["ewallet", "cloud", "dp3", "manual"],
) as dag:
    verify_silver = BashOperator(
        task_id="verify_silver_delta",
        bash_command=(
            "python -m ops.gcp.verify_delta_run "
            "--root 'gs://{{ params.data_bucket }}/lakehouse/runs/"
            "{{ params.silver_run_id }}/silver' "
            + " ".join(f"--expected-table {table}" for table in SILVER_TABLES)
        ),
        retries=1,
        retry_delay=timedelta(minutes=1),
        cwd=str(REPOSITORY_ROOT),
    )
    verify_core_gold = BashOperator(
        task_id="verify_core_gold_delta",
        bash_command=(
            "python -m ops.gcp.verify_delta_run "
            "--root 'gs://{{ params.data_bucket }}/lakehouse/runs/"
            "{{ params.gold_run_id }}/gold' "
            + " ".join(f"--expected-table {table}" for table in CORE_GOLD_TABLES)
        ),
        retries=1,
        retry_delay=timedelta(minutes=1),
        cwd=str(REPOSITORY_ROOT),
    )
    wait_features_quota = BashOperator(
        task_id="wait_features_quota",
        bash_command=(
            "python -m ops.gcp.wait_for_dataproc_quota "
            "--project '{{ params.project_id }}' --region '{{ params.region }}' "
            "--spark-profile '{{ params.spark_profile }}' "
            "--benchmark-id '{{ params.benchmark_id }}' "
            "--handoff 'gold-to-features' "
            "--predecessor-batch-id '{{ params.gold_run_id }}' "
            "--evidence-output 'docs/evidence/quota_handoffs/"
            "{{ params.benchmark_id }}/{{ params.feature_run_id }}-quota.json' "
            "--execute"
        ),
        cwd=str(REPOSITORY_ROOT),
        execution_timeout=timedelta(minutes=20),
    )
    submit_features = BashOperator(
        task_id="submit_historical_features_dataproc",
        bash_command=(
            "python -m ops.gcp.submit_historical_features "
            "--dataset-version '{{ params.dataset_version }}' "
            "--benchmark-id '{{ params.benchmark_id }}' "
            "--source-silver-run-id '{{ params.silver_run_id }}' "
            "--source-core-gold-run-id '{{ params.gold_run_id }}' "
            "--run-id '{{ params.feature_run_id }}' "
            "--spark-profile '{{ params.spark_profile }}' "
            "--git-sha '{{ params.git_sha }}' "
            "--ttl '{{ params.dataproc_ttl }}' --execute"
        ),
        execution_timeout=timedelta(hours=10),
        cwd=str(REPOSITORY_ROOT),
    )
    verify_features = BashOperator(
        task_id="verify_historical_feature_delta",
        bash_command=(
            "python -m ops.gcp.verify_delta_run "
            "--root 'gs://{{ params.data_bucket }}/lakehouse/runs/"
            "{{ params.feature_run_id }}/gold' "
            + " ".join(f"--expected-table {table}" for table in FEATURE_TABLES)
        ),
        retries=1,
        retry_delay=timedelta(minutes=1),
        cwd=str(REPOSITORY_ROOT),
    )
    [verify_silver, verify_core_gold] >> wait_features_quota >> submit_features >> verify_features
