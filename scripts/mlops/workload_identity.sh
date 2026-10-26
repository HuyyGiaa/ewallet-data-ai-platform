#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

action="${1:-}"
[[ "${action}" == "bind" || "${action}" == "unbind" ]] || die "usage: $0 <bind|unbind> [--execute]"
shift
parse_execute_flag "$@"
require_env GCP_PROJECT_ID
require_env TRAINING_GSA
[[ "${GCP_PROJECT_ID}" == "${EXPECTED_PROJECT_ID}" ]] || die "unexpected project"
[[ "${TRAINING_GSA}" == "ewallet-training@${GCP_PROJECT_ID}.iam.gserviceaccount.com" ]] || die "unexpected training GSA"
require_command gcloud
require_command kubectl

verb="add-iam-policy-binding"
annotation="iam.gke.io/gcp-service-account=${TRAINING_GSA}"
if [[ "${action}" == "unbind" ]]; then
  verb="remove-iam-policy-binding"
  annotation="iam.gke.io/gcp-service-account-"
fi

for identity in "ml-platform:training" "kubeflow:pipeline-runner"; do
  namespace="${identity%%:*}"
  ksa="${identity##*:}"
  member="serviceAccount:${GCP_PROJECT_ID}.svc.id.goog[${namespace}/${ksa}]"
  run_or_plan gcloud iam service-accounts "${verb}" "${TRAINING_GSA}" \
    --role roles/iam.workloadIdentityUser --member "${member}" --project "${GCP_PROJECT_ID}"
  run_or_plan kubectl -n "${namespace}" annotate serviceaccount "${ksa}" "${annotation}" --overwrite
done
