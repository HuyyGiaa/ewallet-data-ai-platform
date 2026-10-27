#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

parse_execute_flag "$@"
require_env GCP_PROJECT_ID
require_env GKE_CLUSTER
require_env GKE_ZONE
[[ "${GCP_PROJECT_ID}" == "${EXPECTED_PROJECT_ID}" ]] || die "unexpected project"
require_command gcloud
require_command kubectl
require_gke_auth_plugin

echo "$([[ ${EXECUTE} -eq 1 ]] && echo EXECUTE || echo 'DRY RUN'): configure the bounded cluster DNS context"
if ((!EXECUTE)); then
  print_gke_dns_contract_checks
  print_command gcloud container clusters get-credentials "${GKE_CLUSTER}" \
    --zone "${GKE_ZONE}" \
    --project "${GCP_PROJECT_ID}" \
    --dns-endpoint
  print_command kubectl config current-context
  print_command kubectl config view --minify -o 'jsonpath={.clusters[0].cluster.server}'
  exit 0
fi

load_gke_dns_contract
assert_gke_dns_contract
gcloud container clusters get-credentials "${GKE_CLUSTER}" \
  --zone "${GKE_ZONE}" \
  --project "${GCP_PROJECT_ID}" \
  --dns-endpoint
assert_active_gke_dns_server
kubectl config current-context
