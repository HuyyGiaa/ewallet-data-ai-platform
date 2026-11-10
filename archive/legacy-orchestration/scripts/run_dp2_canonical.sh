#!/usr/bin/env bash
set -euo pipefail

usage() { echo "Usage: ./scripts/run_dp2_canonical.sh <benchmark-bronze-run> --silver-pair-manifest FILE --gold-pair-manifest FILE --profile baseline|optimized [--execute]"; }
(( $# >= 1 )) || { usage >&2; exit 2; }
BRONZE_RUN=$1; shift
SPARK_PROFILE=""; SILVER_PAIR_MANIFEST=""; GOLD_PAIR_MANIFEST=""; EXECUTE=false
while (( $# )); do
  case $1 in
    --profile) SPARK_PROFILE=${2:-}; shift 2 ;;
    --silver-pair-manifest) SILVER_PAIR_MANIFEST=${2:-}; shift 2 ;;
    --gold-pair-manifest) GOLD_PAIR_MANIFEST=${2:-}; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n ${SPARK_PROFILE} ]] || { usage >&2; exit 2; }
if [[ ${EXECUTE} == true ]]; then
  echo "ERROR: Legacy single-profile DAG2 trigger is disabled after Method2 refactor. Use scripts/run_dp2_method2.sh." >&2
  exit 2
fi
[[ -f ${SILVER_PAIR_MANIFEST} && -f ${GOLD_PAIR_MANIFEST} ]] || { usage >&2; exit 2; }
BENCHMARK_ID=${BRONZE_RUN:0:17}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${SCRIPT_DIR}/lib/spark_ab_common.sh"
spark_ab_validate_profile; spark_ab_validate_benchmark_id
[[ ${BRONZE_RUN} =~ ^${BENCHMARK_ID}-(baseline|optimized)-bronze-r[0-9]{12}$ ]] || \
  spark_ab_usage_error "Bronze run is not a selected immutable run for benchmark ${BENCHMARK_ID}"
spark_ab_init_airflow
SILVER_RUN=$(python -m ops.gcp.inspect_spark_stage_pair --pair-manifest "${SILVER_PAIR_MANIFEST}" --spark-profile "${SPARK_PROFILE}" --field output_run_id)
GOLD_RUN=$(python -m ops.gcp.inspect_spark_stage_pair --pair-manifest "${GOLD_PAIR_MANIFEST}" --spark-profile "${SPARK_PROFILE}" --field output_run_id)
DP2_RUN="${SILVER_RUN/silver/dp2}"
CONF="{\"dataset_version\":\"canonical-100gb-v1\",\"benchmark_id\":\"${BENCHMARK_ID}\",\"spark_profile\":\"${SPARK_PROFILE}\",\"git_sha\":\"${GIT_SHA}\",\"silver_pair_manifest\":\"${SILVER_PAIR_MANIFEST}\",\"gold_pair_manifest\":\"${GOLD_PAIR_MANIFEST}\",\"bronze_run_id\":\"${BRONZE_RUN}\",\"silver_run_id\":\"${SILVER_RUN}\",\"gold_run_id\":\"${GOLD_RUN}\",\"dataproc_ttl\":\"8h\",\"project_id\":\"${PROJECT_ID}\",\"region\":\"${REGION}\",\"data_bucket\":\"${DATA_BUCKET}\"}"
echo "MODE: $([[ ${EXECUTE} == true ]] && echo EXECUTE || echo DRY-RUN)"
echo "BENCHMARK_ID: ${BENCHMARK_ID}"; echo "BRONZE_RUN: ${BRONZE_RUN}"
echo "GIT_SHA: ${GIT_SHA}"
echo "SILVER_RUN: ${SILVER_RUN}"; echo "GOLD_RUN: ${GOLD_RUN}"; echo "DP2_RUN: ${DP2_RUN}"
spark_ab_print_profile dp2-silver
spark_ab_print_profile dp2-gold
spark_ab_print_quota_guard bronze-to-silver "${BRONZE_RUN}" "${SILVER_RUN}"
spark_ab_print_quota_guard silver-to-gold "${SILVER_RUN}" "${GOLD_RUN}"
echo "Airflow conf: ${CONF}"; echo "Exact trigger command:"
spark_ab_print_trigger dp2_bronze_to_curated "${DP2_RUN}" "${CONF}"
spark_ab_execute_or_dry_run dp2_bronze_to_curated "${DP2_RUN}" "${CONF}"
