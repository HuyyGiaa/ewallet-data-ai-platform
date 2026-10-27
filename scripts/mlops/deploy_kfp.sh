#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

parse_execute_flag "$@"
assert_canonical_environment
assert_pipeline_binding
require_env KFP_VERSION
require_env KFP_NAMESPACE
[[ "${KFP_VERSION}" == "${EXPECTED_KFP_VERSION}" ]] || die "KFP_VERSION must remain 2.17.3"
[[ "${KFP_NAMESPACE}" == "kubeflow" ]] || die "KFP_NAMESPACE must remain kubeflow"
require_command kubectl

canonical_overlay="${REPO_ROOT}/infra/kubernetes/kfp"
echo "$([[ ${EXECUTE} -eq 1 ]] && echo EXECUTE || echo 'DRY RUN'): deploy standalone KFP ${KFP_VERSION}"
run_or_plan kubectl apply -k "${KFP_CLUSTER_SCOPED_SOURCE}"
run_or_plan kubectl wait --for=condition=Established --timeout=180s crd/applications.app.k8s.io
run_or_plan kubectl apply -k "${canonical_overlay}"
run_or_plan kubectl -n kubeflow delete deployment proxy-agent --ignore-not-found
