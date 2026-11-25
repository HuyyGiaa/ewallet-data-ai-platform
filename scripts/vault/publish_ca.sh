#!/usr/bin/env bash
# Publish Vault's CA certificate (public) for ClusterSecretStore ewallet-vault (Vault V2).
# usage: publish_ca.sh <context> [extra-label key=value]
#
# Source: Secret ewallet-vault/vault-ca, key ca.crt only (the CA's private key is never read).
# Target: ConfigMap external-secrets/vault-ca, the one CA the store trusts (caProvider namespace).
# - absent                     -> created with label ewallet.io/vault-ca=published   prints CA_PUBLISHED
# - present, ours, same cert   -> left alone                                         prints CA_UNCHANGED
# - present, ours, other cert  -> replaced (CA renewal)                              prints CA_UPDATED
# - present without our label  -> REFUSED, nothing changed (never overwrite an unrelated ConfigMap)
set -euo pipefail
context=${1:?context} extra=${2:-}
k() { kubectl --context "$context" "$@"; }
refuse() { echo "REFUSED: $1" >&2; exit 2; }

ca=$(k -n ewallet-vault get secret vault-ca -o jsonpath='{.data.ca\.crt}' | base64 -d) || refuse "cannot read ewallet-vault/vault-ca ca.crt"
printf '%s\n' "$ca" | openssl x509 -noout 2>/dev/null || refuse "ewallet-vault/vault-ca ca.crt is not a certificate"
labels=(ewallet.io/vault-ca=published app.kubernetes.io/part-of=ewallet-platform)
[ -z "$extra" ] || { [[ $extra =~ ^[a-z0-9./-]+=[A-Za-z0-9._-]+$ ]] || refuse "bad extra label"; labels+=("$extra"); }
render() {
  k -n external-secrets create configmap vault-ca --from-file=ca.crt=/dev/stdin --dry-run=client -o json <<<"$ca" \
    | jq --arg l "$(printf '%s\n' "${labels[@]}")" '.metadata.labels = ($l | split("\n") | map(select(. != "") | split("=") | {(.[0]): .[1]}) | add)'
}
current=$(k -n external-secrets get configmap vault-ca -o json --ignore-not-found) || refuse "ConfigMap query failed"
if [ -z "$current" ]; then
  render | k create -f - >/dev/null
  echo CA_PUBLISHED; exit 0
fi
[ "$(jq -r '.metadata.labels["ewallet.io/vault-ca"] // ""' <<<"$current")" = published ] \
  || refuse "external-secrets/vault-ca exists without ewallet.io/vault-ca=published: not ours, nothing changed"
if [ "$(jq -r '.data["ca.crt"] // ""' <<<"$current")" = "$(printf '%s\n' "$ca")" ]; then echo CA_UNCHANGED; exit 0; fi
render | k replace -f - >/dev/null
echo CA_UPDATED
