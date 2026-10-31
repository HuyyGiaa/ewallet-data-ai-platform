#!/usr/bin/env bash
# Istio ambient on GKE (infra/kubernetes/istio-ambient), plan or apply.
# usage: istio_ambient_gke.sh <plan|apply> <evidence-dir>
# Env: KUBE_CONTEXT (required, a gke_* context, never the kubeconfig default);
# ISTIO_CHART_DIR (optional: pre-downloaded <chart>-<version>.tgz files).
# Every chart is checked against its sha256 in versions.env before use.
# plan renders the four charts; apply installs base -> istiod -> cni -> ztunnel
# (same release names as the kind mesh) and reads back the CNI bin dir.
set -euo pipefail
mode=$1 evidence=$2
[[ "$mode" =~ ^(plan|apply)$ ]] || { echo "mode must be plan or apply" >&2; exit 2; }
[ -n "${KUBE_CONTEXT:-}" ] || { echo "KUBE_CONTEXT is required" >&2; exit 2; }
[[ "$KUBE_CONTEXT" == gke_* ]] || { echo "KUBE_CONTEXT must be a GKE context" >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
profile="$root/infra/kubernetes/istio-ambient"
# shellcheck source=/dev/null
source "$profile/versions.env"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$evidence"

declare -A releases=([base]=istio-base [istiod]=istiod [cni]=istio-cni [ztunnel]=ztunnel)
order=(base istiod cni ztunnel)
for chart in "${order[@]}"; do
  file="$chart-$ISTIO_VERSION.tgz"
  if [ -n "${ISTIO_CHART_DIR:-}" ]; then
    cp "$ISTIO_CHART_DIR/$file" "$work/$file"
  else
    curl -fsSL -o "$work/$file" "$ISTIO_CHART_REPO/$file"
  fi
  digest_var="ISTIO_${chart^^}_DIGEST"
  expected=${!digest_var#sha256:}
  actual=$(sha256sum "$work/$file" | cut -d' ' -f1)
  [ "$actual" = "$expected" ] || { echo "$file sha256 $actual != $expected" >&2; exit 1; }
done

for chart in "${order[@]}"; do
  helm template "${releases[$chart]}" "$work/$chart-$ISTIO_VERSION.tgz" --namespace istio-system \
    -f "$profile/values-gke.yaml" > "$evidence/istio-$chart.yaml"
done
[ "$mode" = plan ] && exit 0

for chart in "${order[@]}"; do
  helm upgrade --install "${releases[$chart]}" "$work/$chart-$ISTIO_VERSION.tgz" --kube-context "$KUBE_CONTEXT" \
    --namespace istio-system --create-namespace -f "$profile/values-gke.yaml" --wait --timeout 10m \
    | tee -a "$evidence/helm-install.txt"
done
kubectl --context "$KUBE_CONTEXT" -n istio-system get ds istio-cni-node \
  -o jsonpath='{.spec.template.spec.volumes[?(@.name=="cni-bin-dir")].hostPath.path}' > "$evidence/cni-bin-dir.txt"
kubectl --context "$KUBE_CONTEXT" -n istio-system get ds,deploy,pods,resourcequota -o wide > "$evidence/istio-system.txt"
