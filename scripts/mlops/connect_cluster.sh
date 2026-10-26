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

echo "$([[ ${EXECUTE} -eq 1 ]] && echo EXECUTE || echo 'DRY RUN'): configure the bounded cluster context"
run_or_plan gcloud container clusters get-credentials "${GKE_CLUSTER}" --zone "${GKE_ZONE}" --project "${GCP_PROJECT_ID}"
if ((EXECUTE)); then
  kubectl config current-context
fi
