"""DP2 Method 2: one manually triggered, fail-closed Silver/Gold A/B DAG.

All jobs run sequentially to respect the small Dataproc quota. Gold pair creation
is downstream of the formal Silver equivalence + immutable Silver Winner gate.
"""
from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.sdk import DAG, Param
from airflow.providers.standard.operators.bash import BashOperator

from data_platform.orchestration.airflow.python_runtime import (
    python_module_command, python_task_environment,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PYTHON_TASK_ENV = python_task_environment(REPOSITORY_ROOT)

# Paths and run IDs are explicit DAG run inputs: no runtime IDs are fabricated.
with DAG(
    dag_id="dp2_bronze_to_curated",
    description="Method 2: Silver A/B → Silver Winner → Gold A/B → Gold Winner",
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    params={
        # Input restrictions are intentional: values are interpolated into Bash
        # commands by Airflow. Reject quoting and shell metacharacters at schema
        # validation rather than relying on quote delimiters alone.
        "benchmark_id": Param("ab-20260101000000", type="string", pattern=r"^ab-[0-9]{14}$"),
        "dataset_version": Param("canonical-100gb-v1", type="string", enum=["canonical-100gb-v1"]),
        "git_sha": Param("0" * 40, type="string", pattern=r"^[0-9a-f]{40}$"),
        "bronze_winner": Param("/ABSOLUTE/PATH/dp1-bronze-winner.json", type="string", pattern=r"^/[A-Za-z0-9_./-]+$"),
        "bronze_source_benchmark_id": Param("ab-20260101000000", type="string", pattern=r"^ab-[0-9]{14}$"),
        "bronze_winner_sha256": Param("0" * 64, type="string", pattern=r"^[0-9a-f]{64}$"),
        "artifact_root": Param("/ABSOLUTE/PATH/DP2-METHOD2-EVIDENCE", type="string", pattern=r"^/[A-Za-z0-9_./-]+$"),
        "baseline_silver_run_id": Param("ab-20260101000000-baseline-silver-r260101000001", type="string", pattern=r"^ab-[0-9]{14}-baseline-silver-r[0-9]{12}$"),
        "optimized_silver_run_id": Param("ab-20260101000000-optimized-silver-r260101000001", type="string", pattern=r"^ab-[0-9]{14}-optimized-silver-r[0-9]{12}$"),
        "baseline_gold_run_id": Param("ab-20260101000000-baseline-gold-r260101000002", type="string", pattern=r"^ab-[0-9]{14}-baseline-gold-r[0-9]{12}$"),
        "optimized_gold_run_id": Param("ab-20260101000000-optimized-gold-r260101000002", type="string", pattern=r"^ab-[0-9]{14}-optimized-gold-r[0-9]{12}$"),
        "approval": Param("NOT_APPROVED", type="string", enum=["NOT_APPROVED", "I_APPROVE_DP2_PAID_JOBS"]),
        "ttl": Param("8h", type="string", enum=["8h"]),
        "project_id": Param("ewallet-data-ai-platform-hg26", type="string", pattern=r"^[a-z][a-z0-9-]+$"),
        "region": Param("asia-southeast1", type="string", pattern=r"^[a-z]+-[a-z]+[0-9]$"),
        "data_bucket": Param("ewallet-data-ai-platform-hg26-data", type="string", pattern=r"^[a-z0-9][a-z0-9._-]+[a-z0-9]$"),
    },
    tags=["ewallet", "cloud", "dp2", "method2", "manual"],
) as dag:
    # All paths use the Airflow worker's filesystem (not the webserver's host).
    # All values are validated again by the module before cloud work.
    common = (
        "--root '{{ params.artifact_root }}' "
        "--bronze-winner '{{ params.bronze_winner }}' "
        "--bronze-source-benchmark-id '{{ params.bronze_source_benchmark_id }}' "
        "--bronze-winner-sha256 '{{ params.bronze_winner_sha256 }}' "
        "--benchmark-id '{{ params.benchmark_id }}' "
        "--dataset-version '{{ params.dataset_version }}' "
        "--git-sha '{{ params.git_sha }}' "
        "--data-bucket '{{ params.data_bucket }}' "
        "--project '{{ params.project_id }}' "
        "--region '{{ params.region }}' "
        "--ttl '{{ params.ttl }}' "
        "--baseline-silver-run-id '{{ params.baseline_silver_run_id }}' "
        "--optimized-silver-run-id '{{ params.optimized_silver_run_id }}' "
        "--baseline-gold-run-id '{{ params.baseline_gold_run_id }}' "
        "--optimized-gold-run-id '{{ params.optimized_gold_run_id }}' "
    )

    def step(name: str, action: str, *, stage: str | None = None,
             profile: str | None = None, timeout_hours: int = 1) -> BashOperator:
        extra = (f"--stage {stage} " if stage is not None else "")
        extra += f"--profile {profile} " if profile is not None else ""
        if action == "approve":
            extra += "--approval '{{ params.approval }}' "
        return BashOperator(
            task_id=name,
            bash_command=(python_module_command("ops.gcp.dp2_method2") + " "
                          + common + f"--action {action} " + extra),
            cwd=str(REPOSITORY_ROOT),
            env=PYTHON_TASK_ENV,
            append_env=True,
            execution_timeout=timedelta(hours=timeout_hours),
        )

    approved = step("approve_and_verify_lineage", "approve")
    silver_pair = step("prepare_silver_pair", "prepare-silver")
    silver_baseline_quota = step("silver_baseline_quota", "quota", stage="silver", profile="baseline")
    silver_baseline_submit = step("silver_baseline_submit", "submit", stage="silver", profile="baseline", timeout_hours=10)
    silver_baseline_verify = step("silver_baseline_verify", "verify", stage="silver", profile="baseline")
    silver_optimized_quota = step("silver_optimized_quota", "quota", stage="silver", profile="optimized")
    silver_optimized_submit = step("silver_optimized_submit", "submit", stage="silver", profile="optimized", timeout_hours=10)
    silver_optimized_verify = step("silver_optimized_verify", "verify", stage="silver", profile="optimized")
    silver_baseline_summary = step("silver_baseline_semantic_scan", "summary", stage="silver", profile="baseline", timeout_hours=10)
    silver_optimized_summary = step("silver_optimized_semantic_scan", "summary", stage="silver", profile="optimized", timeout_hours=10)
    silver_baseline_evidence = step("silver_baseline_evidence", "evidence", stage="silver", profile="baseline")
    silver_optimized_evidence = step("silver_optimized_evidence", "evidence", stage="silver", profile="optimized")
    silver_winner = step("validate_and_select_silver_winner", "finalize", stage="silver")
    gold_pair = step("prepare_gold_pair_from_silver_winner", "prepare-gold")
    gold_baseline_quota = step("gold_baseline_quota", "quota", stage="gold", profile="baseline")
    gold_baseline_submit = step("gold_baseline_submit", "submit", stage="gold", profile="baseline", timeout_hours=10)
    gold_baseline_verify = step("gold_baseline_verify", "verify", stage="gold", profile="baseline")
    gold_optimized_quota = step("gold_optimized_quota", "quota", stage="gold", profile="optimized")
    gold_optimized_submit = step("gold_optimized_submit", "submit", stage="gold", profile="optimized", timeout_hours=10)
    gold_optimized_verify = step("gold_optimized_verify", "verify", stage="gold", profile="optimized")
    gold_baseline_summary = step("gold_baseline_semantic_scan", "summary", stage="gold", profile="baseline", timeout_hours=10)
    gold_optimized_summary = step("gold_optimized_semantic_scan", "summary", stage="gold", profile="optimized", timeout_hours=10)
    gold_baseline_evidence = step("gold_baseline_evidence", "evidence", stage="gold", profile="baseline")
    gold_optimized_evidence = step("gold_optimized_evidence", "evidence", stage="gold", profile="optimized")
    gold_winner = step("validate_and_select_gold_winner", "finalize", stage="gold")

    chain = [
        approved, silver_pair,
        silver_baseline_quota, silver_baseline_submit, silver_baseline_verify,
        silver_optimized_quota, silver_optimized_submit, silver_optimized_verify,
        silver_baseline_summary, silver_optimized_summary,
        silver_baseline_evidence, silver_optimized_evidence,
        silver_winner, gold_pair,
        gold_baseline_quota, gold_baseline_submit, gold_baseline_verify,
        gold_optimized_quota, gold_optimized_submit, gold_optimized_verify,
        gold_baseline_summary, gold_optimized_summary,
        gold_baseline_evidence, gold_optimized_evidence, gold_winner,
    ]
    for previous, following in zip(chain, chain[1:]):
        previous >> following
