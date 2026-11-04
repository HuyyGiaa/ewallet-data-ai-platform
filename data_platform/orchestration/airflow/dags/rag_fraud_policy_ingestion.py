"""Airflow-orchestrated fraud-policy RAG ingestion (edai-2 RAG Data Pipeline).

discover → parse/chunk → embed → Feast publish (offline parquet, apply,
materialize to online) → online readback + top-k retrieval check → DataHub
lineage and quality assertions with readback.

Every task runs ``ops.llm_agent.rag_ingest`` in the uv-locked runtime
``infra/docker/rag-ingestion`` and writes one create-only artifact under
``docs/evidence/llm_agent/rag_<run_id>/``. Airflow runs this offline refresh
only; query-time retrieval is served by the knowledge_search MCP tool from the
published Feast rows.
"""

from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.sdk import DAG, Param
from airflow.providers.standard.operators.bash import BashOperator

from data_platform.orchestration.airflow.rag_ingestion_params import (
    RAG_PARAM_SCHEMAS,
    rag_stage_command,
    rag_task_environment,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]

# Params are templated only into environment VALUES (see rag_params); Bash
# expands each "${VAR}" once without re-parsing shell metacharacters.
RAG_PARAMS = {name: Param(**schema) for name, schema in RAG_PARAM_SCHEMAS.items()}
RAG_ENV = rag_task_environment(REPOSITORY_ROOT)

# task_id, stage, timeout
TASKS = (
    ("discover_documents", "discover", timedelta(minutes=5)),
    ("parse_and_chunk", "chunk", timedelta(minutes=5)),
    ("generate_embeddings", "embed", timedelta(minutes=20)),
    ("publish_feast_features", "publish", timedelta(minutes=15)),
    ("validate_online_readback", "validate", timedelta(minutes=15)),
    ("publish_datahub_lineage", "datahub", timedelta(minutes=10)),
)


with DAG(
    dag_id="rag_fraud_policy_ingestion",
    description="Ingest fraud-policy documents into Feast-backed RAG chunks with DataHub lineage",
    start_date=pendulum.datetime(2026, 10, 30, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=1)},
    params=RAG_PARAMS,
    tags=["ewallet", "llm-agent", "rag", "feast", "datahub", "manual"],
) as dag:
    previous = None
    for task_id, stage, timeout in TASKS:
        task = BashOperator(
            task_id=task_id,
            bash_command=rag_stage_command(stage),
            execution_timeout=timeout,
            cwd=str(REPOSITORY_ROOT), env=RAG_ENV, append_env=True,
        )
        if previous is not None:
            previous >> task
        previous = task
