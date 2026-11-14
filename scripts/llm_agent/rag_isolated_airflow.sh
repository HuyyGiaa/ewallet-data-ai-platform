#!/usr/bin/env bash
# Run an isolated Airflow 3 control plane that sees only the RAG ingestion DAG.
#
#   scripts/llm_agent/rag_isolated_airflow.sh start|status|stop
#   scripts/llm_agent/rag_isolated_airflow.sh airflow <args...>   # CLI against this instance
#
# AIRFLOW_HOME ~/.cache/ewallet-rag-airflow, UI http://127.0.0.1:8095, log servers 8798/8799,
# DAG rag_fraud_policy_ingestion only. The mechanics are in scripts/isolated_airflow.sh.
# The tasks inherit from this environment:
#   EWALLET_RAG_ENV_FILE       private KEY=VALUE settings (Redis, Feast registry, DataHub); default
#                              ~/.config/ewallet-rag/runtime.local.env written by rag_runtime_run.sh prepare
#   EWALLET_RAG_EVIDENCE_ROOT  run directories, outside the checkout; default ~/ewallet-evidence/rag
#   HF_HOME                    embedding model cache; default ~/.cache/ewallet-rag-hf
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ISOLATED_AIRFLOW_HOME="${RAG_AIRFLOW_HOME:-$HOME/.cache/ewallet-rag-airflow}"
export ISOLATED_AIRFLOW_PORT="${RAG_AIRFLOW_PORT:-8095}"
export ISOLATED_AIRFLOW_WORKER_LOG_PORT="${RAG_WORKER_LOG_PORT:-8798}"
export ISOLATED_AIRFLOW_TRIGGER_LOG_PORT="${RAG_TRIGGER_LOG_PORT:-8799}"
export ISOLATED_AIRFLOW_DAGS="data_platform/orchestration/airflow/dags/rag_fraud_policy_ingestion.py"
export EWALLET_RAG_ENV_FILE="${EWALLET_RAG_ENV_FILE:-$HOME/.config/ewallet-rag/runtime.local.env}"
export EWALLET_RAG_EVIDENCE_ROOT="${EWALLET_RAG_EVIDENCE_ROOT:-$HOME/ewallet-evidence/rag}"
export HF_HOME="${HF_HOME:-$HOME/.cache/ewallet-rag-hf}"
export EWALLET_RAG_PYTHON="${EWALLET_RAG_PYTHON:-$ROOT/infra/docker/rag-ingestion/.venv/bin/python}"
exec "$ROOT/scripts/isolated_airflow.sh" "$@"
