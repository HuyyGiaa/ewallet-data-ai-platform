#!/usr/bin/env bash
set -euo pipefail

usage() { echo "Usage: ./scripts/run_dp1_canonical.sh --profile baseline|optimized [--benchmark-id ab-YYYYMMDDhhmmss] [--execute]"; }
SPARK_PROFILE=""; BENCHMARK_ID=${SPARK_BENCHMARK_ID:-}; EXECUTE=false
while (( $# )); do
  case $1 in
    --profile) SPARK_PROFILE=${2:-}; shift 2 ;;
    --benchmark-id) BENCHMARK_ID=${2:-}; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n ${SPARK_PROFILE} ]] || { usage >&2; exit 2; }
[[ -n ${BENCHMARK_ID} ]] || BENCHMARK_ID="ab-$(date -u +%Y%m%d%H%M%S)"
ATTEMPT_ID="r$(date -u +%y%m%d%H%M%S)"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${SCRIPT_DIR}/lib/spark_ab_common.sh"
spark_ab_validate_profile; spark_ab_validate_benchmark_id; spark_ab_init_airflow
BRONZE_RUN="${BENCHMARK_ID}-${SPARK_PROFILE}-bronze-${ATTEMPT_ID}"
DP1_RUN="${BENCHMARK_ID}-${SPARK_PROFILE}-dp1-${ATTEMPT_ID}"
CONF="{\"dataset_version\":\"canonical-100gb-v1\",\"benchmark_id\":\"${BENCHMARK_ID}\",\"spark_profile\":\"${SPARK_PROFILE}\",\"git_sha\":\"${GIT_SHA}\",\"bronze_run_id\":\"${BRONZE_RUN}\",\"dataproc_ttl\":\"8h\",\"data_bucket\":\"${DATA_BUCKET}\"}"
echo "MODE: $([[ ${EXECUTE} == true ]] && echo EXECUTE || echo DRY-RUN)"
echo "BENCHMARK_ID: ${BENCHMARK_ID}"; echo "DATASET_VERSION: canonical-100gb-v1"
echo "GIT_SHA: ${GIT_SHA}"
echo "BRONZE_RUN: ${BRONZE_RUN}"; echo "DP1_RUN: ${DP1_RUN}"
spark_ab_print_profile; echo "Airflow conf: ${CONF}"; echo "Exact trigger command:"
spark_ab_print_trigger dp1_source_to_bronze "${DP1_RUN}" "${CONF}"
spark_ab_execute_or_dry_run dp1_source_to_bronze "${DP1_RUN}" "${CONF}"
