#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

direction="${1:-}"
[[ "${direction}" == "up" || "${direction}" == "down" ]] || die "usage: $0 <up|down> [--execute]"
shift
parse_execute_flag "$@"
require_env GCP_PROJECT_ID
require_env GKE_CLUSTER
require_env GKE_ZONE
require_env GKE_NODE_POOL
[[ "${GCP_PROJECT_ID}" == "${EXPECTED_PROJECT_ID}" ]] || die "unexpected project"
require_command gcloud

nodes=0
[[ "${direction}" == "up" ]] && nodes=1
echo "$([[ ${EXECUTE} -eq 1 ]] && echo EXECUTE || echo 'DRY RUN'): set ${GKE_NODE_POOL} to ${nodes} node(s)"
run_or_plan gcloud container clusters resize "${GKE_CLUSTER}" --node-pool "${GKE_NODE_POOL}" --num-nodes "${nodes}" --zone "${GKE_ZONE}" --project "${GCP_PROJECT_ID}" --quiet
