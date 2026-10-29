"""DP2: verified Bronze to immutable Silver and core Gold runs."""

from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.sdk import DAG, Param
from airflow.providers.standard.operators.bash import BashOperator

from data_platform.orchestration.airflow.python_runtime import (
    python_module_command,
    python_task_environment,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PYTHON_TASK_ENV = python_task_environment(REPOSITORY_ROOT)
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
        "dataset_version": Param("canonical-100gb-v1", type="string"),
        "benchmark_id": Param("ab-20260101000000", type="string"),
        "spark_profile": Param(
            "optimized", type="string", enum=["baseline", "optimized"]
        ),
        "git_sha": Param("REQUIRED_GIT_SHA", type="string"),
        "silver_pair_manifest": Param(
            "REQUIRED_SILVER_PAIR_MANIFEST.json", type="string"
        ),
        "gold_pair_manifest": Param(
            "REQUIRED_GOLD_PAIR_MANIFEST.json", type="string"
        ),
        "bronze_run_id": Param(
            "ab-20260101000000-optimized-bronze-r260101000000", type="string"
        ),
        "silver_run_id": Param(
            "ab-20260101000000-optimized-silver-r260101000001", type="string"
        ),
        "gold_run_id": Param(
            "ab-20260101000000-optimized-gold-r260101000002", type="string"
        ),
        "dataproc_ttl": Param("8h", type="string"),
        "project_id": Param("ewallet-data-ai-platform-hg26", type="string"),
        "region": Param("asia-southeast1", type="string"),
        "data_bucket": Param("ewallet-data-ai-platform-hg26-data", type="string"),
    },
    tags=["ewallet", "cloud", "dp2", "manual"],
) as dag:
    verify_bronze = BashOperator(
        task_id="verify_bronze_delta",
        bash_command=(
            python_module_command("ops.gcp.verify_delta_run") + " "
            "--root 'gs://{{ params.data_bucket }}/lakehouse/runs/"
            "{{ params.bronze_run_id }}/bronze' "
            + " ".join(f"--expected-table {table}" for table in SILVER_TABLES)
        ),
        retries=1,
        retry_delay=timedelta(minutes=1),
        cwd=str(REPOSITORY_ROOT),
        env=PYTHON_TASK_ENV,
        append_env=True,
    )
    wait_silver_quota = BashOperator(
        task_id="wait_silver_quota",
        bash_command=(
            python_module_command("ops.gcp.wait_for_dataproc_quota") + " "
            "--project '{{ params.project_id }}' --region '{{ params.region }}' "
            "--spark-profile '{{ params.spark_profile }}' "
            "--benchmark-id '{{ params.benchmark_id }}' "
            "--handoff 'bronze-to-silver' "
            "--predecessor-batch-id '{{ params.bronze_run_id }}' "
            "--evidence-output 'docs/evidence/quota_handoffs/"
            "{{ params.benchmark_id }}/{{ params.silver_run_id }}-quota.json' "
            "--execute"
        ),
        cwd=str(REPOSITORY_ROOT),
        env=PYTHON_TASK_ENV,
        append_env=True,
        execution_timeout=timedelta(minutes=20),
    )
    submit_silver = BashOperator(
        task_id="submit_silver_dataproc",
        bash_command=(
            python_module_command("ops.gcp.submit_bronze_to_silver") + " "
            "--dataset-version '{{ params.dataset_version }}' "
            "--benchmark-id '{{ params.benchmark_id }}' "
            "--source-bronze-run-id '{{ params.bronze_run_id }}' "
            "--run-id '{{ params.silver_run_id }}' "
            "--spark-profile '{{ params.spark_profile }}' "
            "--git-sha '{{ params.git_sha }}' "
            "--pair-manifest '{{ params.silver_pair_manifest }}' "
            "--ttl '{{ params.dataproc_ttl }}' --execute"
        ),
        execution_timeout=timedelta(hours=10),
        cwd=str(REPOSITORY_ROOT),
        env=PYTHON_TASK_ENV,
        append_env=True,
    )
    verify_silver = BashOperator(
        task_id="verify_silver_delta",
        bash_command=(
            python_module_command("ops.gcp.verify_delta_run") + " "
            "--root 'gs://{{ params.data_bucket }}/lakehouse/runs/"
            "{{ params.silver_run_id }}/silver' "
            + " ".join(f"--expected-table {table}" for table in SILVER_TABLES)
        ),
        retries=1,
        retry_delay=timedelta(minutes=1),
        cwd=str(REPOSITORY_ROOT),
        env=PYTHON_TASK_ENV,
        append_env=True,
    )
    wait_gold_quota = BashOperator(
        task_id="wait_gold_quota",
        bash_command=(
            python_module_command("ops.gcp.wait_for_dataproc_quota") + " "
            "--project '{{ params.project_id }}' --region '{{ params.region }}' "
            "--spark-profile '{{ params.spark_profile }}' "
            "--benchmark-id '{{ params.benchmark_id }}' "
            "--handoff 'silver-to-gold' "
            "--predecessor-batch-id '{{ params.silver_run_id }}' "
            "--evidence-output 'docs/evidence/quota_handoffs/"
            "{{ params.benchmark_id }}/{{ params.gold_run_id }}-quota.json' "
            "--execute"
        ),
        cwd=str(REPOSITORY_ROOT),
        env=PYTHON_TASK_ENV,
        append_env=True,
        execution_timeout=timedelta(minutes=20),
    )
    submit_core_gold = BashOperator(
        task_id="submit_core_gold_dataproc",
        bash_command=(
            python_module_command("ops.gcp.submit_silver_to_gold") + " "
            "--dataset-version '{{ params.dataset_version }}' "
            "--benchmark-id '{{ params.benchmark_id }}' "
            "--source-silver-run-id '{{ params.silver_run_id }}' "
            "--run-id '{{ params.gold_run_id }}' "
            "--spark-profile '{{ params.spark_profile }}' "
            "--git-sha '{{ params.git_sha }}' "
            "--pair-manifest '{{ params.gold_pair_manifest }}' "
            "--ttl '{{ params.dataproc_ttl }}' --execute"
        ),
        execution_timeout=timedelta(hours=10),
        cwd=str(REPOSITORY_ROOT),
        env=PYTHON_TASK_ENV,
        append_env=True,
    )
    verify_core_gold = BashOperator(
        task_id="verify_core_gold_delta",
        bash_command=(
            python_module_command("ops.gcp.verify_delta_run") + " "
            "--root 'gs://{{ params.data_bucket }}/lakehouse/runs/"
            "{{ params.gold_run_id }}/gold' "
            + " ".join(f"--expected-table {table}" for table in CORE_GOLD_TABLES)
        ),
        retries=1,
        retry_delay=timedelta(minutes=1),
        cwd=str(REPOSITORY_ROOT),
        env=PYTHON_TASK_ENV,
        append_env=True,
    )
    (
        verify_bronze
        >> wait_silver_quota
        >> submit_silver
        >> verify_silver
        >> wait_gold_quota
        >> submit_core_gold
        >> verify_core_gold
    )
