"""Incremental Offline-to-Online Feast materialization (EDAI-2 row 11).

The DAG orders and gates the existing operator; it holds no materialization
logic. Each task calls ``ops.mlops.feast_incremental`` for one stage. Airflow runs
as a local control plane, and the Feast work runs as a Kubernetes Job on GKE. The
default mode renders manifests and changes nothing.
"""

from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.sdk import DAG, Param
from airflow.providers.standard.operators.bash import BashOperator

from data_platform.orchestration.airflow.feast_params import (
    FEAST_PARAM_SCHEMAS,
    feast_task_environment,
)
from data_platform.orchestration.airflow.python_runtime import (
    python_module_command,
    python_task_environment,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
TASK_ENV = {**python_task_environment(REPOSITORY_ROOT), **feast_task_environment()}
FEAST_PARAMS = {name: Param(**schema) for name, schema in FEAST_PARAM_SCHEMAS.items()}

# task_id, stage, retries, timeout
TASKS = (
    ("validate_config", "config", 0, timedelta(minutes=2)),
    ("validate_source_identity", "source", 1, timedelta(minutes=5)),
    ("determine_window", "window", 1, timedelta(minutes=20)),
    ("materialize_window", "materialize", 1, timedelta(minutes=70)),
    ("verify_job_completion", "completion", 1, timedelta(minutes=20)),
    ("verify_online_readback", "readback", 1, timedelta(minutes=70)),
    ("record_run_provenance", "provenance", 0, timedelta(minutes=2)),
    ("publish_datahub_lineage", "datahub", 1, timedelta(minutes=10)),
)


with DAG(
    dag_id="feast_incremental_materialization",
    description="Validate a snapshot, load one Feast window and verify Redis readback",
    start_date=pendulum.datetime(2026, 11, 1, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    params=FEAST_PARAMS,
    tags=["ewallet", "feast", "mlops", "manual"],
) as dag:
    previous = None
    for task_id, stage, retries, timeout in TASKS:
        task = BashOperator(
            task_id=task_id,
            bash_command=python_module_command("ops.mlops.feast_incremental") + f" --stage {stage}",
            retries=retries,
            retry_delay=timedelta(minutes=1),
            execution_timeout=timeout,
            cwd=str(REPOSITORY_ROOT),
            env=TASK_ENV,
            append_env=True,
        )
        if previous is not None:
            previous >> task
        previous = task
