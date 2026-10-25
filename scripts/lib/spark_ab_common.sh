#!/usr/bin/env bash

spark_ab_usage_error() { echo "ERROR: $*" >&2; exit 2; }

spark_ab_validate_profile() {
  [[ ${SPARK_PROFILE} == baseline || ${SPARK_PROFILE} == optimized ]] || \
    spark_ab_usage_error "--profile must be baseline or optimized"
}

spark_ab_validate_benchmark_id() {
  [[ ${BENCHMARK_ID} =~ ^ab-[0-9]{14}$ ]] || \
    spark_ab_usage_error "benchmark ID must match ab-YYYYMMDDhhmmss"
  [[ ${BENCHMARK_ID} != ab-20261025112811 ]] || \
    spark_ab_usage_error "benchmark ab-20261025112811 is troubleshooting evidence only; create a new benchmark ID"
}

spark_ab_init_airflow() {
  SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[1]}")" && pwd)
  REPOSITORY_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
  GIT_SHA=$(git -C "${REPOSITORY_ROOT}" rev-parse HEAD)
  [[ ${GIT_SHA} =~ ^[0-9a-f]{40}$ ]] || spark_ab_usage_error "invalid Git SHA"
  export AIRFLOW_HOME="${REPOSITORY_ROOT}/data_platform/orchestration/airflow"
  export AIRFLOW__CORE__DAGS_FOLDER="${AIRFLOW_HOME}/dags"
  export AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:8084/execution/"
  AIRFLOW_BIN=${AIRFLOW_BIN:-${HOME}/miniconda3/envs/airflow/bin/airflow}
  [[ -x ${AIRFLOW_BIN} ]] || spark_ab_usage_error "Airflow CLI not found: ${AIRFLOW_BIN}"
  local version
  version=$("${AIRFLOW_BIN}" version)
  [[ ${version} =~ ^3\. ]] || spark_ab_usage_error "expected Airflow 3.x, found ${version}"
  DATA_BUCKET=${EWALLET_DATA_BUCKET:-ewallet-data-ai-platform-hg26-data}
  [[ ${DATA_BUCKET} =~ ^[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]$ ]] || \
    spark_ab_usage_error "invalid EWALLET_DATA_BUCKET '${DATA_BUCKET}'"
  PROJECT_ID=${GCP_PROJECT_ID:-ewallet-data-ai-platform-hg26}
  REGION=${GCP_REGION:-asia-southeast1}
  [[ ${PROJECT_ID} =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || \
    spark_ab_usage_error "invalid GCP_PROJECT_ID '${PROJECT_ID}'"
  [[ ${REGION} =~ ^[a-z]+-[a-z]+[0-9]$ ]] || \
    spark_ab_usage_error "invalid GCP_REGION '${REGION}'"
}

spark_ab_print_profile() {
  echo "SPARK_PROFILE: ${SPARK_PROFILE^^}"
  if [[ ${SPARK_PROFILE} == baseline ]]; then
    echo "AQE: false"; echo "SKEW_JOIN: false"; echo "SHUFFLE_PARTITIONS: 200"
    echo "DYNAMIC_ALLOCATION: true"; echo "DYNAMIC_ALLOCATION_PROFILE: platform-neutral"
    echo "INITIAL_EXECUTORS: 6"; echo "MAX_EXECUTORS: 7"
  else
    echo "AQE: true"; echo "SKEW_JOIN: true"; echo "SHUFFLE_PARTITIONS: 400"
    echo "DYNAMIC_ALLOCATION: true"; echo "DYNAMIC_ALLOCATION_PROFILE: performance"
    echo "INITIAL_EXECUTORS: 6"; echo "MAX_EXECUTORS: 7"
  fi
  echo "TIER: standard"; echo "RUNTIME: 2.2"; echo "TTL: 8h"
}

spark_ab_print_trigger() {
  local dag_id=$1 run_id=$2 conf=$3
  printf 'AIRFLOW_HOME=%q AIRFLOW__CORE__DAGS_FOLDER=%q AIRFLOW__CORE__EXECUTION_API_SERVER_URL=%q airflow dags trigger %q --run-id %q --conf %q\n' \
    "${AIRFLOW_HOME}" "${AIRFLOW__CORE__DAGS_FOLDER}" \
    "${AIRFLOW__CORE__EXECUTION_API_SERVER_URL}" "${dag_id}" "${run_id}" "${conf}"
}

spark_ab_print_quota_guard() {
  local handoff=$1 predecessor=${2:-} next_batch=$3
  local output="docs/evidence/quota_handoffs/${BENCHMARK_ID}/${next_batch}-quota.json"
  local command=(
    python3 -m ops.gcp.wait_for_dataproc_quota
    --project "${PROJECT_ID}"
    --region "${REGION}"
    --spark-profile "${SPARK_PROFILE}"
    --benchmark-id "${BENCHMARK_ID}"
    --handoff "${handoff}"
    --evidence-output "${output}"
  )
  if [[ -n ${predecessor} ]]; then
    command+=(--predecessor-batch-id "${predecessor}")
  fi
  echo "Quota readiness plan (${handoff}; execute mode polls 15s→30s, timeout 900s):"
  (cd "${REPOSITORY_ROOT}" && "${command[@]}")
}

spark_ab_require_clean_commit() {
  local status
  status=$(git -C "${REPOSITORY_ROOT}" status --porcelain)
  [[ -z ${status} ]] || spark_ab_usage_error \
    "benchmark execution requires a clean committed worktree"
  [[ $(git -C "${REPOSITORY_ROOT}" rev-parse HEAD) == "${GIT_SHA}" ]] || \
    spark_ab_usage_error "Git HEAD changed after benchmark configuration"
}

spark_ab_execute_or_dry_run() {
  local dag_id=$1 run_id=$2 conf=$3
  if [[ ${EXECUTE} != true ]]; then
    echo "DRY-RUN only; no Airflow DAG or cloud workload was triggered."
    return
  fi
  spark_ab_require_clean_commit
  AIRFLOW_BIN="${AIRFLOW_BIN}" "${SCRIPT_DIR}/start_airflow_local.sh" --status
  "${AIRFLOW_BIN}" dags unpause -y "${dag_id}"
  "${AIRFLOW_BIN}" dags trigger "${dag_id}" --run-id "${run_id}" --conf "${conf}"
}
