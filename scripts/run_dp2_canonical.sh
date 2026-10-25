#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ./scripts/run_dp2_canonical.sh <successful-canonical-bronze-run> [--execute]"
}

if (( $# < 1 || $# > 2 )); then usage >&2; exit 2; fi
BRONZE_RUN=$1
MODE=${2:-}
if [[ -n ${MODE} && ${MODE} != --execute ]]; then usage >&2; exit 2; fi
if [[ ! ${BRONZE_RUN} =~ ^canonical-bronze-[0-9]{8}-[0-9]{6}$ ]]; then
  echo "ERROR: expected a successful canonical Bronze run ID, got '${BRONZE_RUN}'." >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPOSITORY_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
export AIRFLOW_HOME="${REPOSITORY_ROOT}/data_platform/orchestration/airflow"
export AIRFLOW__CORE__DAGS_FOLDER="${AIRFLOW_HOME}/dags"
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:8084/execution/"
AIRFLOW_BIN=${AIRFLOW_BIN:-${HOME}/miniconda3/envs/airflow/bin/airflow}
if [[ ! -x ${AIRFLOW_BIN} ]]; then
  echo "ERROR: Airflow CLI not found at '${AIRFLOW_BIN}'. Activate conda env 'airflow' or set AIRFLOW_BIN." >&2
  exit 1
fi
version=$("${AIRFLOW_BIN}" version)
if [[ ! ${version} =~ ^3\. ]]; then echo "ERROR: expected Airflow 3.x, found ${version}." >&2; exit 1; fi

DATA_BUCKET=${EWALLET_DATA_BUCKET:-ewallet-data-ai-platform-hg26-data}
if [[ ! ${DATA_BUCKET} =~ ^[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]$ ]]; then
  echo "ERROR: invalid EWALLET_DATA_BUCKET '${DATA_BUCKET}'." >&2
  exit 1
fi
STAMP=$(date -u +%Y%m%d-%H%M%S)
SILVER_RUN="canonical-silver-${STAMP}"
GOLD_RUN="canonical-gold-${STAMP}"
DP2_RUN="canonical-dp2-${STAMP}"
CONF="{\"dataset_version\":\"canonical-100gb-v1\",\"bronze_run_id\":\"${BRONZE_RUN}\",\"silver_run_id\":\"${SILVER_RUN}\",\"gold_run_id\":\"${GOLD_RUN}\",\"dataproc_ttl\":\"8h\",\"dataproc_performance_profile\":\"canonical\",\"data_bucket\":\"${DATA_BUCKET}\"}"

echo "MODE: $([[ ${MODE} == --execute ]] && echo EXECUTE || echo DRY-RUN)"
echo "DATASET_VERSION: canonical-100gb-v1"
echo "BRONZE_RUN: ${BRONZE_RUN}"
echo "SILVER_RUN: ${SILVER_RUN}"
echo "GOLD_RUN: ${GOLD_RUN}"
echo "DP2_RUN: ${DP2_RUN}"
echo "Airflow conf: ${CONF}"
echo "Exact trigger command:"
printf 'AIRFLOW_HOME=%q AIRFLOW__CORE__DAGS_FOLDER=%q AIRFLOW__CORE__EXECUTION_API_SERVER_URL=%q airflow dags trigger dp2_bronze_to_curated --run-id %q --conf %q\n' \
  "${AIRFLOW_HOME}" "${AIRFLOW__CORE__DAGS_FOLDER}" "${AIRFLOW__CORE__EXECUTION_API_SERVER_URL}" "${DP2_RUN}" "${CONF}"

if [[ ${MODE} != --execute ]]; then
  echo "DRY-RUN only; no Airflow DAG or cloud workload was triggered. Add --execute after confirming BRONZE_RUN."
else
  AIRFLOW_BIN="${AIRFLOW_BIN}" "${SCRIPT_DIR}/start_airflow_local.sh" --status
  "${AIRFLOW_BIN}" dags unpause -y dp2_bronze_to_curated
  "${AIRFLOW_BIN}" dags trigger dp2_bronze_to_curated --run-id "${DP2_RUN}" --conf "${CONF}"
fi

echo "Monitor with:"
printf 'AIRFLOW_HOME=%q AIRFLOW__CORE__DAGS_FOLDER=%q AIRFLOW__CORE__EXECUTION_API_SERVER_URL=%q airflow tasks states-for-dag-run dp2_bronze_to_curated %q\n' \
  "${AIRFLOW_HOME}" "${AIRFLOW__CORE__DAGS_FOLDER}" "${AIRFLOW__CORE__EXECUTION_API_SERVER_URL}" "${DP2_RUN}"
