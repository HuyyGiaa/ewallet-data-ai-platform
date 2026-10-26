#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

parse_execute_flag "$@"
assert_canonical_environment
require_command kubectl
require_command git
require_command sha256sum
require_command gcloud
require_env TRAINING_GSA

evidence_root="${MLOPS_EVIDENCE_DIR:-${REPO_ROOT}/tmp/mlops-gke-evidence/$(date -u +%Y%m%d-%H%M%S)}"
if ((!EXECUTE)); then
  echo "DRY RUN: collect read-only evidence under ${evidence_root}"
  echo "  cluster resources, image digests, Workload Identity, bounded logs, Git SHA and pipeline SHA-256"
  exit 0
fi

mkdir -p "${evidence_root}"
git -C "${REPO_ROOT}" show -s --format=fuller HEAD >"${evidence_root}/git-head.txt"
sha256sum "${PIPELINE_PACKAGE}" >"${evidence_root}/pipeline-sha256.txt"
gcloud artifacts docker images describe "${F9_RUNTIME_IMAGE}" --format=json >"${evidence_root}/f9-image.json"
gcloud artifacts docker images describe "${MLFLOW_IMAGE}" --format=json >"${evidence_root}/mlflow-image.json"
gcloud iam service-accounts get-iam-policy "${TRAINING_GSA}" --project "${GCP_PROJECT_ID}" --format=json >"${evidence_root}/training-gsa-policy.json"
kubectl -n ml-platform get pods,service,statefulset,deployment,pvc -o wide >"${evidence_root}/mlflow-resources.txt"
kubectl -n kubeflow get pods,service,deployment -o wide >"${evidence_root}/kfp-resources.txt"
kubectl -n ml-platform get serviceaccount training -o yaml >"${evidence_root}/mlflow-ksa.yaml"
kubectl -n kubeflow get serviceaccount pipeline-runner -o yaml >"${evidence_root}/kfp-runner-ksa.yaml"
kubectl -n ml-platform describe statefulset/mlflow-postgresql >"${evidence_root}/postgresql-describe.txt"
kubectl -n ml-platform describe deployment/mlflow >"${evidence_root}/mlflow-describe.txt"
kubectl -n ml-platform logs deployment/mlflow --tail=200 >"${evidence_root}/mlflow.log"
kubectl -n ml-platform logs statefulset/mlflow-postgresql --tail=200 >"${evidence_root}/postgresql.log"
for pod in $(kubectl -n kubeflow get pods -o name); do
  kubectl -n kubeflow logs "${pod}" --all-containers=true --tail=100 \
    >"${evidence_root}/kfp-${pod##*/}.log" 2>&1 || true
done
echo "Evidence written to ${evidence_root}"
