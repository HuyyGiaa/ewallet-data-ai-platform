#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPOSITORY_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
export AIRFLOW_HOME="${REPOSITORY_ROOT}/data_platform/orchestration/airflow"
export AIRFLOW__CORE__DAGS_FOLDER="${AIRFLOW_HOME}/dags"
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:8084/execution/"
# DAG files and task commands import project modules (data_platform, ops):
# every component needs the repository root on the import path.
export PYTHONPATH="${REPOSITORY_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

usage() {
  cat <<'EOF'
Usage: ./scripts/start_airflow_local.sh [--status | --stop]

With no option, reuse a healthy local Airflow 3 runtime, or start the missing
components one at a time (API server on 127.0.0.1:8084, then scheduler, then
DAG processor), waiting for each to be healthy.
  --status  read-only report of the three components and the health endpoint
  --stop    SIGTERM to this runtime's own processes only (never force-killed)

This helper never initializes, migrates or resets the metadata database, and
never triggers a DAG. AIRFLOW_LOCAL_START_TIMEOUT sets the per-component wait
in seconds (default 120). Runbook: docs/runbooks/edai1/airflow/AIRFLOW_LOCAL_RUNTIME.md
EOF
}

resolve_airflow() {
  if [[ -n ${AIRFLOW_BIN:-} && -x ${AIRFLOW_BIN} ]]; then
    return
  fi
  if [[ -n ${CONDA_PREFIX:-} && ${CONDA_DEFAULT_ENV:-} == airflow \
        && -x ${CONDA_PREFIX}/bin/airflow ]]; then
    AIRFLOW_BIN="${CONDA_PREFIX}/bin/airflow"
  elif [[ -x ${HOME}/miniconda3/envs/airflow/bin/airflow ]]; then
    AIRFLOW_BIN="${HOME}/miniconda3/envs/airflow/bin/airflow"
  else
    echo "ERROR: Airflow 3 CLI not found. Activate conda env 'airflow' or set AIRFLOW_BIN." >&2
    exit 1
  fi
  export AIRFLOW_BIN
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --status) action=status ;;
  --stop) action=stop ;;
  "") action=start ;;
  *) usage >&2; exit 2 ;;
esac

resolve_airflow
# The runtime manager is standard-library Python; use the Airflow interpreter.
PYTHON_BIN="$(dirname -- "${AIRFLOW_BIN}")/python"
[[ -x ${PYTHON_BIN} ]] || PYTHON_BIN=python3

if [[ ${action} == start ]]; then
  version=$("${AIRFLOW_BIN}" version)
  if [[ ! ${version} =~ ^3\. ]]; then
    echo "ERROR: expected Airflow 3.x, found ${version}." >&2
    exit 1
  fi
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/airflow_local_runtime.py" "${action}" \
  --airflow-home "${AIRFLOW_HOME}" --airflow-bin "${AIRFLOW_BIN}"
