#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

parse_execute_flag "$@"
require_command kubectl
require_command gcloud
require_env MLFLOW_DB_USERNAME
require_env GCP_PROJECT_ID
require_env TRAINING_GSA
[[ "${GCP_PROJECT_ID}" == "${EXPECTED_PROJECT_ID}" ]] || die "unexpected project"
[[ "${TRAINING_GSA}" == "ewallet-training@${GCP_PROJECT_ID}.iam.gserviceaccount.com" ]] || \
  die "unexpected training GSA"

verify_workload_identity() {
  local namespace="$1"
  local ksa="$2"
  local member="serviceAccount:${GCP_PROJECT_ID}.svc.id.goog[${namespace}/${ksa}]"

  if ((!EXECUTE)); then
    print_command kubectl -n "${namespace}" get serviceaccount "${ksa}" \
      -o "jsonpath={.metadata.annotations.iam\\.gke\\.io/gcp-service-account}"
    print_command gcloud iam service-accounts get-iam-policy "${TRAINING_GSA}" \
      --project "${GCP_PROJECT_ID}" --flatten="bindings[].members" \
      --filter="bindings.role=roles/iam.workloadIdentityUser" \
      --format="value(bindings.members)"
    echo "  assert KSA ${namespace}/${ksa} annotation=${TRAINING_GSA}"
    echo "  assert GSA binding member=${member}"
    return
  fi

  local annotation policy_members
  annotation="$(kubectl -n "${namespace}" get serviceaccount "${ksa}" \
    -o 'jsonpath={.metadata.annotations.iam\.gke\.io/gcp-service-account}')"
  [[ "${annotation}" == "${TRAINING_GSA}" ]] || \
    die "KSA ${namespace}/${ksa} has unexpected GSA annotation: ${annotation:-<missing>}"

  policy_members="$(gcloud iam service-accounts get-iam-policy "${TRAINING_GSA}" \
    --project "${GCP_PROJECT_ID}" --flatten="bindings[].members" \
    --filter="bindings.role=roles/iam.workloadIdentityUser" \
    --format="value(bindings.members)")"
  grep -Fqx "${member}" <<<"${policy_members}" || \
    die "training GSA is missing Workload Identity binding for ${member}"
}

echo "$([[ ${EXECUTE} -eq 1 ]] && echo EXECUTE || echo 'DRY RUN'): validate bounded MLOps rollouts"
run_or_plan kubectl -n ml-platform rollout status statefulset/mlflow-postgresql --timeout=10m
run_or_plan kubectl -n ml-platform rollout status deployment/mlflow --timeout=10m
run_or_plan kubectl -n kubeflow wait --for=condition=Available deployment --all --timeout=20m
run_or_plan kubectl -n ml-platform exec statefulset/mlflow-postgresql -- pg_isready -U "${MLFLOW_DB_USERNAME}" -d mlflow
run_or_plan kubectl -n ml-platform get pods,service,statefulset,deployment,pvc -o wide
run_or_plan kubectl -n kubeflow get pods,service,deployment -o wide
verify_workload_identity ml-platform training
verify_workload_identity kubeflow pipeline-runner
