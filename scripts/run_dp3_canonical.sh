#!/usr/bin/env bash
set -euo pipefail

usage() { echo "Usage: ./scripts/run_dp3_canonical.sh <benchmark-silver-run> <benchmark-gold-run> --profile baseline|optimized [--execute]"; }
(( $# >= 2 )) || { usage >&2; exit 2; }
SILVER_RUN=$1; GOLD_RUN=$2; shift 2
SPARK_PROFILE=""; EXECUTE=false
while (( $# )); do
  case $1 in
    --profile) SPARK_PROFILE=${2:-}; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n ${SPARK_PROFILE} ]] || { usage >&2; exit 2; }
BENCHMARK_ID=${SILVER_RUN:0:17}
ATTEMPT_ID="r$(date -u +%y%m%d%H%M%S)"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${SCRIPT_DIR}/lib/spark_ab_common.sh"
spark_ab_validate_profile; spark_ab_validate_benchmark_id
[[ ${SILVER_RUN} =~ ^${BENCHMARK_ID}-${SPARK_PROFILE}-silver-r[0-9]{12}$ ]] || \
  spark_ab_usage_error "Silver run is not bound to benchmark ${BENCHMARK_ID}/${SPARK_PROFILE}"
[[ ${GOLD_RUN} =~ ^${BENCHMARK_ID}-${SPARK_PROFILE}-gold-r[0-9]{12}$ ]] || \
  spark_ab_usage_error "Gold run is not bound to benchmark ${BENCHMARK_ID}/${SPARK_PROFILE}"
spark_ab_init_airflow
FEATURE_RUN="${BENCHMARK_ID}-${SPARK_PROFILE}-feat-${ATTEMPT_ID}"
DP3_RUN="${BENCHMARK_ID}-${SPARK_PROFILE}-dp3-${ATTEMPT_ID}"
CONF="{\"dataset_version\":\"canonical-100gb-v1\",\"benchmark_id\":\"${BENCHMARK_ID}\",\"spark_profile\":\"${SPARK_PROFILE}\",\"git_sha\":\"${GIT_SHA}\",\"silver_run_id\":\"${SILVER_RUN}\",\"gold_run_id\":\"${GOLD_RUN}\",\"feature_run_id\":\"${FEATURE_RUN}\",\"dataproc_ttl\":\"8h\",\"data_bucket\":\"${DATA_BUCKET}\"}"
echo "MODE: $([[ ${EXECUTE} == true ]] && echo EXECUTE || echo DRY-RUN)"
echo "BENCHMARK_ID: ${BENCHMARK_ID}"; echo "SILVER_RUN: ${SILVER_RUN}"
echo "GIT_SHA: ${GIT_SHA}"
echo "GOLD_RUN: ${GOLD_RUN}"; echo "FEATURE_RUN: ${FEATURE_RUN}"; echo "DP3_RUN: ${DP3_RUN}"
spark_ab_print_profile; echo "Airflow conf: ${CONF}"; echo "Exact trigger command:"
spark_ab_print_trigger dp3_historical_features "${DP3_RUN}" "${CONF}"
spark_ab_execute_or_dry_run dp3_historical_features "${DP3_RUN}" "${CONF}"
