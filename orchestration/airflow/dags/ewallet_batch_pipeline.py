from datetime import timedelta

import pendulum
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator


default_args = {
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
}


with DAG(
    dag_id="ewallet_batch_pipeline",
    description="E-wallet Bronze -> Silver -> Gold batch pipeline",
    start_date=pendulum.datetime(2026, 8, 1, tz="Asia/Ho_Chi_Minh"),
    schedule="@daily",
    catchup=False,
    default_args=default_args,
    tags=["ewallet", "batch"],
    max_active_runs=1,
) as dag:

    bronze_ingestion = BashOperator(
        task_id="bronze_ingestion",
        bash_command="""
            set -euo pipefail
            cd "{{ var.value.project_root }}"
            "{{ var.value.fintech_python }}" storage/scripts/init_storage.py
        """,
    )

    validate_bronze = BashOperator(
        task_id="validate_bronze",
        bash_command="""
            set -euo pipefail
            cd "{{ var.value.project_root }}"
            "{{ var.value.fintech_python }}" -m validation.validate_bronze
        """,
    )
    
    silver_transformation = BashOperator(
        task_id="silver_transformation",
        bash_command="""
            set -euo pipefail
            cd "{{ var.value.project_root }}"
            PYSPARK_SUBMIT_ARGS="--driver-memory 8g pyspark-shell" \
            "{{ var.value.fintech_python }}" -m transformation.spark.silver.silver_pipeline
        """,
    )

    validate_silver = BashOperator(
        task_id="validate_silver",
        bash_command="""
            set -euo pipefail
            cd "{{ var.value.project_root }}"
            "{{ var.value.fintech_python }}" -m validation.validate_silver
        """,
    )

    gold_transformation = BashOperator(
        task_id="gold_transformation",
        bash_command="""
            set -euo pipefail
            cd "{{ var.value.project_root }}"
            PYSPARK_SUBMIT_ARGS="--driver-memory 8g pyspark-shell" \
            "{{ var.value.fintech_python }}" -m transformation.spark.gold.gold_pipeline
        """,
    )

    validate_gold = BashOperator(
        task_id="validate_gold",
        bash_command="""
            set -euo pipefail
            cd "{{ var.value.project_root }}"
            "{{ var.value.fintech_python }}" -m validation.validate_gold
        """,
    )

    (
        bronze_ingestion
        >> validate_bronze
        >> silver_transformation
        >> validate_silver
        >> gold_transformation
        >> validate_gold
    )