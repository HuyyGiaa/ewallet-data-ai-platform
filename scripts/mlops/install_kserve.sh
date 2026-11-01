#!/usr/bin/env bash
# Install cert-manager and KServe 0.16.0 in Standard mode (no Knative, no
# Istio, no ingress). Dry-run by default; --execute applies with the explicit
# GKE context. Existing Helm releases owned by other workstreams are refused.
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
parse_execute_flag "$@"

readonly CERT_MANAGER_VERSION="v1.16.2"
readonly KSERVE_VERSION="v0.16.0"
KUBE_CONTEXT="${KUBE_CONTEXT:-gke_${EXPECTED_PROJECT_ID}_asia-southeast1-b_ewallet-dev}"
HELM=(helm --kube-context "${KUBE_CONTEXT}")

for release in cert-manager:cert-manager kserve-crd:kserve kserve:kserve; do
  name="${release%%:*}"; namespace="${release##*:}"
  owner="$("${HELM[@]}" list -n "${namespace}" -f "^${name}\$" -o json 2>/dev/null || echo '[]')"
  if [[ "${owner}" != "[]" ]] && ! grep -q '"app_version"' <<<"${owner}"; then
    die "unexpected Helm state for ${name} in ${namespace}: ${owner}"
  fi
done

run_or_plan "${HELM[@]}" upgrade --install cert-manager cert-manager \
  --repo https://charts.jetstack.io --version "${CERT_MANAGER_VERSION}" \
  --namespace cert-manager --create-namespace --wait --timeout 10m \
  --set crds.enabled=true \
  --set resources.requests.cpu=20m --set resources.requests.memory=64Mi \
  --set webhook.resources.requests.cpu=10m --set webhook.resources.requests.memory=32Mi \
  --set cainjector.resources.requests.cpu=20m --set cainjector.resources.requests.memory=64Mi

run_or_plan "${HELM[@]}" upgrade --install kserve-crd oci://ghcr.io/kserve/charts/kserve-crd \
  --version "${KSERVE_VERSION}" --namespace kserve --create-namespace --wait --timeout 10m

run_or_plan "${HELM[@]}" upgrade --install kserve oci://ghcr.io/kserve/charts/kserve \
  --version "${KSERVE_VERSION}" --namespace kserve --wait --timeout 10m \
  --set kserve.controller.deploymentMode=Standard \
  --set kserve.controller.gateway.disableIngressCreation=true \
  --set kserve.controller.gateway.ingressGateway.className=none

if ((EXECUTE)); then
  kubectl --context "${KUBE_CONTEXT}" -n kserve rollout status deploy/kserve-controller-manager --timeout=5m
  kubectl --context "${KUBE_CONTEXT}" get crd inferenceservices.serving.kserve.io
fi
