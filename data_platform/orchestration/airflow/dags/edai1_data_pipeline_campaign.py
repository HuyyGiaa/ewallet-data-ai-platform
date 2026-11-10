"""EDAI-1 DP1-DP3 campaign: one profile, one DagRun, every stage gated.

One manually triggered run carries one Spark profile from the immutable Source
dataset through Bronze, Silver, Core Gold and DP3 features. Trigger Baseline first
and review it; trigger Optimized as a separate run. Both read the same Source but
never each other's intermediates. There is no winner selection and no promotion.

The default parameters are non-executing: every task refuses to run without the
exact approval token bound to this benchmark, profile, dataset and Git revision.

Parameters reach the task runner through the environment, never through the shell
text; all logic lives in ``ops/gcp/campaign_task.py``.
"""

from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG, Param, TaskGroup

from data_platform.config.dataset_identity import REHEARSAL_DATA_BUCKET
from data_platform.orchestration.airflow.python_runtime import (
    python_module_command,
    python_task_environment,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PARAMETER_NAMES = (
    "dataset_version", "benchmark_id", "spark_profile", "git_sha", "source_manifest_sha256",
    "bronze_run_id", "silver_run_id", "gold_run_id", "feature_run_id",
    "project_id", "region", "data_bucket", "dataproc_ttl", "terraform_output_json",
    "execution_approval",
)
TASK_ENV = {
    **python_task_environment(REPOSITORY_ROOT),
    **{f"EDAI1_CAMPAIGN_{name.upper()}": "{{ params." + name + " }}" for name in PARAMETER_NAMES},
    "EDAI1_CAMPAIGN_ATTEMPT": "{{ ti.try_number }}",
}
_RUN = r"^ab-[0-9]{14}-(baseline|optimized)-%s-r[0-9]{12}$"
TRANSFORM_TIMEOUT = timedelta(hours=10)
VALIDATE_TIMEOUT = timedelta(hours=8)
READ_TIMEOUT = timedelta(minutes=30)


def campaign_task(task_id: str, action: str, *, timeout: timedelta, retries: int = 0) -> BashOperator:
    """Transformations and paid validators have retries=0; read-only checks may retry."""
    return BashOperator(
        task_id=task_id,
        bash_command=python_module_command("ops.gcp.campaign_task") + f" {action}",
        cwd=str(REPOSITORY_ROOT),
        env=TASK_ENV,
        append_env=True,
        retries=retries,
        retry_delay=timedelta(minutes=1),
        execution_timeout=timeout,
    )


def stage_group(group_id: str, stage: str) -> TaskGroup:
    """Quota -> transformation -> physical Delta check -> semantic validation."""
    with TaskGroup(group_id=group_id) as group:
        wait_quota = campaign_task("wait_quota", f"quota-{stage}", timeout=timedelta(minutes=20))
        submit = campaign_task("submit_transformation", f"submit-{stage}", timeout=TRANSFORM_TIMEOUT)
        physical = campaign_task("physical_validation", f"physical-{stage}", timeout=READ_TIMEOUT, retries=2)
        semantic = campaign_task("semantic_validation", f"validate-{stage}", timeout=VALIDATE_TIMEOUT)
        wait_quota >> submit >> physical >> semantic
    return group


with DAG(
    dag_id="edai1_data_pipeline_campaign",
    description="One Spark profile from Source through DP3, gated by physical and semantic validation",
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    default_args={"retries": 0},
    params={
        "dataset_version": Param("rehearsal-5gb-v1", type="string", pattern=r"^[a-z0-9][a-z0-9.-]{1,62}$"),
        "benchmark_id": Param("ab-20260101000000", type="string", pattern=r"^ab-[0-9]{14}$"),
        "spark_profile": Param("baseline", type="string", enum=["baseline", "optimized"]),
        "git_sha": Param("0" * 40, type="string", pattern=r"^[0-9a-f]{40}$"),
        "source_manifest_sha256": Param("0" * 64, type="string", pattern=r"^[0-9a-f]{64}$"),
        "bronze_run_id": Param("ab-20260101000000-baseline-bronze-r260101000000", type="string", pattern=_RUN % "bronze"),
        "silver_run_id": Param("ab-20260101000000-baseline-silver-r260101000001", type="string", pattern=_RUN % "silver"),
        "gold_run_id": Param("ab-20260101000000-baseline-gold-r260101000002", type="string", pattern=_RUN % "gold"),
        "feature_run_id": Param("ab-20260101000000-baseline-feat-r260101000003", type="string", pattern=_RUN % "feat"),
        "project_id": Param("ewallet-data-ai-platform-hg26", type="string", pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$"),
        "region": Param("asia-southeast1", type="string", pattern=r"^[a-z]+-[a-z]+[0-9]+$"),
        # Must equal the dataset's registered bucket; every task checks it.
        "data_bucket": Param(REHEARSAL_DATA_BUCKET, type="string", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,61}[A-Za-z0-9]$"),
        "dataproc_ttl": Param("8h", type="string", enum=["8h"]),
        "terraform_output_json": Param("", type="string", pattern=r"^(|/[A-Za-z0-9_./-]+\.json)$"),
        "execution_approval": Param("NOT_APPROVED", type="string", pattern=r"^[A-Za-z0-9:_.-]+$"),
    },
    tags=["ewallet", "cloud", "edai1", "campaign", "manual"],
) as dag:
    with TaskGroup(group_id="preflight") as preflight:
        identity = campaign_task("verify_identity_and_absence", "preflight", timeout=READ_TIMEOUT)
        source = campaign_task("verify_source_dataset", "verify-source", timeout=READ_TIMEOUT, retries=2)
        identity >> source

    dp1_bronze = stage_group("dp1_bronze", "bronze")
    dp2_silver = stage_group("dp2_silver", "silver")
    dp2_gold = stage_group("dp2_gold", "gold")
    dp3_features = stage_group("dp3_features", "features")

    with TaskGroup(group_id="finalize") as finalize:
        campaign_task("write_campaign_manifest", "finalize", timeout=READ_TIMEOUT)

    preflight >> dp1_bronze >> dp2_silver >> dp2_gold >> dp3_features >> finalize
