#!/usr/bin/env bash
set -euo pipefail

usage() { echo "Usage: ./scripts/run_dp1_canonical.sh --pair-manifest FILE --profile baseline|optimized [--predecessor-batch-id ID] [--execute]"; }
SPARK_PROFILE=""; PAIR_MANIFEST=""; PREDECESSOR_BATCH_ID=""; EXECUTE=false
while (( $# )); do
  case $1 in
    --profile) SPARK_PROFILE=${2:-}; shift 2 ;;
    --pair-manifest) PAIR_MANIFEST=${2:-}; shift 2 ;;
    --predecessor-batch-id) PREDECESSOR_BATCH_ID=${2:-}; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n ${SPARK_PROFILE} ]] || { usage >&2; exit 2; }
[[ -f ${PAIR_MANIFEST} ]] || { usage >&2; exit 2; }
BENCHMARK_ID=$(python -m ops.gcp.inspect_spark_stage_pair --pair-manifest "${PAIR_MANIFEST}" --spark-profile "${SPARK_PROFILE}" --field benchmark_id)
BRONZE_RUN=$(python -m ops.gcp.inspect_spark_stage_pair --pair-manifest "${PAIR_MANIFEST}" --spark-profile "${SPARK_PROFILE}" --field output_run_id)
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${SCRIPT_DIR}/lib/spark_ab_common.sh"
spark_ab_validate_profile; spark_ab_validate_benchmark_id; spark_ab_init_airflow
DP1_RUN="${BRONZE_RUN/bronze/dp1}"
CONF="{\"dataset_version\":\"canonical-100gb-v1\",\"benchmark_id\":\"${BENCHMARK_ID}\",\"spark_profile\":\"${SPARK_PROFILE}\",\"git_sha\":\"${GIT_SHA}\",\"pair_manifest\":\"${PAIR_MANIFEST}\",\"bronze_run_id\":\"${BRONZE_RUN}\",\"dataproc_ttl\":\"8h\",\"project_id\":\"${PROJECT_ID}\",\"region\":\"${REGION}\",\"predecessor_batch_id\":\"${PREDECESSOR_BATCH_ID}\",\"data_bucket\":\"${DATA_BUCKET}\"}"
echo "MODE: $([[ ${EXECUTE} == true ]] && echo EXECUTE || echo DRY-RUN)"
echo "BENCHMARK_ID: ${BENCHMARK_ID}"; echo "DATASET_VERSION: canonical-100gb-v1"
echo "GIT_SHA: ${GIT_SHA}"
echo "BRONZE_RUN: ${BRONZE_RUN}"; echo "DP1_RUN: ${DP1_RUN}"
spark_ab_print_profile dp1-bronze
spark_ab_print_quota_guard external-to-bronze "${PREDECESSOR_BATCH_ID}" "${BRONZE_RUN}"
echo "Airflow conf: ${CONF}"; echo "Exact trigger command:"
spark_ab_print_trigger dp1_source_to_bronze "${DP1_RUN}" "${CONF}"
spark_ab_execute_or_dry_run dp1_source_to_bronze "${DP1_RUN}" "${CONF}"
