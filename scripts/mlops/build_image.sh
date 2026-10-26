#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

component="${1:-}"
[[ -n "${component}" ]] || die "usage: $0 <f9|mlflow> [--execute]"
shift
parse_execute_flag "$@"
assert_canonical_environment
require_command docker

case "${component}" in
  f9)
    assert_pipeline_binding
    image="${F9_RUNTIME_IMAGE}"
    dockerfile="${REPO_ROOT}/infra/kubernetes/kfp/image/Dockerfile"
    context="${REPO_ROOT}"
    ;;
  mlflow)
    image="${MLFLOW_IMAGE}"
    dockerfile="${REPO_ROOT}/infra/kubernetes/mlflow/image/Dockerfile"
    context="${REPO_ROOT}/infra/kubernetes/mlflow/image"
    ;;
  *) die "unsupported image component: ${component}" ;;
esac

assert_pinned_image "${image}"
echo "$([[ ${EXECUTE} -eq 1 ]] && echo EXECUTE || echo 'DRY RUN'): build ${component} image"
run_or_plan docker build --pull=false --file "${dockerfile}" --tag "${image}" "${context}"
