#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

target="${1:-}"
[[ "${target}" == "mlflow" || "${target}" == "kfp" || "${target}" == "all" ]] || \
  die "usage: $0 <mlflow|kfp|all> [--delete-postgres-data] [--execute]"
shift
delete_postgres_data=0
execute_args=()
while (($#)); do
  case "$1" in
    --delete-postgres-data) delete_postgres_data=1 ;;
    --execute) execute_args+=("$1") ;;
    *) die "unsupported argument: $1" ;;
  esac
  shift
done
parse_execute_flag "${execute_args[@]}"
require_command kubectl
if ((EXECUTE)) && [[ "${target}" == "kfp" || "${target}" == "all" ]]; then
  [[ "${ALLOW_KFP_CONTROL_PLANE_DELETE:-}" == "YES" ]] || \
    die "set ALLOW_KFP_CONTROL_PLANE_DELETE=YES after confirming kubeflow is dedicated"
fi

cleanup_mlflow() {
  run_or_plan kubectl delete -f "${REPO_ROOT}/infra/kubernetes/mlflow/deployment.yaml" --ignore-not-found
  run_or_plan kubectl delete -f "${REPO_ROOT}/infra/kubernetes/mlflow/service.yaml" --ignore-not-found
  run_or_plan kubectl delete -f "${REPO_ROOT}/infra/kubernetes/mlflow/postgresql.yaml" --ignore-not-found
  run_or_plan kubectl -n ml-platform delete configmap mlflow-runtime --ignore-not-found
  run_or_plan kubectl -n ml-platform delete secret mlflow-backend-store mlflow-postgresql --ignore-not-found
  run_or_plan kubectl delete -f "${REPO_ROOT}/infra/kubernetes/mlflow/service-account.yaml" --ignore-not-found
  if ((delete_postgres_data)); then
    run_or_plan kubectl -n ml-platform delete pvc data-mlflow-postgresql-0 --ignore-not-found
  else
    echo "PostgreSQL PVC is preserved. Use --delete-postgres-data only after evidence review."
  fi
}

cleanup_kfp() {
  run_or_plan kubectl delete -f "${REPO_ROOT}/infra/kubernetes/kfp/pipeline-runner-service-account.yaml" --ignore-not-found
  run_or_plan kubectl delete -k "${KFP_DEV_SOURCE}" --ignore-not-found
  run_or_plan kubectl delete -k "${KFP_CLUSTER_SCOPED_SOURCE}" --ignore-not-found
}

echo "$([[ ${EXECUTE} -eq 1 ]] && echo EXECUTE || echo 'DRY RUN'): bounded cleanup target=${target}"
[[ "${target}" == "mlflow" || "${target}" == "all" ]] && cleanup_mlflow
[[ "${target}" == "kfp" || "${target}" == "all" ]] && cleanup_kfp
echo "Protected by design: GSA/IAM policy, GKE cluster, GCS, Artifact Registry, canonical datasets and DP outputs are untouched."
