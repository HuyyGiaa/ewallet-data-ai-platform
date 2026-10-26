"""DP1: immutable source manifest to bounded Bronze Delta run."""

from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.sdk import DAG, Param
from airflow.providers.standard.operators.bash import BashOperator


BRONZE_TABLES = (
    "users", "accounts", "merchants", "devices", "transactions",
    "balance_snapshots", "login_events", "fraud_labels",
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


with DAG(
    dag_id="dp1_source_to_bronze",
    description="Submit and verify immutable source-to-Bronze Dataproc run",
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
        "pair_manifest": Param("REQUIRED_PAIR_MANIFEST.json", type="string"),
        "bronze_run_id": Param(
            "ab-20260101000000-optimized-bronze-r260101000000", type="string"
        ),
        "dataproc_ttl": Param("8h", type="string"),
        "project_id": Param("ewallet-data-ai-platform-hg26", type="string"),
        "region": Param("asia-southeast1", type="string"),
        "predecessor_batch_id": Param("", type="string"),
        "data_bucket": Param("ewallet-data-ai-platform-hg26-data", type="string"),
    },
    tags=["ewallet", "cloud", "dp1", "manual"],
) as dag:
    verify_source_manifest = BashOperator(
        task_id="verify_source_manifest",
        bash_command=(
            "gcloud storage ls "
            "gs://{{ params.data_bucket }}/source/{{ params.dataset_version }}/"
            "_control/dataset_manifest.json"
        ),
        retries=1,
        retry_delay=timedelta(minutes=1),
        execution_timeout=timedelta(minutes=5),
    )
    wait_bronze_quota = BashOperator(
        task_id="wait_bronze_quota",
        bash_command=(
            "python -m ops.gcp.wait_for_dataproc_quota "
            "--project '{{ params.project_id }}' "
            "--region '{{ params.region }}' "
            "--spark-profile '{{ params.spark_profile }}' "
            "--benchmark-id '{{ params.benchmark_id }}' "
            "--handoff 'external-to-bronze' "
            "--predecessor-batch-id '{{ params.predecessor_batch_id }}' "
            "--evidence-output 'docs/evidence/quota_handoffs/"
            "{{ params.benchmark_id }}/{{ params.bronze_run_id }}-quota.json' "
            "--execute"
        ),
        cwd=str(REPOSITORY_ROOT),
        execution_timeout=timedelta(minutes=20),
    )
    submit_bronze = BashOperator(
        task_id="submit_bronze_dataproc",
        bash_command=(
            "python -m ops.gcp.submit_source_to_bronze "
            "--dataset-version '{{ params.dataset_version }}' "
            "--benchmark-id '{{ params.benchmark_id }}' "
            "--run-id '{{ params.bronze_run_id }}' "
            "--spark-profile '{{ params.spark_profile }}' "
            "--git-sha '{{ params.git_sha }}' "
            "--pair-manifest '{{ params.pair_manifest }}' "
            "--ttl '{{ params.dataproc_ttl }}' --execute"
        ),
        cwd=str(REPOSITORY_ROOT),
        execution_timeout=timedelta(hours=10),
    )
    verify_bronze = BashOperator(
        task_id="verify_bronze_delta",
        bash_command=(
            "python -m ops.gcp.verify_delta_run "
            "--root 'gs://{{ params.data_bucket }}/lakehouse/runs/"
            "{{ params.bronze_run_id }}/bronze' "
            + " ".join(f"--expected-table {table}" for table in BRONZE_TABLES)
        ),
        cwd=str(REPOSITORY_ROOT),
        retries=1,
        retry_delay=timedelta(minutes=1),
        execution_timeout=timedelta(minutes=15),
    )
    verify_source_manifest >> wait_bronze_quota >> submit_bronze >> verify_bronze
