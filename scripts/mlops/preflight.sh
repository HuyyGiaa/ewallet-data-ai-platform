#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

parse_execute_flag "$@"
assert_canonical_environment
assert_pinned_image "${F9_RUNTIME_IMAGE}"
assert_pinned_image "${MLFLOW_IMAGE}"
assert_pipeline_binding

require_env GKE_CLUSTER
require_env GKE_ZONE
require_env GKE_NODE_POOL
require_env KFP_VERSION
require_env KFP_NAMESPACE
require_env MLFLOW_ARTIFACT_ROOT
require_env TRAINING_GSA

[[ "${KFP_VERSION}" == "${EXPECTED_KFP_VERSION}" ]] || die "KFP_VERSION must remain 2.17.3"
[[ "${KFP_NAMESPACE}" == "kubeflow" ]] || die "KFP_NAMESPACE must remain kubeflow"
[[ "${MLFLOW_ARTIFACT_ROOT}" == gs://* ]] || die "MLFLOW_ARTIFACT_ROOT must be a gs:// URI"
[[ "${TRAINING_GSA}" == "ewallet-training@${GCP_PROJECT_ID}.iam.gserviceaccount.com" ]] || \
  die "TRAINING_GSA must be the canonical training service account"
require_gke_auth_plugin

echo "Local contract checks: PASS"
echo "Compiled pipeline SHA-256: ${EXPECTED_PIPELINE_SHA256}"
echo "F9 runtime image: ${F9_RUNTIME_IMAGE}"
echo "MLflow image: ${MLFLOW_IMAGE}"
echo "KFP manifests: ${KFP_CLUSTER_SCOPED_SOURCE} and ${KFP_DEV_SOURCE}"

if ((!EXECUTE)); then
  echo "DRY RUN: cloud read-only checks were not executed. Re-run with --execute."
  print_command gcloud projects describe "${GCP_PROJECT_ID}"
  print_gke_dns_contract_checks
  print_command gcloud container node-pools describe "${GKE_NODE_POOL}" --cluster "${GKE_CLUSTER}" --zone "${GKE_ZONE}" --project "${GCP_PROJECT_ID}"
  print_command kubectl config view --minify -o 'jsonpath={.clusters[0].cluster.server}'
  print_command kubectl auth can-i get pods --all-namespaces
  exit 0
fi

for command in git docker gcloud kubectl curl; do require_command "${command}"; done
git -C "${REPO_ROOT}" diff --quiet || die "repository has unstaged tracked changes"
git -C "${REPO_ROOT}" diff --cached --quiet || die "repository has staged changes"
gcloud projects describe "${GCP_PROJECT_ID}" --format='value(projectId)' >/dev/null
load_gke_dns_contract
assert_gke_dns_contract
assert_active_gke_dns_server
gcloud container node-pools describe "${GKE_NODE_POOL}" --cluster "${GKE_CLUSTER}" --zone "${GKE_ZONE}" --project "${GCP_PROJECT_ID}" --format='value(name)' >/dev/null
kubectl auth can-i get pods --all-namespaces >/dev/null
echo "Cloud read-only preflight: PASS"
