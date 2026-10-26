#!/usr/bin/env bash
set -euo pipefail

usage() { echo "Usage: ./scripts/run_dp3_canonical.sh <benchmark-silver-run> <benchmark-gold-run> --pair-manifest FILE --profile baseline|optimized [--execute]"; }
(( $# >= 2 )) || { usage >&2; exit 2; }
SILVER_RUN=$1; GOLD_RUN=$2; shift 2
SPARK_PROFILE=""; PAIR_MANIFEST=""; EXECUTE=false
while (( $# )); do
  case $1 in
    --profile) SPARK_PROFILE=${2:-}; shift 2 ;;
    --pair-manifest) PAIR_MANIFEST=${2:-}; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n ${SPARK_PROFILE} ]] || { usage >&2; exit 2; }
[[ -f ${PAIR_MANIFEST} ]] || { usage >&2; exit 2; }
BENCHMARK_ID=${SILVER_RUN:0:17}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${SCRIPT_DIR}/lib/spark_ab_common.sh"
spark_ab_validate_profile; spark_ab_validate_benchmark_id
[[ ${SILVER_RUN} =~ ^${BENCHMARK_ID}-(baseline|optimized)-silver-r[0-9]{12}$ ]] || \
  spark_ab_usage_error "Silver run is not a selected immutable run for benchmark ${BENCHMARK_ID}"
[[ ${GOLD_RUN} =~ ^${BENCHMARK_ID}-(baseline|optimized)-gold-r[0-9]{12}$ ]] || \
  spark_ab_usage_error "Gold run is not a selected immutable run for benchmark ${BENCHMARK_ID}"
spark_ab_init_airflow
FEATURE_RUN=$(python -m ops.gcp.inspect_spark_stage_pair --pair-manifest "${PAIR_MANIFEST}" --spark-profile "${SPARK_PROFILE}" --field output_run_id)
DP3_RUN="${FEATURE_RUN/feat/dp3}"
CONF="{\"dataset_version\":\"canonical-100gb-v1\",\"benchmark_id\":\"${BENCHMARK_ID}\",\"spark_profile\":\"${SPARK_PROFILE}\",\"git_sha\":\"${GIT_SHA}\",\"pair_manifest\":\"${PAIR_MANIFEST}\",\"silver_run_id\":\"${SILVER_RUN}\",\"gold_run_id\":\"${GOLD_RUN}\",\"feature_run_id\":\"${FEATURE_RUN}\",\"dataproc_ttl\":\"8h\",\"project_id\":\"${PROJECT_ID}\",\"region\":\"${REGION}\",\"data_bucket\":\"${DATA_BUCKET}\"}"
echo "MODE: $([[ ${EXECUTE} == true ]] && echo EXECUTE || echo DRY-RUN)"
echo "BENCHMARK_ID: ${BENCHMARK_ID}"; echo "SILVER_RUN: ${SILVER_RUN}"
echo "GIT_SHA: ${GIT_SHA}"
echo "GOLD_RUN: ${GOLD_RUN}"; echo "FEATURE_RUN: ${FEATURE_RUN}"; echo "DP3_RUN: ${DP3_RUN}"
spark_ab_print_profile dp3-features
spark_ab_print_quota_guard gold-to-features "${GOLD_RUN}" "${FEATURE_RUN}"
echo "Airflow conf: ${CONF}"; echo "Exact trigger command:"
spark_ab_print_trigger dp3_historical_features "${DP3_RUN}" "${CONF}"
spark_ab_execute_or_dry_run dp3_historical_features "${DP3_RUN}" "${CONF}"
