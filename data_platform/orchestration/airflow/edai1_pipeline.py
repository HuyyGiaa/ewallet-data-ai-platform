"""Build the three independent EDAI-1 pipeline DAGs from one definition.

``dp1_source_to_bronze``, ``dp2_bronze_to_curated`` and ``dp3_historical_features``
each call :func:`build_pipeline_dag`. Every task runs ``ops.gcp.campaign_task`` with
one action; parameters reach it only through the environment, never through the
shell text. Paid transformations and validators never retry automatically; read-only
checks may retry.

A DagRun is one pipeline and one Spark profile. A pipeline consumes the immutable
handoff manifest of its predecessor and publishes its own only after every stage it
owns passed the physical and the semantic validation.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG, Param, TaskGroup

from data_platform.config.dataset_identity import REHEARSAL_5GB, REHEARSAL_DATA_BUCKET
from data_platform.orchestration.airflow.python_runtime import (
    python_module_command,
    python_task_environment,
)
from ops.gcp.campaign_task import NOT_APPROVED, PIPELINES, REQUIRED_TTL


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TRANSFORM_TIMEOUT = timedelta(hours=10)
VALIDATE_TIMEOUT = timedelta(hours=8)
READ_TIMEOUT = timedelta(minutes=30)
QUOTA_TIMEOUT = timedelta(minutes=20)
_RUN = r"^ab-[0-9]{14}-(baseline|optimized)-%s-r[0-9]{12}$"
_RUN_ALIAS = {"bronze_run_id": "bronze", "silver_run_id": "silver", "gold_run_id": "gold", "feature_run_id": "feat"}
_PLACEHOLDER_STAMP = {"bronze_run_id": 0, "silver_run_id": 1, "gold_run_id": 2, "feature_run_id": 3}
STAGE_GROUPS = {"bronze": "bronze", "silver": "silver", "gold": "gold", "features": "features"}
PREFLIGHT_TASKS = {
    # pipeline -> (task_id, action, retries) in order
    "dp1": (("verify_identity_and_absence", "preflight", 0),
            ("verify_source_dataset", "verify-source", 2)),
    "dp2": (("verify_identity_and_absence", "preflight", 0),
            ("verify_dp1_handoff", "verify-upstream-handoff", 2),
            ("verify_bronze_inputs", "verify-upstream-inputs", 2)),
    "dp3": (("verify_identity_and_absence", "preflight", 0),
            ("verify_dp2_handoff", "verify-upstream-handoff", 2),
            ("verify_silver_gold_inputs", "verify-upstream-inputs", 2)),
}
PUBLISH_TASK = {"dp1": "publish_dp1_handoff", "dp2": "publish_dp2_handoff", "dp3": "publish_dp3_completion"}
DESCRIPTIONS = {
    "dp1": "EDAI-1 DP1: immutable Source -> Bronze, validated, publishes the DP1 handoff",
    "dp2": "EDAI-1 DP2: DP1 Bronze -> Silver -> Core Gold, validated, publishes the DP2 handoff",
    "dp3": "EDAI-1 DP3: DP2 Silver + Core Gold -> historical features, validated, publishes the completion",
}


def task_environment(pipeline: str) -> dict[str, str]:
    """Environment of every task: the pipeline, all parameters and the DagRun identity."""

    spec = PIPELINES[pipeline]
    return {
        **python_task_environment(REPOSITORY_ROOT),
        "EDAI1_CAMPAIGN_PIPELINE": pipeline,
        **{f"EDAI1_CAMPAIGN_{name.upper()}": "{{ params." + name + " }}" for name in spec.parameter_names},
        "EDAI1_CAMPAIGN_ATTEMPT": "{{ ti.try_number }}",
        "EDAI1_CAMPAIGN_DAG_ID": "{{ dag.dag_id }}",
        "EDAI1_CAMPAIGN_DAG_RUN_ID": "{{ run_id }}",
    }


def default_params(pipeline: str) -> dict[str, Param]:
    """Non-executing defaults: every task refuses them until the exact approval is given."""

    params = {
        "dataset_version": Param(REHEARSAL_5GB, type="string", pattern=r"^[a-z0-9][a-z0-9.-]{1,62}$"),
        "benchmark_id": Param("ab-20260101000000", type="string", pattern=r"^ab-[0-9]{14}$"),
        "spark_profile": Param("baseline", type="string", enum=["baseline", "optimized"]),
        "git_sha": Param("0" * 40, type="string", pattern=r"^[0-9a-f]{40}$"),
        "source_manifest_sha256": Param("0" * 64, type="string", pattern=r"^[0-9a-f]{64}$"),
    }
    for name in PIPELINES[pipeline].run_parameters:
        alias = _RUN_ALIAS[name]
        params[name] = Param(f"ab-20260101000000-baseline-{alias}-r26010100000{_PLACEHOLDER_STAMP[name]}",
                             type="string", pattern=_RUN % alias)
    params.update({
        "project_id": Param("ewallet-data-ai-platform-hg26", type="string", pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$"),
        "region": Param("asia-southeast1", type="string", pattern=r"^[a-z]+-[a-z]+[0-9]+$"),
        # Must equal the dataset's registered bucket; every task checks it.
        "data_bucket": Param(REHEARSAL_DATA_BUCKET, type="string", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,61}[A-Za-z0-9]$"),
        "dataproc_ttl": Param(REQUIRED_TTL, type="string", enum=[REQUIRED_TTL]),
        "terraform_output_json": Param("", type="string", pattern=r"^(|/[A-Za-z0-9_./-]+\.json)$"),
        "execution_approval": Param(NOT_APPROVED, type="string", pattern=r"^[A-Za-z0-9:_.-]+$"),
    })
    return params


def campaign_task(pipeline: str, task_id: str, action: str, *, timeout: timedelta, retries: int = 0) -> BashOperator:
    """Transformations and paid validators have retries=0; read-only checks may retry."""

    return BashOperator(
        task_id=task_id,
        bash_command=python_module_command("ops.gcp.campaign_task") + f" {action}",
        cwd=str(REPOSITORY_ROOT),
        env=task_environment(pipeline),
        append_env=True,
        retries=retries,
        retry_delay=timedelta(minutes=1),
        execution_timeout=timeout,
    )


def stage_group(pipeline: str, stage: str) -> TaskGroup:
    """Quota -> transformation -> physical Delta check -> semantic validation."""

    with TaskGroup(group_id=STAGE_GROUPS[stage]) as group:
        wait_quota = campaign_task(pipeline, "wait_quota", f"quota-{stage}", timeout=QUOTA_TIMEOUT)
        submit = campaign_task(pipeline, "submit_transformation", f"submit-{stage}", timeout=TRANSFORM_TIMEOUT)
        physical = campaign_task(pipeline, "physical_validation", f"physical-{stage}", timeout=READ_TIMEOUT, retries=2)
        semantic = campaign_task(pipeline, "semantic_validation", f"validate-{stage}", timeout=VALIDATE_TIMEOUT)
        wait_quota >> submit >> physical >> semantic
    return group


def build_pipeline_dag(pipeline: str) -> DAG:
    """preflight >> one TaskGroup per owned stage >> finalize, strictly linear."""

    spec = PIPELINES[pipeline]
    with DAG(
        dag_id=spec.dag_id,
        description=DESCRIPTIONS[pipeline],
        doc_md=__doc__,
        start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
        schedule=None,
        catchup=False,
        max_active_runs=1,
        max_active_tasks=1,
        default_args={"retries": 0},
        params=default_params(pipeline),
        tags=["ewallet", "cloud", "edai1", pipeline, "manual"],
    ) as dag:
        with TaskGroup(group_id="preflight") as preflight:
            checks = [campaign_task(pipeline, task_id, action, timeout=READ_TIMEOUT, retries=retries)
                      for task_id, action, retries in PREFLIGHT_TASKS[pipeline]]
            for earlier, later in zip(checks, checks[1:]):
                earlier >> later
        groups = [stage_group(pipeline, stage) for stage in spec.stages]
        with TaskGroup(group_id="finalize") as finalize:
            # Write-once and idempotent: a retry can only find identical content.
            campaign_task(pipeline, PUBLISH_TASK[pipeline], "publish-handoff", timeout=READ_TIMEOUT, retries=2)
        chain = [preflight, *groups, finalize]
        for earlier, later in zip(chain, chain[1:]):
            earlier >> later
    return dag
