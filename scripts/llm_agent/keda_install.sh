#!/usr/bin/env bash
# Install the pinned KEDA chart. Dry-run by default: it verifies the chart digest and
# renders the manifests, and changes nothing without --execute.
#   KUBE_CONTEXT=<context> scripts/llm_agent/keda_install.sh [--execute]
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
# shellcheck source=../../infra/helm/keda/versions.env
. "$root/infra/helm/keda/versions.env"
execute=0
[[ "${1:-}" == --execute ]] && execute=1
: "${KUBE_CONTEXT:?KUBE_CONTEXT is required; the kubeconfig default is never used}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
helm pull keda --repo "$KEDA_CHART_REPO" --version "$KEDA_VERSION" --destination "$work"
actual="$(sha256sum "$work/keda-$KEDA_VERSION.tgz" | cut -d' ' -f1)"
[[ "$actual" == "$KEDA_CHART_TGZ_SHA256" ]] || { echo "chart digest mismatch: $actual" >&2; exit 1; }
if kubectl --context "$KUBE_CONTEXT" get crd scaledobjects.keda.sh >/dev/null 2>&1; then
  echo "KEDA CRDs already exist; refusing a second install" >&2
  exit 1
fi
if ((!execute)); then
  helm template keda "$work/keda-$KEDA_VERSION.tgz" --namespace keda -f "$root/infra/helm/keda/values.yaml" > "$work/rendered.yaml"
  grep -c '^kind:' "$work/rendered.yaml" | sed 's/^/DRY RUN: rendered objects: /'
  exit 0
fi
helm upgrade --install keda "$work/keda-$KEDA_VERSION.tgz" --kube-context "$KUBE_CONTEXT" \
  --namespace keda --create-namespace -f "$root/infra/helm/keda/values.yaml" --wait --timeout 5m
kubectl --context "$KUBE_CONTEXT" -n keda get deploy
