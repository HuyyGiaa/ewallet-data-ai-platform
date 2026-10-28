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
readonly -a KFP_INVERSE_PROXY_RESOURCES=(
  "serviceaccount/proxy-agent-runner"
  "role.rbac.authorization.k8s.io/proxy-agent-runner"
  "rolebinding.rbac.authorization.k8s.io/proxy-agent-runner"
  "configmap/inverse-proxy-config"
  "deployment.apps/proxy-agent"
)
readonly GKE_DNS_ENDPOINT_FIELD="controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint"
readonly GKE_DNS_EXTERNAL_TRAFFIC_FIELD="controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic"
readonly GKE_DNS_TOKENS_FIELD="controlPlaneEndpointsConfig.dnsEndpointConfig.enableK8sTokensViaDns"
readonly GKE_DNS_CERTS_FIELD="controlPlaneEndpointsConfig.dnsEndpointConfig.enableK8sCertsViaDns"

EXECUTE=0

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_gke_auth_plugin() {
  command -v gke-gcloud-auth-plugin >/dev/null 2>&1 || \
    die "gke-gcloud-auth-plugin is required. Debian/Ubuntu: sudo apt-get install google-cloud-cli-gke-gcloud-auth-plugin"
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

verify_kfp_inverse_proxy_absent() {
  local resource found
  local -a remaining=()
  local -a verification_errors=()
  for resource in "${KFP_INVERSE_PROXY_RESOURCES[@]}"; do
    if ((!EXECUTE)); then
      print_command kubectl -n kubeflow get "${resource}" --ignore-not-found -o name
      echo "  assert ${resource} is absent from the canonical KFP control plane"
      continue
    fi

    if ! found="$(kubectl -n kubeflow get "${resource}" --ignore-not-found -o name 2>&1)"; then
      verification_errors+=("${resource}: ${found}")
      continue
    fi
    [[ -z "${found}" ]] || remaining+=("${resource}")
  done

  ((${#verification_errors[@]} == 0)) || \
    die "failed to verify inverse-proxy resource absence: ${verification_errors[*]}"
  ((${#remaining[@]} == 0)) || \
    die "inverse-proxy resources remain after bounded deletion: ${remaining[*]}"
}

gke_cluster_value() {
  local field="$1"
  gcloud container clusters describe "${GKE_CLUSTER}" \
    --zone "${GKE_ZONE}" \
    --project "${GCP_PROJECT_ID}" \
    --format="value(${field})"
}

print_gke_dns_contract_checks() {
  local field
  for field in \
    status \
    "${GKE_DNS_ENDPOINT_FIELD}" \
    "${GKE_DNS_EXTERNAL_TRAFFIC_FIELD}" \
    "${GKE_DNS_TOKENS_FIELD}" \
    "${GKE_DNS_CERTS_FIELD}"; do
    print_command gcloud container clusters describe "${GKE_CLUSTER}" \
      --zone "${GKE_ZONE}" \
      --project "${GCP_PROJECT_ID}" \
      --format="value(${field})"
  done
  echo "  assert cluster status=RUNNING, DNS endpoint enabled, external DNS traffic allowed"
  echo "  assert Kubernetes tokens and client certificates over DNS remain disabled"
}

load_gke_dns_contract() {
  GKE_CLUSTER_STATUS="$(gke_cluster_value status)"
  GKE_DNS_ENDPOINT="$(gke_cluster_value "${GKE_DNS_ENDPOINT_FIELD}")"
  GKE_DNS_ALLOW_EXTERNAL="$(gke_cluster_value "${GKE_DNS_EXTERNAL_TRAFFIC_FIELD}")"
  GKE_DNS_TOKENS_ENABLED="$(gke_cluster_value "${GKE_DNS_TOKENS_FIELD}")"
  GKE_DNS_CERTS_ENABLED="$(gke_cluster_value "${GKE_DNS_CERTS_FIELD}")"
}

assert_gke_dns_contract() {
  local remediation
  remediation="gcloud container clusters update ${GKE_CLUSTER} --zone ${GKE_ZONE} --project ${GCP_PROJECT_ID} --enable-dns-access"

  [[ "${GKE_CLUSTER_STATUS}" == "RUNNING" ]] || \
    die "GKE cluster must be RUNNING; found ${GKE_CLUSTER_STATUS:-<missing>}"
  [[ -n "${GKE_DNS_ENDPOINT}" && "${GKE_DNS_ENDPOINT}" == *.gke.goog ]] || \
    die "GKE DNS endpoint is not enabled. Remediate explicitly with: ${remediation}"
  [[ "${GKE_DNS_ALLOW_EXTERNAL,,}" == "true" ]] || \
    die "GKE DNS endpoint does not allow external traffic. Remediate explicitly with: ${remediation}"
  [[ "${GKE_DNS_TOKENS_ENABLED,,}" != "true" ]] || \
    die "Kubernetes ServiceAccount tokens over DNS must remain disabled"
  [[ "${GKE_DNS_CERTS_ENABLED,,}" != "true" ]] || \
    die "Kubernetes client certificates over DNS must remain disabled"
}

assert_active_gke_dns_server() {
  local active_server expected_server
  active_server="$(kubectl config view --minify -o 'jsonpath={.clusters[0].cluster.server}')"
  expected_server="https://${GKE_DNS_ENDPOINT}"
  [[ "${active_server}" == "${expected_server}" ]] || \
    die "active kubeconfig server does not match the canonical cluster DNS endpoint"
  [[ "${active_server}" == https://*.gke.goog ]] || \
    die "active kubeconfig server must end in .gke.goog"
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
