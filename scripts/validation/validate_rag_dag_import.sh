#!/usr/bin/env bash
# Parse the RAG DAG with a real Airflow interpreter in a throwaway AIRFLOW_HOME.
# Never reads the live Airflow config or metadata DB, registers or triggers nothing.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AIRFLOW_PYTHON="${1:?usage: $0 /path/to/airflow-env/bin/python [report.json]}"
REPORT="$(realpath -m "${2:?usage: $0 /path/to/airflow-env/bin/python report.json}")"
ISOLATED_HOME="$(mktemp -d "${TMPDIR:-/tmp}/rag-dag-parse.XXXXXX")"
trap 'rm -rf "${ISOLATED_HOME}"' EXIT
mkdir -p "${ISOLATED_HOME}/empty-dags"

cd "${ROOT_DIR}"
env -i HOME="${ISOLATED_HOME}" PATH=/usr/bin:/bin PYTHONPATH="${ROOT_DIR}" PYTHONDONTWRITEBYTECODE=1 \
  TMPDIR="${TMPDIR:-/tmp}" \
  AIRFLOW_HOME="${ISOLATED_HOME}" \
  AIRFLOW__CORE__DAGS_FOLDER="${ISOLATED_HOME}/empty-dags" \
  AIRFLOW__CORE__LOAD_EXAMPLES=False \
  AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:///${ISOLATED_HOME}/isolated.db" \
  AIRFLOW__LOGGING__BASE_LOG_FOLDER="${ISOLATED_HOME}/logs" \
  "${AIRFLOW_PYTHON}" scripts/validation/rag_dag_isolated_parse.py \
  "${ROOT_DIR}/data_platform/orchestration/airflow/dags/rag_fraud_policy.py" "${REPORT}" >&2
