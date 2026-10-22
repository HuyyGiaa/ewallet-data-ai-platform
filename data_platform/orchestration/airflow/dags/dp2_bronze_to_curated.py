"""DP2: verified Bronze to immutable Silver and core Gold runs."""

from datetime import timedelta

import pendulum
from airflow.sdk import DAG, Param
from airflow.providers.standard.operators.bash import BashOperator


SILVER_TABLES = (
    "users", "accounts", "merchants", "devices", "transactions",
    "balance_snapshots", "login_events", "fraud_labels",
)
CORE_GOLD_TABLES = (
    "dim_user", "dim_account", "dim_merchant", "dim_device", "dim_date",
    "fact_transactions", "fact_login_events", "fact_balance_snapshot",
    "obt_transaction_enriched", "feat_user_90d", "opt_merchant_performance",
)


with DAG(
    dag_id="dp2_bronze_to_curated",
    description="Submit and verify Bronze-to-Silver-to-core-Gold Dataproc runs",
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    params={
        "dataset_version": Param("r4-pilot-1gb-v1", type="string"),
        "bronze_run_id": Param("r4-bronze-20261022-095608", type="string"),
        "silver_run_id": Param("r5-airflow-silver-manual", type="string"),
        "gold_run_id": Param("r5-airflow-gold-manual", type="string"),
        "data_bucket": Param("ewallet-data-ai-platform-hg26-data", type="string"),
    },
    tags=["ewallet", "cloud", "dp2", "manual"],
) as dag:
    verify_bronze = BashOperator(
        task_id="verify_bronze_delta",
        bash_command=(
            "python -m ops.gcp.verify_delta_run "
            "--root 'gs://{{ params.data_bucket }}/lakehouse/runs/"
            "{{ params.bronze_run_id }}/bronze' "
            + " ".join(f"--expected-table {table}" for table in SILVER_TABLES)
        ),
        retries=1,
        retry_delay=timedelta(minutes=1),
    )
    submit_silver = BashOperator(
        task_id="submit_silver_dataproc",
        bash_command=(
            "python -m ops.gcp.submit_bronze_to_silver "
            "--dataset-version '{{ params.dataset_version }}' "
            "--source-bronze-run-id '{{ params.bronze_run_id }}' "
            "--run-id '{{ params.silver_run_id }}' --execute"
        ),
        execution_timeout=timedelta(hours=2),
    )
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
    )
    submit_core_gold = BashOperator(
        task_id="submit_core_gold_dataproc",
        bash_command=(
            "python -m ops.gcp.submit_silver_to_gold "
            "--dataset-version '{{ params.dataset_version }}' "
            "--source-silver-run-id '{{ params.silver_run_id }}' "
            "--run-id '{{ params.gold_run_id }}' --execute"
        ),
        execution_timeout=timedelta(hours=2),
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
    )
    (
        verify_bronze
        >> submit_silver
        >> verify_silver
        >> submit_core_gold
        >> verify_core_gold
    )
