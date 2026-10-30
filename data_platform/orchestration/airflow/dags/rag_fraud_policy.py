"""Manual Phase C DAG for immutable fraud-policy RAG artifacts.

The DAG builds source artifacts only. Feast apply/materialization and DataHub
publication/readback are deliberately separate approved runtime gates.
"""

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
MANIFEST = "data/llm_agent/rag/fraud-policy-v1/manifest.json"
EVALUATION = "data/llm_agent/rag/fraud-policy-v1/evaluation.json"


with DAG(
    dag_id="rag_fraud_policy",
    description="Build and evaluate immutable Feast-backed fraud-policy RAG artifacts",
    start_date=pendulum.datetime(2026, 10, 30, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    params={
        "embedding_endpoint": Param("http://rag-embedding.e-wallet-llm-agent.svc:8000", type="string"),
        "api_key_file": Param("/var/run/secrets/rag-embedding/api-key", type="string"),
        "online_store": Param("redis", type="string"),
        "evidence_dir": Param("docs/evidence/llm_agent/rag/manual-run-id", type="string"),
    },
    tags=["ewallet", "llm-agent", "rag", "manual"],
) as dag:
    validate_corpus = BashOperator(
        task_id="validate_versioned_corpus",
        bash_command=python_module_command("ops.llm_agent.rag_pipeline") + f" --manifest {MANIFEST} --validate-only",
        cwd=str(REPOSITORY_ROOT), env=PYTHON_TASK_ENV, append_env=True,
    )
    build_publication = BashOperator(
        task_id="build_feast_publication_artifact",
        bash_command=(
            python_module_command("ops.llm_agent.rag_pipeline")
            + f" --manifest {MANIFEST}"
            + " --embedding-endpoint '{{ params.embedding_endpoint }}'"
            + " --api-key-file '{{ params.api_key_file }}'"
            + " --online-store '{{ params.online_store }}'"
            + " --output '{{ params.evidence_dir }}/publication.json'"
        ),
        execution_timeout=timedelta(minutes=30),
        cwd=str(REPOSITORY_ROOT), env=PYTHON_TASK_ENV, append_env=True,
    )
    evaluate_retrieval = BashOperator(
        task_id="evaluate_retrieval_with_citations",
        bash_command=(
            python_module_command("ops.llm_agent.run_rag_evaluation")
            + f" --manifest {MANIFEST} --evaluation {EVALUATION}"
            + " --embedding-endpoint '{{ params.embedding_endpoint }}'"
            + " --api-key-file '{{ params.api_key_file }}'"
            + " --output '{{ params.evidence_dir }}/evaluation.json'"
        ),
        execution_timeout=timedelta(minutes=30),
        cwd=str(REPOSITORY_ROOT), env=PYTHON_TASK_ENV, append_env=True,
    )
    verify_artifacts = BashOperator(
        task_id="verify_source_artifacts_and_runtime_gate",
        bash_command=(
            python_module_command("ops.llm_agent.verify_rag_artifacts")
            + " --build-artifact '{{ params.evidence_dir }}/publication.json'"
            + " --evaluation-artifact '{{ params.evidence_dir }}/evaluation.json'"
        ),
        cwd=str(REPOSITORY_ROOT), env=PYTHON_TASK_ENV, append_env=True,
    )
    validate_corpus >> [build_publication, evaluate_retrieval] >> verify_artifacts
