#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ./scripts/run_dp3_canonical.sh <successful-canonical-silver-run> <successful-canonical-gold-run> [--execute]"
}

if (( $# < 2 || $# > 3 )); then usage >&2; exit 2; fi
SILVER_RUN=$1
GOLD_RUN=$2
MODE=${3:-}
if [[ -n ${MODE} && ${MODE} != --execute ]]; then usage >&2; exit 2; fi
if [[ ! ${SILVER_RUN} =~ ^canonical-silver-[0-9]{8}-[0-9]{6}$ ]]; then
  echo "ERROR: expected a successful canonical Silver run ID, got '${SILVER_RUN}'." >&2
  exit 1
fi
if [[ ! ${GOLD_RUN} =~ ^canonical-gold-[0-9]{8}-[0-9]{6}$ ]]; then
  echo "ERROR: expected a successful canonical Gold run ID, got '${GOLD_RUN}'." >&2
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
FEATURE_RUN="canonical-features-${STAMP}"
DP3_RUN="canonical-dp3-${STAMP}"
CONF="{\"dataset_version\":\"canonical-100gb-v1\",\"silver_run_id\":\"${SILVER_RUN}\",\"gold_run_id\":\"${GOLD_RUN}\",\"feature_run_id\":\"${FEATURE_RUN}\",\"dataproc_ttl\":\"8h\",\"dataproc_performance_profile\":\"canonical\",\"data_bucket\":\"${DATA_BUCKET}\"}"

echo "MODE: $([[ ${MODE} == --execute ]] && echo EXECUTE || echo DRY-RUN)"
echo "DATASET_VERSION: canonical-100gb-v1"
echo "SILVER_RUN: ${SILVER_RUN}"
echo "GOLD_RUN: ${GOLD_RUN}"
echo "FEATURE_RUN: ${FEATURE_RUN}"
echo "DP3_RUN: ${DP3_RUN}"
echo "Airflow conf: ${CONF}"
echo "Exact trigger command:"
printf 'AIRFLOW_HOME=%q AIRFLOW__CORE__DAGS_FOLDER=%q AIRFLOW__CORE__EXECUTION_API_SERVER_URL=%q airflow dags trigger dp3_historical_features --run-id %q --conf %q\n' \
  "${AIRFLOW_HOME}" "${AIRFLOW__CORE__DAGS_FOLDER}" "${AIRFLOW__CORE__EXECUTION_API_SERVER_URL}" "${DP3_RUN}" "${CONF}"

if [[ ${MODE} != --execute ]]; then
  echo "DRY-RUN only; no Airflow DAG or cloud workload was triggered. Add --execute after confirming both upstream runs passed."
else
  AIRFLOW_BIN="${AIRFLOW_BIN}" "${SCRIPT_DIR}/start_airflow_local.sh" --status
  "${AIRFLOW_BIN}" dags unpause -y dp3_historical_features
  "${AIRFLOW_BIN}" dags trigger dp3_historical_features --run-id "${DP3_RUN}" --conf "${CONF}"
fi

echo "Monitor with:"
printf 'AIRFLOW_HOME=%q AIRFLOW__CORE__DAGS_FOLDER=%q AIRFLOW__CORE__EXECUTION_API_SERVER_URL=%q airflow tasks states-for-dag-run dp3_historical_features %q\n' \
  "${AIRFLOW_HOME}" "${AIRFLOW__CORE__DAGS_FOLDER}" "${AIRFLOW__CORE__EXECUTION_API_SERVER_URL}" "${DP3_RUN}"
