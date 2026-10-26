#!/usr/bin/env bash

set -Eeuo pipefail

MLOPS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
REPO_ROOT="$(cd "${MLOPS_SCRIPT_DIR}/../.." && pwd -P)"

readonly EXPECTED_PROJECT_ID="ewallet-data-ai-platform-hg26"
readonly EXPECTED_REGION="asia-southeast1"
readonly EXPECTED_F9_RUNTIME_IMAGE="asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/f9-kfp-runtime:kfp-2.17.3-f9-v1"
readonly EXPECTED_MLFLOW_IMAGE="asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/mlflow:v3.17.1-gcs-postgres-1"
readonly EXPECTED_PIPELINE_SHA256="4f8c039f76123519828702aea07765e5d882c453d98aab0aa408bd2dfebcf4b4"
readonly EXPECTED_KFP_VERSION="2.17.3"
readonly PIPELINE_PACKAGE="${REPO_ROOT}/ai_platform/ml/pipelines/compiled/f9_canonical_pipeline.yaml"
readonly KFP_CLUSTER_SCOPED_SOURCE="github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=2.17.3"
readonly KFP_DEV_SOURCE="github.com/kubeflow/pipelines/manifests/kustomize/env/dev?ref=2.17.3"

EXECUTE=0

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_env() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "required environment variable is unset: ${name}"
}

parse_execute_flag() {
  EXECUTE=0
  while (($#)); do
    case "$1" in
      --execute) EXECUTE=1 ;;
      *) die "unsupported argument: $1" ;;
    esac
    shift
  done
}

print_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

run_or_plan() {
  if ((EXECUTE)); then
    "$@"
  else
    print_command "$@"
  fi
}

assert_canonical_environment() {
  require_env GCP_PROJECT_ID
  require_env GCP_REGION
  require_env F9_RUNTIME_IMAGE
  require_env MLFLOW_IMAGE
  [[ "${GCP_PROJECT_ID}" == "${EXPECTED_PROJECT_ID}" ]] || \
    die "GCP_PROJECT_ID must be ${EXPECTED_PROJECT_ID}"
  [[ "${GCP_REGION}" == "${EXPECTED_REGION}" ]] || \
    die "GCP_REGION must be ${EXPECTED_REGION}"
  [[ "${F9_RUNTIME_IMAGE}" == "${EXPECTED_F9_RUNTIME_IMAGE}" ]] || \
    die "F9_RUNTIME_IMAGE differs from the compiled canonical image"
  [[ "${MLFLOW_IMAGE}" == "${EXPECTED_MLFLOW_IMAGE}" ]] || \
    die "MLFLOW_IMAGE differs from the bounded canonical image"
}

assert_pinned_image() {
  local image="$1"
  [[ "${image}" != *":latest" ]] || die "latest image tags are forbidden: ${image}"
  [[ "${image}" == *@sha256:* || "${image##*/}" == *:* ]] || \
    die "image must use an explicit tag or digest: ${image}"
}

assert_pipeline_binding() {
  require_command sha256sum
  require_command awk
  require_command sort

  [[ -f "${PIPELINE_PACKAGE}" ]] || die "compiled KFP package is missing"
  local actual_sha
  actual_sha="$(sha256sum "${PIPELINE_PACKAGE}" | awk '{print $1}')"
  [[ "${actual_sha}" == "${EXPECTED_PIPELINE_SHA256}" ]] || \
    die "compiled KFP package SHA-256 mismatch: ${actual_sha}"

  local image_count unique_count unique_image
  image_count="$(awk '$1 == "image:" {count++} END {print count+0}' "${PIPELINE_PACKAGE}")"
  unique_count="$(awk '$1 == "image:" {print $2}' "${PIPELINE_PACKAGE}" | sort -u | awk 'END {print NR+0}')"
  unique_image="$(awk '$1 == "image:" {print $2}' "${PIPELINE_PACKAGE}" | sort -u)"
  [[ "${image_count}" -gt 0 && "${unique_count}" -eq 1 ]] || \
    die "compiled KFP package must bind one runtime image"
  [[ "${unique_image}" == "${EXPECTED_F9_RUNTIME_IMAGE}" ]] || \
    die "compiled KFP runtime image mismatch: ${unique_image}"
}

registry_host_for() {
  local image="$1"
  printf '%s\n' "${image%%/*}"
}
