#!/usr/bin/env bash
# kind only: install cert-manager v1.16.2 (the version on GKE ewallet-dev) for a Vault rehearsal, and remove it after.
# usage: kind_cert_manager.sh <install|remove> <kind-context>
# install refuses if cert-manager or its CRDs already exist; remove deletes only what install created
# (namespace label ewallet.io/vault-rehearsal-cert-manager=installed), never a cert-manager it did not install.
set -euo pipefail
action=${1:?install|remove} ctx=${2:?kind context}
[[ $ctx == kind-* ]] || { echo "REFUSED: $ctx is not a kind context" >&2; exit 2; }
k() { kubectl --context "$ctx" "$@"; }
h() { helm --kube-context "$ctx" "$@"; }
MARK=ewallet.io/vault-rehearsal-cert-manager
case "$action" in
  install)
    if k get ns cert-manager >/dev/null 2>&1 || [ -n "$(k get crd -o name | grep '\.cert-manager\.io$' || true)" ]; then
      echo "REFUSED: cert-manager already present; use it or remove it yourself" >&2; exit 2
    fi
    k create namespace cert-manager >/dev/null
    k label namespace cert-manager "$MARK=installed" >/dev/null
    h install cert-manager cert-manager --repo https://charts.jetstack.io --version v1.16.2 -n cert-manager \
      --set crds.enabled=true --wait --timeout 10m >/dev/null
    echo CERT_MANAGER_INSTALLED ;;
  remove)
    [ "$(k get ns cert-manager -o jsonpath="{.metadata.labels.ewallet\.io/vault-rehearsal-cert-manager}" 2>/dev/null)" = installed ] \
      || { echo "REFUSED: cert-manager was not installed by this helper; nothing removed" >&2; exit 2; }
    h uninstall cert-manager -n cert-manager --wait --timeout 5m >/dev/null
    k delete crd $(k get crd -o name | grep '\.cert-manager\.io$' | sed 's#.*/##') --wait=true >/dev/null
    k delete namespace cert-manager --wait=true --timeout=300s >/dev/null
    echo CERT_MANAGER_REMOVED ;;
  *) echo "REFUSED: unknown action $action" >&2; exit 2 ;;
esac
