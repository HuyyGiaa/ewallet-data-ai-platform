#!/usr/bin/env bash
set -euo pipefail

usage() { echo "Usage: ./scripts/run_dp2_canonical.sh <benchmark-bronze-run> --profile baseline|optimized [--execute]"; }
(( $# >= 1 )) || { usage >&2; exit 2; }
BRONZE_RUN=$1; shift
SPARK_PROFILE=""; EXECUTE=false
while (( $# )); do
  case $1 in
    --profile) SPARK_PROFILE=${2:-}; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n ${SPARK_PROFILE} ]] || { usage >&2; exit 2; }
BENCHMARK_ID=${BRONZE_RUN:0:17}
ATTEMPT_ID="r$(date -u +%y%m%d%H%M%S)"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${SCRIPT_DIR}/lib/spark_ab_common.sh"
spark_ab_validate_profile; spark_ab_validate_benchmark_id
[[ ${BRONZE_RUN} =~ ^${BENCHMARK_ID}-${SPARK_PROFILE}-bronze-r[0-9]{12}$ ]] || \
  spark_ab_usage_error "Bronze run is not bound to benchmark ${BENCHMARK_ID}/${SPARK_PROFILE}"
spark_ab_init_airflow
SILVER_RUN="${BENCHMARK_ID}-${SPARK_PROFILE}-silver-${ATTEMPT_ID}"
GOLD_RUN="${BENCHMARK_ID}-${SPARK_PROFILE}-gold-${ATTEMPT_ID}"
DP2_RUN="${BENCHMARK_ID}-${SPARK_PROFILE}-dp2-${ATTEMPT_ID}"
CONF="{\"dataset_version\":\"canonical-100gb-v1\",\"benchmark_id\":\"${BENCHMARK_ID}\",\"spark_profile\":\"${SPARK_PROFILE}\",\"git_sha\":\"${GIT_SHA}\",\"bronze_run_id\":\"${BRONZE_RUN}\",\"silver_run_id\":\"${SILVER_RUN}\",\"gold_run_id\":\"${GOLD_RUN}\",\"dataproc_ttl\":\"8h\",\"data_bucket\":\"${DATA_BUCKET}\"}"
echo "MODE: $([[ ${EXECUTE} == true ]] && echo EXECUTE || echo DRY-RUN)"
echo "BENCHMARK_ID: ${BENCHMARK_ID}"; echo "BRONZE_RUN: ${BRONZE_RUN}"
echo "GIT_SHA: ${GIT_SHA}"
echo "SILVER_RUN: ${SILVER_RUN}"; echo "GOLD_RUN: ${GOLD_RUN}"; echo "DP2_RUN: ${DP2_RUN}"
spark_ab_print_profile; echo "Airflow conf: ${CONF}"; echo "Exact trigger command:"
spark_ab_print_trigger dp2_bronze_to_curated "${DP2_RUN}" "${CONF}"
spark_ab_execute_or_dry_run dp2_bronze_to_curated "${DP2_RUN}" "${CONF}"
