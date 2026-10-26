#!/usr/bin/env bash

set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

component="${1:-}"
[[ -n "${component}" ]] || die "usage: $0 <f9|mlflow> [--execute]"
shift
parse_execute_flag "$@"
assert_canonical_environment
require_command docker
require_command gcloud

case "${component}" in
  f9) assert_pipeline_binding; image="${F9_RUNTIME_IMAGE}" ;;
  mlflow) image="${MLFLOW_IMAGE}" ;;
  *) die "unsupported image component: ${component}" ;;
esac

assert_pinned_image "${image}"
registry_host="$(registry_host_for "${image}")"

if ((!EXECUTE)); then
  echo "DRY RUN: authenticate to ${registry_host} with a short-lived gcloud token"
  echo "  gcloud auth print-access-token | docker login --username oauth2accesstoken --password-stdin ${registry_host}"
  print_command docker push "${image}"
  exit 0
fi

docker image inspect "${image}" >/dev/null
gcloud auth print-access-token | docker login --username oauth2accesstoken --password-stdin "${registry_host}"
docker push "${image}"
