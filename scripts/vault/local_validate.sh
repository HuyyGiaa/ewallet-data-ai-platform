#!/usr/bin/env bash
# Validate the Vault integration end to end on a disposable kind namespace set.
# usage: local_validate.sh <vault-chart.tgz> <evidence-dir>
#
# Uses the official chart, Raft on a PVC, TLS and real projected ServiceAccount tokens.
# This is local evidence only; it says nothing about GKE. Refuses any other context.
# Everything it creates carries ewallet.io/vault-local=true and is removed on exit.
set -euo pipefail
chart=${1:?chart archive required} evidence=${2:?evidence directory required}
CONTEXT=${VAULT_LOCAL_CONTEXT:-kind-llm-agent-local}
[[ "$CONTEXT" == kind-* ]] || { echo "refusing non-kind context $CONTEXT" >&2; exit 2; }
NS=ewallet-vault APP=ml-platform PORT=${VAULT_LOCAL_PORT:-18230} PROBE_IMAGE=python:3.12-alpine
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d); keys=$work/keys.json; forward=
mkdir -p "$evidence"; export PYTHONPATH=$root
k() { kubectl --context "$CONTEXT" "$@"; }
vault() { python3 -m ops.vault.cli --address "https://127.0.0.1:$PORT" --ca-file "$work/ca.crt" "$@"; }
record() { printf '%s\n' "$2" | jq -c --arg step "$1" '{step: $step} + .' | tee -a "$evidence/results.jsonl"; }

cleanup() {
  [ -z "$forward" ] || kill "$forward" 2>/dev/null || true
  helm --kube-context "$CONTEXT" uninstall vault -n "$NS" >/dev/null 2>&1 || true
  for ns in "$NS" "$APP"; do
    [ "$(k get ns "$ns" -o jsonpath='{.metadata.labels.ewallet\.io/vault-local}' 2>/dev/null)" = true ] \
      && k delete ns "$ns" --wait=false >/dev/null
  done
  rm -rf "$work"
}
trap cleanup EXIT

forward_vault() {
  # A refused first connection ends kubectl port-forward, so wait for Ready and reopen.
  k -n "$NS" wait --for=condition=Ready pod/vault-0 --timeout=120s >/dev/null
  for _ in $(seq 10); do
    [ -z "$forward" ] || kill "$forward" 2>/dev/null || true
    k -n "$NS" port-forward pod/vault-0 "$PORT:8200" >/dev/null 2>&1 & forward=$!
    sleep 2
    vault status >/dev/null 2>&1 && return
  done
  echo "Vault did not answer through the port-forward" >&2; return 1
}

probe() {  # <pod-name> <service-account> <audience> <secrets-json> [vault-role]
  k -n "$APP" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: $1, labels: {ewallet.io/vault-local: "true"}}
spec:
  restartPolicy: Never
  serviceAccountName: $2
  automountServiceAccountToken: false
  containers:
    - name: probe
      image: $PROBE_IMAGE
      imagePullPolicy: IfNotPresent
      command: [python3, /probe/workload_probe.py]
      env:
        - {name: EWALLET_VAULT_ADDR, value: "https://vault.$NS.svc.cluster.local:8200"}
        - {name: EWALLET_VAULT_ROLE, value: ${5:-fraud-api}}
        - {name: EWALLET_VAULT_CA_PATH, value: /ca/ca.crt}
        - {name: EWALLET_VAULT_SECRETS, value: '$4'}
      volumeMounts:
        - {name: token, mountPath: /var/run/secrets/vault, readOnly: true}
        - {name: code, mountPath: /probe, readOnly: true}
        - {name: ca, mountPath: /ca, readOnly: true}
  volumes:
    - name: token
      projected: {sources: [{serviceAccountToken: {path: token, audience: $3, expirationSeconds: 600}}]}
    - {name: code, configMap: {name: vault-probe}}
    - {name: ca, configMap: {name: vault-ca}}
YAML
  k -n "$APP" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$1" --timeout=120s >/dev/null
  k -n "$APP" logs "$1" | tail -1
}
SECRETS='{"EWALLET_REDIS_CONNECTION_STRING": "fraud-api/feast-redis#connection_string"}'
OTHER='{"EWALLET_REDIS_CONNECTION_STRING": "fraud-api/other#connection_string"}'
FEATURE='{"EWALLET_REDIS_CONNECTION_STRING": "feature-api/feast-redis#connection_string"}'

for ns in "$NS" "$APP"; do k create ns "$ns" >/dev/null; k label ns "$ns" ewallet.io/vault-local=true >/dev/null; done
k label ns "$NS" pod-security.kubernetes.io/enforce=restricted >/dev/null
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj /CN=local-vault-ca \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' -keyout "$work/ca.key" -out "$work/ca.crt" 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj /CN=vault \
  -keyout "$work/tls.key" -out "$work/tls.csr" 2>/dev/null
openssl x509 -req -in "$work/tls.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial -days 1 \
  -out "$work/tls.crt" -extfile <(printf 'subjectAltName=DNS:vault,DNS:vault.%s.svc,DNS:vault.%s.svc.cluster.local,DNS:localhost,IP:127.0.0.1\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n' "$NS" "$NS") 2>/dev/null
k -n "$NS" create secret tls vault-server-tls --cert="$work/tls.crt" --key="$work/tls.key" >/dev/null
k -n "$APP" create configmap vault-ca --from-file=ca.crt="$work/ca.crt" >/dev/null
k -n "$APP" create configmap vault-probe --from-file="$root/ai_platform/inference/vault.py" \
  --from-file="$root/scripts/vault/workload_probe.py" >/dev/null
# R2: each serving API has its own account; training (MLflow) must no longer log in.
for account in fraud-api feature-api training unrelated; do
  k -n "$APP" create serviceaccount "$account" >/dev/null
done

helm --kube-context "$CONTEXT" install vault "$chart" -n "$NS" \
  -f "$root/infra/kubernetes/vault/values-gke.yaml" -f "$root/infra/kubernetes/vault/values-local.yaml" >/dev/null
k -n "$NS" wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=240s >/dev/null
forward_vault
record status-uninitialised "$(vault status)"
record init "$(vault init --keys-out "$keys")"
record unseal "$(vault unseal --keys-file "$keys")"
for _ in $(seq 40); do vault configure --keys-file "$keys" > "$work/configure.json" 2>/dev/null && break; sleep 1; done
record configure "$(cat "$work/configure.json")"
secret_v1=$(openssl rand -hex 24); secret_v2=$(openssl rand -hex 24)
record seed "$(printf '%s' "$secret_v1" | vault put --keys-file "$keys" --path fraud-api/feast-redis --key connection_string --stdin)"
printf '%s' other | vault put --keys-file "$keys" --path fraud-api/other --key connection_string --stdin >/dev/null
feature_secret=$(openssl rand -hex 24)
printf '%s' "$feature_secret" | vault put --keys-file "$keys" --path feature-api/feast-redis --key connection_string --stdin >/dev/null
hash1=$(printf '%s' "$secret_v1" | sha256sum | cut -d' ' -f1); hash2=$(printf '%s' "$secret_v2" | sha256sum | cut -d' ' -f1)

result=$(probe authorized fraud-api vault "$SECRETS"); record authorized-read "$result"
[ "$(jq -r .value_sha256 <<<"$result")" = "$hash1" ] || { echo "authorized read returned the wrong value" >&2; exit 1; }
result=$(probe wrong-sa unrelated vault "$SECRETS"); record wrong-service-account "$result"
[ "$(jq -r .code <<<"$result")" = vault_login_denied ]
result=$(probe feature-read feature-api vault "$FEATURE" feature-api); record feature-api-authorized-read "$result"
[ "$(jq -r .value_sha256 <<<"$result")" = "$(printf '%s' "$feature_secret" | sha256sum | cut -d' ' -f1)" ]
result=$(probe training-sa training vault "$SECRETS"); record training-account-denied "$result"
[ "$(jq -r .code <<<"$result")" = vault_login_denied ]
result=$(probe feature-as-fraud feature-api vault "$SECRETS" fraud-api); record feature-api-cannot-use-fraud-role "$result"
[ "$(jq -r .code <<<"$result")" = vault_login_denied ]
result=$(probe fraud-as-feature fraud-api vault "$FEATURE" feature-api); record fraud-api-cannot-use-feature-role "$result"
[ "$(jq -r .code <<<"$result")" = vault_login_denied ]
result=$(probe feature-reads-fraud feature-api vault "$SECRETS" feature-api); record feature-role-denied-fraud-path "$result"
[ "$(jq -r .code <<<"$result")" = vault_read_denied ]
result=$(probe wrong-aud fraud-api kubernetes "$SECRETS"); record wrong-audience "$result"
[ "$(jq -r .code <<<"$result")" = vault_login_denied ]
result=$(probe wrong-path fraud-api vault "$OTHER"); record policy-denied-path "$result"
[ "$(jq -r .code <<<"$result")" = vault_read_denied ]

record rotate "$(printf '%s' "$secret_v2" | vault put --keys-file "$keys" --path fraud-api/feast-redis --key connection_string --stdin)"
result=$(probe rotated fraud-api vault "$SECRETS"); record read-after-rotation "$result"
[ "$(jq -r .value_sha256 <<<"$result")" = "$hash2" ] && [ "$(jq -r .version <<<"$result")" = 2 ]

k -n "$NS" delete pod vault-0 --wait=true >/dev/null
k -n "$NS" wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=240s >/dev/null
forward_vault
record restart-is-sealed "$(vault status)"
result=$(probe sealed fraud-api vault "$SECRETS"); record read-while-sealed "$result"
[ "$(jq -r .result <<<"$result")" = denied ]
record unseal-after-restart "$(vault unseal --keys-file "$keys")"
sleep 5
result=$(probe persisted fraud-api vault "$SECRETS"); record read-after-restart "$result"
[ "$(jq -r .value_sha256 <<<"$result")" = "$hash2" ]

record revoke-role "$(vault revoke-role --keys-file "$keys" --role fraud-api)"
result=$(probe revoked fraud-api vault "$SECRETS"); record read-after-revocation "$result"
[ "$(jq -r .code <<<"$result")" = vault_login_denied ]

audit=$(k -n "$NS" exec vault-0 -- sh -c 'wc -l < /vault/audit/audit.log')
leaks=$(k -n "$NS" exec vault-0 -- sh -c "grep -c -e '$secret_v1' -e '$secret_v2' /vault/audit/audit.log || true")
record audit-log "$(jq -cn --argjson lines "$audit" --argjson leaks "$leaks" '{lines: $lines, plaintext_secret_matches: $leaks}')"
[ "$leaks" = 0 ] && [ "$audit" -gt 0 ]
record retire-root "$(vault retire-root --keys-file "$keys")"
if vault versions --keys-file "$keys" --path fraud-api/feast-redis >/dev/null 2>&1; then
  echo "root token still works after retirement" >&2; exit 1
fi
record root-token-rejected '{"result":"denied"}'
echo "LOCAL_VAULT_VALIDATION_PASS"
