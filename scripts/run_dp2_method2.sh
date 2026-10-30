#!/usr/bin/env bash
# A single Airflow trigger; four immutable processing batches plus four read-only scans.
set -euo pipefail

usage() {
  cat >&2 <<'HELP'
Usage: scripts/run_dp2_method2.sh BRONZE_WINNER.json \
  --artifact-root ABSOLUTE_OUTSIDE_REPO \
  --baseline-silver-run-id RUN --optimized-silver-run-id RUN \
  --baseline-gold-run-id RUN --optimized-gold-run-id RUN [--execute]

Without --execute this performs local lineage/identity checks and prints the
exact Airflow trigger. --execute explicitly authorizes paid GCP workloads.
HELP
}
(( $# >= 1 )) || { usage; exit 2; }
BRONZE_WINNER=$1; shift
ARTIFACT_ROOT=""; BASE_S=""; OPT_S=""; BASE_G=""; OPT_G=""; EXECUTE=false
while (( $# )); do
  case "$1" in
    --artifact-root) ARTIFACT_ROOT=${2:?}; shift 2 ;;
    --baseline-silver-run-id) BASE_S=${2:?}; shift 2 ;;
    --optimized-silver-run-id) OPT_S=${2:?}; shift 2 ;;
    --baseline-gold-run-id) BASE_G=${2:?}; shift 2 ;;
    --optimized-gold-run-id) OPT_G=${2:?}; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    *) usage; exit 2 ;;
  esac
done
[[ -n $ARTIFACT_ROOT && -n $BASE_S && -n $OPT_S && -n $BASE_G && -n $OPT_G ]] || { usage; exit 2; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${SCRIPT_DIR}/lib/spark_ab_common.sh"
spark_ab_init_airflow
BRONZE_WINNER=$(realpath -- "$BRONZE_WINNER")
[[ "$ARTIFACT_ROOT" = /* ]] || spark_ab_usage_error "--artifact-root must be absolute"
mkdir -p -- "$ARTIFACT_ROOT"
ARTIFACT_ROOT=$(realpath -- "$ARTIFACT_ROOT")
BENCHMARK_ID=$(python3 - "$BRONZE_WINNER" <<'PY'
import json, sys
x=json.load(open(sys.argv[1]))
print(x['benchmark_id'])
PY
)
spark_ab_validate_benchmark_id
APPROVAL="NOT_APPROVED"
APPROVAL_ARGS=()
if [[ $EXECUTE == true ]]; then
  APPROVAL="I_APPROVE_DP2_PAID_JOBS"
  APPROVAL_ARGS+=(--require-clean)
fi
python3 -m ops.gcp.dp2_method2 --action approve \
  --root "$ARTIFACT_ROOT" --bronze-winner "$BRONZE_WINNER" \
  --benchmark-id "$BENCHMARK_ID" --dataset-version canonical-100gb-v1 \
  --git-sha "$GIT_SHA" --data-bucket "$DATA_BUCKET" \
  --project "$PROJECT_ID" --region "$REGION" --ttl 8h \
  --baseline-silver-run-id "$BASE_S" --optimized-silver-run-id "$OPT_S" \
  --baseline-gold-run-id "$BASE_G" --optimized-gold-run-id "$OPT_G" \
  --approval I_APPROVE_DP2_PAID_JOBS "${APPROVAL_ARGS[@]}"

CONF=$(python3 - "$BENCHMARK_ID" "$GIT_SHA" "$BRONZE_WINNER" "$ARTIFACT_ROOT" \
  "$BASE_S" "$OPT_S" "$BASE_G" "$OPT_G" "$APPROVAL" "$DATA_BUCKET" "$PROJECT_ID" "$REGION" <<'PY'
import json,sys
(b,sha,winner,root,bs,os,bg,og,approval,bucket,project,region)=sys.argv[1:]
print(json.dumps({
  'benchmark_id':b, 'dataset_version':'canonical-100gb-v1', 'git_sha':sha,
  'bronze_winner':winner, 'artifact_root':root,
  'baseline_silver_run_id':bs, 'optimized_silver_run_id':os,
  'baseline_gold_run_id':bg, 'optimized_gold_run_id':og,
  'approval':approval, 'ttl':'8h', 'data_bucket':bucket,
  'project_id':project, 'region':region,
},separators=(',',':')))
PY
)
DAG_ID=dp2_bronze_to_curated
DAG_RUN_ID="dp2-method2-${BENCHMARK_ID}"
echo "MODE: $([[ $EXECUTE == true ]] && echo 'PAID EXECUTE' || echo 'DRY RUN')"
echo "Bronze Winner: $BRONZE_WINNER"
echo "DP2 evidence folder: $ARTIFACT_ROOT"
echo "DAG run: $DAG_RUN_ID"
echo "Exact trigger command:"
spark_ab_print_trigger "$DAG_ID" "$DAG_RUN_ID" "$CONF"
if [[ $EXECUTE == true ]]; then
  spark_ab_require_clean_commit
fi
spark_ab_execute_or_dry_run "$DAG_ID" "$DAG_RUN_ID" "$CONF"
