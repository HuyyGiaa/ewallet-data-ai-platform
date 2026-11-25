#!/usr/bin/env bash
# Vault V2 + ESO rehearsal (Campaign A2 on GKE, the V2 kind campaign locally), one phase per call.
# Procedure, approvals and PASS conditions: docs/runbooks/edai2/VAULT_GKE_REHEARSAL.md.
#
#   REHEARSAL_CONTEXT=<kind-* | the pinned GKE context>  REHEARSAL_ID=vault-rehearsal-<UTC yyyymmddThhmmssZ>
#   REHEARSAL_DIR=<directory outside the repository>     CHARTS=<directory with the two pinned chart archives>
#   bash scripts/vault/gke_rehearsal.sh <preflight|vault|init|configure|ceremony|seal|eso|consumer|negative|lifecycle|cleanup|verify-clean>
#   bash scripts/vault/gke_rehearsal.sh inspect | gate <retain|after-wake|clean>      (READ-ONLY, §9 of the runbook)
#
# Order: preflight vault init configure ceremony seal (kind only) eso consumer feature-api switch negative rollback
#   agents (kind only) lifecycle retain [GKE: approved sleep + wake] after-wake cleanup verify-clean.
# feature-api.. need FEATURE_IMAGE=<the Feature API image pinned by digest> and run the real serving chart
# as release `rehearsal-serving` in vault-rehearsal (post-renderer scripts/vault/rehearsal/serving_post_render.py).
# agents (kind only) runs the real llm-agent-platform chart (agentgateway + real vLLM CPU, staged model)
# as release `rehearsal-agents` in vault-rehearsal-agents (post-renderer scripts/vault/rehearsal/agents_post_render.py).
#
# Vault V2 (docs/edai2/platform/VAULT_V2_ARCHITECTURE_AUDIT.md): the official Vault and ESO charts with the
# repository values, certificate, NetworkPolicy and bootstrap (names as in the official campaign:
# ewallet-vault, external-secrets, releases vault and external-secrets). Auto-unseal: on kind a throwaway
# transit "unsealer" Vault in ewallet-vault stands in for Cloud KMS (every such result is labelled
# SIMULATED_AUTO_UNSEAL, never a KMS result); on GKE the gcpckms seal needs the P4 KMS resources, and the
# vault phase refuses until P4 exists. Stores: the rehearsal's own ClusterSecretStore ewallet-vault-rehearsal
# (exact namespaces vault-rehearsal, vault-rehearsal-agents) logs in as the official reader identity
# external-secrets/eso-vault-reader through the narrower role eso-vault-rehearsal. On kind only, the official
# ClusterSecretStore ewallet-vault is also created from the committed file, to prove it Ready. Consumers live
# only in the temporary rehearsal namespaces. No object is created in ml-platform, ewallet-agents or any other
# existing namespace; feast-online-redis, the Feature API and the Agent Platform are never changed.
#
# Safety:
# - preflight refuses if any of those namespaces, releases, ESO CRDs, chart cluster objects or
#   Vault volumes already exist, so everything under those names afterwards is the rehearsal's;
# - every object it creates carries ewallet.io/vault-rehearsal=<REHEARSAL_ID>, set at creation
#   with `kubectl create` (fails rather than overwrites); cleanup refuses a namespace without it;
# - on GKE each mutating phase needs VAULT_REHEARSAL_APPROVED=<REHEARSAL_ID>, and cleanup (which
#   deletes the synthetic Vault disks and the ESO CRDs) VAULT_REHEARSAL_CLEANUP_APPROVED=<REHEARSAL_ID>;
# - secret values are synthetic, live only in REHEARSAL_DIR/private (mode 700, removed by cleanup)
#   and are never printed: the evidence holds statuses, key names and 12-character hash prefixes.
# - fail-fast: once a mutating phase has failed or was interrupted, or cleanup has started, every
#   normal phase refuses; only inspect, cleanup and verify-clean run. `gate` exits 0 only when the
#   run may go on to the next cloud step (sleep, cleanup verdict, final sleep);
# - cleanup works from the state of each recorded Vault volume (scripts/vault/rehearsal/volume_states.jq),
#   including a Retain recovery stopped half-way, and refuses on anything it cannot identify.
# It never wakes or sleeps the cluster: that is cloud_lab.py, approved separately.
set -euo pipefail
case $- in *x*) echo "refusing to run with xtrace: it would print secret values" >&2; exit 2 ;; esac

phase=${1:?phase} arg=${2:-}
GKE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
GKE_KUBECONFIG=$HOME/.cache/ewallet-cloud-lab/kubeconfig
PROJECT=ewallet-data-ai-platform-hg26
CTX=${REHEARSAL_CONTEXT:?REHEARSAL_CONTEXT} ID=${REHEARSAL_ID:?REHEARSAL_ID} DIR=${REHEARSAL_DIR:?REHEARSAL_DIR}
VNS=ewallet-vault ENS=external-secrets RNS=vault-rehearsal LABEL=ewallet.io/vault-rehearsal PORT=${REHEARSAL_PORT:-18233}
ANS=vault-rehearsal-agents DNS=vault-rehearsal-denied SANDBOX=ewallet-sandbox STORE=ewallet-vault-rehearsal OFFICIAL=ewallet-vault
UPORT=${REHEARSAL_UNSEALER_PORT:-18235} GPORT=${REHEARSAL_GATEWAY_PORT:-18236} AGENTS=rehearsal-agents MODELPV=vault-rehearsal-staged-models
SEED_SECRET=feast-online-redis-vault RPATH=ml-platform/rehearsal-redis
VPATH=agent-platform/rehearsal-vllm-api-key CPATH=agent-platform/rehearsal-agentgateway-client-key
CLUSTER_KINDS=clusterrole,clusterrolebinding,validatingwebhookconfiguration,mutatingwebhookconfiguration
SERVING=rehearsal-serving LEGACY=rehearsal-feast-online-redis FPORT=${REHEARSAL_FEATURE_PORT:-18234}
REDIS_IMAGE=docker.io/library/redis:7.4.2-alpine@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952
root=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONPATH=$root

refuse() { echo "REFUSED: $1" >&2; exit 2; }
[[ $ID =~ ^vault-rehearsal-[0-9]{8}T[0-9]{6}Z$ ]] || refuse "REHEARSAL_ID must look like vault-rehearsal-20261125T010203Z"
if [[ $CTX == kind-* ]]; then KIND=1
elif [ "$CTX" = "$GKE_CONTEXT" ]; then
  KIND=
  [ "${KUBECONFIG:-}" = "$GKE_KUBECONFIG" ] || refuse "on GKE, KUBECONFIG must be $GKE_KUBECONFIG"
else refuse "context $CTX is neither kind-* nor $GKE_CONTEXT"; fi
[ -z "${REHEARSAL_FAULT:-}" ] || [ -n "$KIND" ] || refuse "REHEARSAL_FAULT is for kind fault-injection tests only"
case "$phase" in preflight|verify-clean|inspect) ;;
  gate) case "$arg" in retain|after-wake|clean) ;; *) refuse "gate needs retain, after-wake or clean" ;; esac ;;
  cleanup) [ -n "$KIND" ] || [ "${VAULT_REHEARSAL_CLEANUP_APPROVED:-}" = "$ID" ] || refuse "cleanup needs VAULT_REHEARSAL_CLEANUP_APPROVED=$ID" ;;
  vault|init|ceremony|seal|configure|eso|consumer|negative|lifecycle|feature-api|switch|rollback|agents|retain|after-wake)
    [ -n "$KIND" ] || [ "${VAULT_REHEARSAL_APPROVED:-}" = "$ID" ] || refuse "$phase needs VAULT_REHEARSAL_APPROVED=$ID" ;;
  *) refuse "unknown phase $phase" ;;
esac
DIR=$(realpath -m "$DIR")                       # resolve first: nothing is created inside the repository
case "$DIR/" in "$root"/*) refuse "REHEARSAL_DIR must be outside the repository" ;; esac
mkdir -p "$DIR"
PRIV=$DIR/private
case "$phase" in preflight|verify-clean|inspect|gate) ;; *) mkdir -p -m 700 "$PRIV"; chmod 700 "$PRIV" ;; esac   # synthetic material only
EV=$DIR/results.jsonl

k() { kubectl --context "$CTX" "$@"; }
h() { helm --kube-context "$CTX" "$@"; }
vault() { python3 -m ops.vault.cli --address "https://127.0.0.1:$PORT" --ca-file "$PRIV/ca.crt" "$@"; }
record() {  # <step> <json>: an empty or non-JSON result is itself a failure, never a silent skip
  local line
  line=$(printf '%s\n' "$2" | jq -c --arg step "$1" --arg phase "$phase" '{phase: $phase, step: $step} + .' 2>/dev/null) || line=
  if [ -z "$line" ]; then
    jq -cn --arg step "$1" --arg phase "$phase" '{phase: $phase, step: $step, result: "FAIL", detail: "no JSON result"}' | tee -a "$EV"
    exit 1
  fi
  printf '%s\n' "$line" | tee -a "$EV"
}
fail() { record "$1" "$(jq -cn --arg detail "$2" '{result: "FAIL", detail: $detail}')"; exit 1; }
own() {  # stdin YAML -> stdout YAML with this rehearsal's label on every object, set at creation
  python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
for d in docs:
    d.setdefault("metadata", {}).setdefault("labels", {})[sys.argv[1]] = sys.argv[2]
yaml.safe_dump_all(docs, sys.stdout)' "$LABEL" "$ID"
}
create() { own | k create -f - >/dev/null; }      # fails, rather than overwrites, if the name exists
sha() { printf '%s' "$1" | sha256sum | cut -c1-12; }
secret_sha() { k -n "$1" get secret "$2" -o go-template="{{index .data \"$3\"}}" | base64 -d | sha256sum | cut -c1-12; }
conn() { printf 'redis.%s.svc.cluster.local:6379,password=%s' "$RNS" "$1"; }
synthetic() { sed -n "s/^$1=//p" "$PRIV/synthetic.env"; }   # a synthetic value saved by the consumer phase
set_synthetic() { (umask 077; sed -i "/^$1=/d" "$PRIV/synthetic.env"; printf '%s=%s\n' "$1" "$2" >> "$PRIV/synthetic.env"); }
seal_kind() { [ -n "$KIND" ] && echo transit || echo gcpckms; }
AUTO=$([ -n "$KIND" ] && echo SIMULATED_AUTO_UNSEAL || echo KMS_GKE)   # the evidence label of every auto-unseal result
wait_unsealed() {  # [seconds]: auto-unseal, never a manual unseal; prints the status
  local s
  for _ in $(seq "${1:-120}"); do
    s=$(vault status 2>/dev/null) && [ "$(jq -r '"\(.initialized) \(.sealed)"' <<<"$s")" = "true false" ] && { printf '%s' "$s"; return 0; }
    sleep 1
  done
  return 1
}
mine() { [ "$(k get ns "$1" -o jsonpath="{.metadata.labels.ewallet\.io/vault-rehearsal}" 2>/dev/null)" = "$ID" ]; }

forward=
stop_forward() { [ -z "$forward" ] || kill "$forward" 2>/dev/null || true; forward=; }
ffwd=
stop_ffwd() { [ -z "$ffwd" ] || kill "$ffwd" 2>/dev/null || true; ffwd=; }
trap 'stop_forward; stop_ffwd' EXIT
open_forward_nowait() {  # a port-forward that may not answer yet (Vault without a listener during a seal outage)
  stop_forward
  kubectl --context "$CTX" -n "$VNS" port-forward pod/vault-0 "$PORT:8200" >/dev/null 2>&1 & forward=$!
  sleep 2
}
open_forward() {  # Vault through a port-forward (enters the pod's network namespace; TLS verified with vault-ca)
  for _ in $(seq 15); do
    stop_forward
    # kubectl itself in the background (not the k function): $! must be kubectl's PID, or kill orphans it.
    kubectl --context "$CTX" -n "$VNS" port-forward pod/vault-0 "$PORT:8200" >/dev/null 2>&1 & forward=$!
    sleep 2
    vault status >/dev/null 2>&1 && return 0
  done
  fail port-forward "Vault did not answer on 127.0.0.1:$PORT"
}
status_is() {  # <initialized> <sealed>: the real Vault state, never Pod readiness
  local s; s=$(vault status) || fail vault-status "status call failed"
  [ "$(jq -r '"\(.initialized) \(.sealed)"' <<<"$s")" = "$1 $2" ] || fail vault-status "expected initialized=$1 sealed=$2, got $s"
  printf '%s' "$s"
}
pod_ready() { k -n "$VNS" get pod vault-0 -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'; }

# ---- ExternalSecret helpers
es_condition() { k -n "$1" get externalsecret "$2" -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status} {.reason}{end}' 2>/dev/null; }
es_synced_rv() { k -n "$1" get externalsecret "$2" -o jsonpath='{.status.syncedResourceVersion}' 2>/dev/null; }
es_wait() {  # <ns> <es> <True|False> [seconds]
  for _ in $(seq "${4:-120}"); do [ "$(es_condition "$1" "$2" | cut -d' ' -f1)" = "$3" ] && return 0; sleep 1; done; return 1
}
es_error() {  # <object> [since]: the cause class from the ESO controller log (events are generic); never the log line
  local text
  text=$(k -n "$ENS" logs deploy/external-secrets --since="${2:-10m}" 2>/dev/null | grep -F "\"$1\"" | tail -40) || true
  case "$text" in
    *"namespace not authorized"*) echo role-bound-to-other-namespace ;;
    *"service account name not authorized"*) echo service-account-not-bound-to-role ;;
    *"invalid audience"*) echo token-audience-rejected ;;
    *"x509"*) echo tls-untrusted-ca ;;
    *"Vault is sealed"*|*"Code: 503"*) echo vault-sealed ;;
    *"connection refused"*|*"no such host"*|*"i/o timeout"*|*"dial tcp"*) echo vault-unreachable ;;
    *"cannot find secret data for key"*) echo vault-key-missing ;;
    *"Code: 404"*|*"Secret does not exist"*|*"secret does not exist"*) echo vault-secret-missing ;;
    *"/login"*"permission denied"*) echo vault-login-denied ;;
    *"permission denied"*) echo vault-permission-denied ;;
    *) echo unclassified ;;
  esac
}
force_sync() { k -n "${2:-$RNS}" annotate externalsecret "$1" "force-sync=$(date +%s%N)" --overwrite >/dev/null; }   # <es> [ns]
synced_after() {  # <es> <previous syncedResourceVersion> [ns]: a NEW reconciliation, not a stale Ready=True
  local ns=${3:-$RNS}
  for _ in $(seq 120); do
    local rv; rv=$(es_synced_rv "$ns" "$1")
    [ -n "$rv" ] && [ "$rv" != "$2" ] && [ "$(es_condition "$ns" "$1")" = "True SecretSynced" ] && { printf '%s' "$rv"; return 0; }
    sleep 1
  done
  return 1
}
negative() {  # <step> <externalsecret> <expected class> [ns]: unsynced, no target Secret, and the specific cause
  local class= ns=${4:-$RNS}
  es_wait "$ns" "$2" False 90 || fail "$1" "ExternalSecret $2 did not report a failure"
  if k -n "$ns" get secret "$2" >/dev/null 2>&1; then fail "$1" "target Secret $2 exists"; fi
  for _ in $(seq 30); do class=$(es_error "$2"); [ "$class" = "$3" ] && break; sleep 2; done
  [ "$class" = "$3" ] || fail "$1" "expected $3, observed $class"
  record "$1" "$(jq -cn --arg c "$(es_condition "$ns" "$2")" --arg e "$class" --arg n "$ns" \
    '{result: "denied", namespace: $n, condition: $c, error_class: $e, target_secret: "absent"}')"
}
negative_namespace() {  # <step> <ns> <externalsecret> <store>: the store's namespace conditions refuse it before any Vault call
  local msg
  es_wait "$2" "$3" False 90 || fail "$1" "ExternalSecret $2/$3 did not report a failure"
  if k -n "$2" get secret "$3" >/dev/null 2>&1; then fail "$1" "target Secret $2/$3 exists"; fi
  msg=$(k -n "$2" get externalsecret "$3" -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}')
  case "$msg" in *"$4"*"not allowed"*|*"$4"*"denied by spec.condition"*) ;; *) fail "$1" "unexpected cause: ${msg:0:200}" ;; esac
  record "$1" "$(jq -cn --arg n "$2" --arg m "$msg" --arg s "$4" \
    '{result: "denied", namespace: $n, store: $s, error_class: "store-not-allowed-in-namespace", message: $m, target_secret: "absent"}')"
}
external_secret() {  # <ns> <name> <target> <store kind> <store> <key> <property> [extra label]
  own <<YAML | python3 -c 'import sys,yaml; d=yaml.safe_load(sys.stdin); l=sys.argv[1]; (l and d["metadata"]["labels"].update({l: "true"})); yaml.safe_dump(d, sys.stdout)' "${8:-}" | k create -f - >/dev/null
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata: {name: $2, namespace: $1}
spec:
  refreshInterval: 1h
  secretStoreRef: {kind: $4, name: $5}
  target: {name: $3, creationPolicy: Owner, deletionPolicy: Retain}
  data:
    - secretKey: $7
      remoteRef: {key: $6, property: $7}
YAML
}
secret_store() {  # <name> <service account> <audience> <role> <ca configmap>: a namespaced store in vault-rehearsal (negative tests)
  create <<YAML
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata: {name: $1, namespace: $RNS, labels: {ewallet.io/vault-rehearsal-negative: "true"}}
spec:
  provider:
    vault:
      server: https://vault.$VNS.svc.cluster.local:8200
      path: ewallet
      version: v2
      caProvider: {type: ConfigMap, name: $5, key: ca.crt}
      auth:
        kubernetes:
          mountPath: kubernetes
          role: $4
          serviceAccountRef: {name: $2, audiences: [$3]}
YAML
}
cluster_store() {  # <name> <audience> <role> <ca configmap in external-secrets> <namespaces json> [negative=false]
  create <<YAML
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata: {name: $1, labels: {ewallet.io/vault-rehearsal-negative: "${6:-false}"}}
spec:
  conditions:
    - namespaces: $5
  provider:
    vault:
      server: https://vault.$VNS.svc.cluster.local:8200
      path: ewallet
      version: v2
      caProvider: {type: ConfigMap, name: $4, key: ca.crt, namespace: $ENS}
      auth:
        kubernetes:
          mountPath: kubernetes
          role: $3
          serviceAccountRef: {name: eso-vault-reader, namespace: $ENS, audiences: [$2]}
YAML
}
store_ready() { k get clustersecretstore "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status} {.status.conditions[?(@.type=="Ready")].reason}' 2>/dev/null; }
reader_probe() {  # <role>: log in as the official reader identity and try what it must and must not do (status codes only)
  local hdr=$PRIV/reader-header code out='{}' body
  k -n "$ENS" create token eso-vault-reader --audience vault --duration 10m \
    | vault login --role "$1" --token-out "$PRIV/reader-$1.json" >/dev/null || fail reader-login "login as $1 failed"
  (umask 077; printf 'X-Vault-Token: %s\n' "$(jq -r .token "$PRIV/reader-$1.json")" > "$hdr")
  for c in "GET ewallet/data/$RPATH" "GET ewallet/data/mcp-platform/out-of-prefix-probe" "GET ewallet/metadata/$RPATH" \
           "LIST ewallet/metadata/ml-platform" "POST ewallet/data/$RPATH" "GET sys/policies/acl" "POST sys/policies/acl/intruder" \
           "GET auth/kubernetes/role?list=true" "POST sys/generate-root/attempt"; do
    body=(); [ "${c%% *}" != POST ] || body=(--data '{"data": {"x": "y"}, "policy": "path \"*\" {capabilities = [\"sudo\"]}"}')
    code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$PRIV/ca.crt" -H "@$hdr" -X "${c%% *}" "${body[@]}" \
      "https://127.0.0.1:$PORT/v1/${c#* }") || code=000
    out=$(jq -c --arg c "$c" --arg s "$code" '. + {($c): $s}' <<<"$out")
  done
  curl -s -o /dev/null --cacert "$PRIV/ca.crt" -H "@$hdr" -X POST "https://127.0.0.1:$PORT/v1/auth/token/revoke-self" || true
  rm -f "$hdr" "$PRIV/reader-$1.json"
  printf '%s' "$out"
}
restricted='securityContext: {runAsNonRoot: true, runAsUser: 999, runAsGroup: 999, seccompProfile: {type: RuntimeDefault}}'
container_security='securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}'
consume() {  # [secret]: one real authenticated Redis round trip (default: the ESO-managed Secret); prints OK, AUTH_FAILED or UNREACHABLE
  local job state secret=${1:-feast-online-redis-vault}
  job=$(own <<YAML | k create -f - -o name
apiVersion: batch/v1
kind: Job
metadata: {generateName: rehearsal-consumer-, namespace: $RNS}
spec:
  backoffLimit: 0
  template:
    metadata: {labels: {app.kubernetes.io/name: rehearsal-consumer}}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      $restricted
      containers:
        - name: consumer
          image: $REDIS_IMAGE
          $container_security
          env: [{name: CONN, valueFrom: {secretKeyRef: {name: $secret, key: connection_string}}}]
          command: [sh, -c]
          args:
            - |
              host=\${CONN%%:*}; rest=\${CONN#*:}; port=\${rest%%,*}; export REDISCLI_AUTH=\${CONN##*password=}
              # AUTH_FAILED only for a rejected password; a connection error (Service endpoints still being
              # programmed) is retried for 30 s and then reported as UNREACHABLE, never as an auth result.
              for i in \$(seq 30); do
                out=\$(redis-cli -h "\$host" -p "\$port" --no-auth-warning PING 2>&1)
                case "\$out" in PONG) break ;; *WRONGPASS*|*NOAUTH*) echo AUTH_FAILED; exit 1 ;; esac
                sleep 1
              done
              [ "\$out" = PONG ] || { echo UNREACHABLE; exit 1; }
              nonce=\$(cat /proc/sys/kernel/random/uuid)
              redis-cli -h "\$host" -p "\$port" --no-auth-warning SET rehearsal:nonce "\$nonce" EX 60 >/dev/null
              [ "\$(redis-cli -h "\$host" -p "\$port" --no-auth-warning GET rehearsal:nonce)" = "\$nonce" ] && echo ROUND_TRIP_OK || { echo ROUND_TRIP_FAILED; exit 1; }
YAML
)
  for _ in $(seq 150); do
    state=$(k -n "$RNS" get "$job" -o jsonpath='{.status.succeeded}/{.status.failed}')
    case "$state" in 1/*) break ;; */1) break ;; esac; sleep 1
  done
  case "$(k -n "$RNS" logs "$job" 2>/dev/null | tail -1)" in
    ROUND_TRIP_OK) echo OK ;; AUTH_FAILED) echo AUTH_FAILED ;; UNREACHABLE) echo UNREACHABLE ;; *) echo "UNEXPECTED:$state" ;;
  esac
}
collisions() {  # JSON of every pre-existing object the rehearsal would own; fails closed on any query error
  local ns crds rel objs pvs
  # ewallet-sandbox is a rehearsal namespace on kind only (the official store's excluded-namespace test); on GKE it is production.
  ns=$(k get ns -o json | jq -c --argjson want "$(rehearsal_namespaces | jq -R . | jq -cs .)" '[.items[].metadata.name | select(IN($want[]))]') || return 1
  crds=$(k get crd -o json | jq -c '[.items[].metadata.name | select(endswith(".external-secrets.io"))] | length') || return 1
  rel=$(h list -A -a -o json | jq -c '[.[] | select(.name == "vault" or .name == "external-secrets" or .name == "rehearsal-serving" or .name == "rehearsal-agents") | "\(.namespace)/\(.name)"]') || return 1
  objs=$(k get "$CLUSTER_KINDS" -l 'app.kubernetes.io/instance in (vault,external-secrets)' -o json | jq -c '[.items[] | "\(.kind)/\(.metadata.name)"]') || return 1
  pvs=$(k get pv -o json | jq -c --arg a "$VNS" --arg c "$RNS" --arg g "$ANS" --arg m "$MODELPV" '[.items[] | select(.spec.claimRef.namespace == $a or .spec.claimRef.namespace == $c
    or .spec.claimRef.namespace == $g or .metadata.name == $m) | .metadata.name]') || return 1
  jq -cn --argjson ns "$ns" --argjson crds "$crds" --argjson rel "$rel" --argjson objs "$objs" --argjson pvs "$pvs" \
    '{namespaces: $ns, eso_crds: $crds, releases: $rel, chart_cluster_objects: $objs, vault_volumes: $pvs}'
}
rehearsal_namespaces() { printf '%s\n' "$VNS" "$ENS" "$RNS" "$ANS" "$DNS"; [ -z "$KIND" ] || printf '%s\n' "$SANDBOX"; }
none_found() { jq -e '(.namespaces + .releases + .chart_cluster_objects + .vault_volumes | length) == 0 and .eso_crds == 0' <<<"$1" >/dev/null; }
snapshot() {  # production state the rehearsal must not change (names and versions only; its own releases excluded)
  # The Feature API is compared by its pod template (image, Secret reference), not metadata.generation:
  # a guarded sleep scales Deployments to zero, which raises generation without changing the configuration.
  local rel feast fapi lbs gw agents
  rel=$(h list -A -a -o json | jq -c '[.[] | select(.name != "vault" and .name != "external-secrets" and .name != "rehearsal-serving" and .name != "rehearsal-agents") | {name, namespace, revision, status}]
    | sort_by(.namespace, .name)') || return 1
  feast=$(k -n ml-platform get secret feast-online-redis --ignore-not-found -o jsonpath='{.metadata.resourceVersion}') || return 1
  fapi=$(k -n ml-platform get deploy feature-api --ignore-not-found -o json | jq -cs '.[0] | if . == null then null else
    {image: .spec.template.spec.containers[0].image, redis_secret: ([.spec.template.spec.containers[0].env[]? | select(.name == "EWALLET_REDIS_CONNECTION_STRING") | .valueFrom.secretKeyRef.name][0])} end') || return 1
  lbs=$(k get svc -A -o json | jq -c '[.items[] | select(.spec.type == "LoadBalancer") | "\(.metadata.namespace)/\(.metadata.name)"] | sort') || return 1
  gw=$(k -n ewallet-ingress get svc ewallet-edge-gateway --ignore-not-found -o jsonpath='{.spec.type}') || return 1
  # The live Agent Platform credentials and their consumer: resourceVersions and Secret references only, by name.
  agents=$(for s in vllm-api-key agentgateway-client-key; do
      printf '{"secret": "%s", "resource_version": "%s"}\n' "$s" "$(k -n ewallet-agents get secret "$s" --ignore-not-found -o jsonpath='{.metadata.resourceVersion}')"
    done; k -n ewallet-agents get deploy agentgateway --ignore-not-found -o json | jq -c 'select(. != null)
      | {deployment: .metadata.name, secrets: [.spec.template.spec.containers[].env[]?.valueFrom.secretKeyRef.name // empty]}') || return 1
  agents=$(jq -cs . <<<"$agents") || return 1
  jq -cn --argjson rel "$rel" --arg feast "$feast" --argjson fapi "$fapi" --argjson lbs "$lbs" --arg gw "$gw" --argjson agents "${agents:-[]}" \
    '{helm_releases: $rel, feast_online_redis_resource_version: $feast, feature_api: $fapi, loadbalancer_services: $lbs, edge_gateway_service_type: $gw,
      agent_platform_credentials: $agents}'
}

compare_production() {  # <step>: the production snapshot must equal baseline.json; any drift stops the run
  local now
  [ -f "$DIR/baseline.json" ] || fail "$1" "no baseline.json: run preflight first"
  now=$(snapshot) || fail "$1" "snapshot query failed"
  if jq -e --argjson now "$now" '. == $now' "$DIR/baseline.json" >/dev/null; then
    record "$1" '{"result": "PASS", "compared": ["helm releases and revisions", "feast-online-redis resourceVersion", "feature-api image and Secret", "LoadBalancer Services", "edge gateway Service type", "ewallet-agents vllm-api-key, agentgateway-client-key and agentgateway Secret references"]}'
  else
    printf '%s\n' "$now" > "$DIR/snapshot-differs-$1.json"
    fail "$1" "production differs from baseline in: $(jq -rn --argjson b "$(cat "$DIR/baseline.json")" --argjson n "$now" \
      '[($b | keys[]) as $k | select($b[$k] != $n[$k]) | $k] | join(", ")') (see snapshot-differs-$1.json); stop, nothing was cleaned up"
  fi
}
disk_state() {  # <volume handle projects/P/zones/Z/disks/D>: present | absent | unknown (an API error is never "absent")
  # `gcloud compute disks list` exits 0 even when the project cannot be read, so it cannot prove absence.
  # `describe` can: only a 404 naming this exact disk path means "absent". The error text (it names the
  # gcloud account) is matched in memory and never recorded.
  local zone name out err
  zone=$(cut -d/ -f4 <<<"$1"); name=${1##*/}
  [ -n "$zone" ] && [ -n "$name" ] && [ "$(cut -d/ -f1-2 <<<"$1")" = "projects/$PROJECT" ] || { echo unknown; return; }
  if out=$(gcloud compute disks describe "$name" --zone "$zone" --project "$PROJECT" --format='value(name)' 2>/dev/null); then
    [ "$out" = "$name" ] && echo present || echo unknown
  else
    err=$(gcloud compute disks describe "$name" --zone "$zone" --project "$PROJECT" --format='value(name)' 2>&1 >/dev/null) || true
    case "$err" in
      *"HTTPError 404: The resource 'projects/$PROJECT/zones/$zone/disks/$name' was not found"*) echo absent ;;
      *) echo unknown ;;
    esac
  fi
}
check_volumes_before_delete() {  # <allowed states>: identity and state of the recorded volumes; the only gate before a PVC/PV change
  local st bad
  st=$(volume_states)
  [ "$(jq '.problems | length' <<<"$st")" = 0 ] \
    || fail volumes-identity "$(jq -r '.problems | join("; ")' <<<"$st"); nothing deleted, owner decision (VAULT_GKE_REHEARSAL.md §9)"
  bad=$(jq -r --arg ok "$1" '[.volumes[] | select(.state | IN($ok | split(" ")[]) | not) | "\(.claim): \(.state)"] | join(", ")' <<<"$st")
  [ -z "$bad" ] || fail volumes-identity "volume state $bad, expected one of: $1; nothing deleted"
  if [ -z "$KIND" ]; then   # a disk whose PV still exists must be confirmed; a gone PV's disk is proven absent at the end
    for handle in $(jq -r '.volumes[] | select(.state != "gone") | .disk' <<<"$st"); do
      [ "$(disk_state "$handle")" = present ] || fail volumes-identity "disk ${handle##*/} not confirmed present before deletion"
    done
  fi
  record volumes-identity "$(jq -c --arg d "$([ -z "$KIND" ] && echo 'confirmed present where the PV exists' || echo 'kind local-path')" \
    '{result: "PASS", volumes: [.volumes[] | {claim, name, state, reclaim}], disks: $d}' <<<"$st")"
}
volume_states() {  # READ-ONLY: the recorded volumes classified by volume_states.jq; fails closed on a missing record or a query error
  local recorded pvs pvcs
  recorded=$(jq -ce 'select(type == "array" and length == 2 and all(.[]; (.name | type) == "string" and (.claim | type) == "string" and has("disk")))' \
    "$DIR/volumes.json" 2>/dev/null) || fail volumes-identity "volumes.json missing or invalid: the rehearsal volumes cannot be identified; nothing deleted"
  pvs=$(k get pv -o json) || fail volumes-identity "PV query failed; nothing deleted"
  pvcs=$(k -n "$VNS" get pvc -o json) || fail volumes-identity "PVC query failed; nothing deleted"
  printf '%s\n%s\n%s\n' "$recorded" "$pvs" "$pvcs" | jq -cs --arg ns "$VNS" '{recorded: .[0], pvs: .[1].items, pvcs: .[2].items, ns: $ns}' \
    | jq -c -f "$root/scripts/vault/rehearsal/volume_states.jq" || fail volumes-identity "volume classification failed; nothing deleted"
}
rebind_volume() {  # <pv> <claim>: the runbook's Recovery of a Released PV (clear claimRef, recreate the PVC with volumeName); resumable
  local v=$1 claim=$2 pv
  pv=$(k get pv "$v" -o json) || fail rebind "PV $v unreadable"
  if [ "$(jq -r .status.phase <<<"$pv")" = Released ]; then
    k patch pv "$v" --type json -p '[{"op":"remove","path":"/spec/claimRef"}]' >/dev/null
  fi
  fault "claimref-removed:$claim"
  if [ -z "$(k -n "$VNS" get pvc "$claim" -o name --ignore-not-found)" ]; then
    create <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: $claim, namespace: $VNS}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $(jq -r .spec.storageClassName <<<"$pv")
  volumeName: $v
  resources: {requests: {storage: $(jq -r .spec.capacity.storage <<<"$pv")}}
YAML
  fi
  for _ in $(seq 60); do [ "$(k -n "$VNS" get pvc "$claim" -o jsonpath='{.status.phase}/{.spec.volumeName}')" = "Bound/$v" ] && break; sleep 2; done
  [ "$(k -n "$VNS" get pvc "$claim" -o jsonpath='{.status.phase}/{.spec.volumeName}')" = "Bound/$v" ] || fail rebind "PVC $claim not bound to $v"
}
fault() {  # <point>: kind fault-injection tests stop here (REHEARSAL_FAULT is refused on GKE)
  [ "${REHEARSAL_FAULT:-}" != "$1" ] || fail fault-injected "stopped at $1 (kind fault-injection test)"
}
check_eso_scope() {  # ESO may only be serving the rehearsal: no store or ExternalSecret elsewhere, every cluster store ours, no inventory drift
  local where objs stores
  where=$(k get externalsecret,secretstore,pushsecret -A -o json | jq -c --argjson ok "$(rehearsal_namespaces | jq -R . | jq -cs .)" \
    '[.items[] | select(.metadata.namespace | IN($ok[]) | not) | "\(.kind) \(.metadata.namespace)/\(.metadata.name)"]') \
    || fail eso-scope "ExternalSecret/SecretStore query failed"
  [ "$where" = "[]" ] || fail eso-scope "ESO objects outside the rehearsal namespaces: $where"
  stores=$(k get clustersecretstore -o json | jq -c --arg l "$LABEL" --arg id "$ID" '[.items[] | select(.metadata.labels[$l] != $id) | .metadata.name]') \
    || fail eso-scope "ClusterSecretStore query failed"
  [ "$stores" = "[]" ] || fail eso-scope "ClusterSecretStores not created by this rehearsal: $stores (never deleted)"
  objs=$(k get "$CLUSTER_KINDS" -l app.kubernetes.io/instance=external-secrets -o json | jq -c '[.items[] | "\(.kind)/\(.metadata.name)"] | sort') \
    || fail eso-scope "cluster object query failed"
  [ "$objs" = "$(jq -c '.cluster_objects | sort' "$DIR/eso-cluster-objects.json")" ] || fail eso-scope "ESO cluster objects differ from the recorded inventory: $objs"
  record eso-scope '{"result": "PASS", "externalsecrets_and_stores": "only in the rehearsal namespaces", "cluster_secret_stores": "all labelled with this id", "cluster_objects": "as recorded"}'
}

# ---- Campaign A+: the real Feature API (serving chart as a Helm release) on the ESO-managed Secret
serving() {  # <install|upgrade> [extra helm args]: the serving chart through the rehearsal post-renderer
  local verb=$1; shift
  REHEARSAL_NAMESPACE=$RNS REHEARSAL_ID=$ID h "$verb" "$SERVING" "$root/infra/helm/ewallet-serving" -n "$RNS" \
    --post-renderer "$root/scripts/vault/rehearsal/serving_post_render.py" --wait --timeout 10m "$@" >/dev/null
}
secret_ref() { k -n "$RNS" get deploy feature-api -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="EWALLET_REDIS_CONNECTION_STRING")].valueFrom.secretKeyRef.name}'; }
open_feature_forward() {  # the Feature API Service; reopened after every rollout (the old pod goes away)
  for _ in $(seq 20); do
    stop_ffwd
    kubectl --context "$CTX" -n "$RNS" port-forward svc/feature-api "$FPORT:80" >/dev/null 2>&1 & ffwd=$!
    sleep 2
    curl -sf --max-time 5 -o /dev/null "http://127.0.0.1:$FPORT/health/live" && return 0
  done
  fail feature-forward "Feature API did not answer on 127.0.0.1:$FPORT"
}
lookup_class() {  # <gate>: one real lookup and readiness, classified as in the acceptance plan §4; prints the class
  local body=$DIR/lookup-$1.json code rc ready rrc cls json
  code=$(curl -sS --max-time 30 -o "$body" -w '%{http_code}' -H 'content-type: application/json' \
    --data-binary "@$DIR/lookup-request.json" "http://127.0.0.1:$FPORT/v1/features/lookup" 2>/dev/null) && rc=0 || rc=$?
  ready=$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$FPORT/health/ready" 2>/dev/null) && rrc=0 || rrc=$?
  json=$(jq -c . "$body" 2>/dev/null) || json=; [ -n "$json" ] || json='{}'     # an empty or invalid body is never a pass
  cls=$(jq -rn --arg c "${code:-000}" --argjson rc "$rc" --arg r "${ready:-000}" --argjson rrc "$rrc" \
    --argjson want "$(jq -c '.feature_names | sort' "$DIR/lookup-request.json")" \
    --argjson b "$json" '
    if $rc == 0 and $c == "200" and $rrc == 0 and $r == "200" and (($b.features // {}) | keys | sort) == $want then "BASELINE_OK"
    elif $rc == 0 and $c == "424" and $r == "200" and (($b.error.code // "") | IN("missing_feature", "stale_feature", "invalid_feature_timestamp"))
      then "BASELINE_DATA_UNAVAILABLE"
    else "BASELINE_UNHEALTHY" end')
  record "lookup-$1" "$(jq -cn --arg g "$1" --arg c "${code:-000}" --argjson rc "$rc" --arg r "${ready:-000}" --arg cls "$cls" \
    --argjson n "$(jq '(.features // {}) | length' <<<"$json")" \
    '{gate: $g, http: $c, curl_exit: $rc, ready_http: $r, features_returned: $n, class: $cls}')" >/dev/null
  printf '%s' "$cls"
}
expect_lookup() {  # <gate>: the lookup must be a real business success; anything else stops the run
  local cls; cls=$(lookup_class "$1")
  [ "$cls" = BASELINE_OK ] || fail "lookup-$1" "class $cls (see lookup-$1.json); HTTP 424 is never a business success"
}
seed_registry() {  # Feast apply + materialize of a synthetic fixture into the rehearsal Redis, on the Feature API image
  local job state out
  job=$(own <<YAML | k create -f - -o name
apiVersion: batch/v1
kind: Job
metadata: {generateName: rehearsal-feast-seed-, namespace: $RNS}
spec:
  backoffLimit: 0
  template:
    metadata: {labels: {app.kubernetes.io/name: rehearsal-feast-seed}}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: seed
          image: $FEATURE_IMAGE
          $container_security
          command: [python, -c]
          args:
            - |
              # The same fixture as tests/integration/test_feast_online_operator.py, on the production
              # image: three entities per view, three updates each, materialized through Feast.
              import base64, json
              from datetime import datetime
              from pathlib import Path
              import pandas as pd
              from ai_platform.features.feast import online_operator
              from ai_platform.features.feast.schema import FEATURE_SPECS
              from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS
              entities = {"user_behavior": ("u1", "u2", "u3"), "account_behavior": ("a1", "a2", "a3"),
                          "device_behavior": ("d1", "d2", "d3"), "merchant_behavior": ("m1", "m2", "m3")}
              t0 = pd.Timestamp("2026-05-31 22:00:00", tz="UTC")
              root = Path("/tmp/offline")
              for view, spec in FEATURE_SPECS.items():
                  rows = []
                  for i, entity in enumerate(entities[view]):
                      for step, minutes in enumerate((5, 50, 115)):
                          row = {spec["entity_key"]: entity, "event_timestamp": t0 + pd.Timedelta(minutes=minutes + i)}
                          for j, feature in enumerate(spec["features"]):
                              value = i * 1000 + step * 100 + j
                              row[feature] = value if feature in spec["integer_features"] else value / 7
                          rows.append(row)
                  frame = pd.DataFrame(rows)
                  frame["event_timestamp"] = frame["event_timestamp"].astype("datetime64[us, UTC]")
                  (root / spec["table"]).mkdir(parents=True)
                  frame.to_parquet(root / spec["table"] / "part-0.parquet", index=False)
              store = online_operator.get_online_store()
              online_operator.apply(store)
              online_operator.materialize(store, datetime.fromisoformat("2026-05-31T22:00:00+00:00"),
                                          datetime.fromisoformat("2026-06-01T00:00:00+00:00"))
              print(json.dumps({"feature_names": list(HISTORICAL_FEATURE_COLUMNS),
                                "registry_b64": base64.b64encode(Path("/registry/registry.db").read_bytes()).decode()}))
          env:
            - {name: EWALLET_FEAST_REGISTRY, value: /registry/registry.db}
            - {name: EWALLET_FEAST_OFFLINE_DATA_ROOT, value: /tmp/offline}
            - {name: HOME, value: /tmp}
            - {name: FEAST_USAGE, value: "False"}
            - name: EWALLET_REDIS_CONNECTION_STRING
              valueFrom: {secretKeyRef: {name: $SEED_SECRET, key: connection_string}}
          volumeMounts: [{name: registry, mountPath: /registry}, {name: tmp, mountPath: /tmp}]
      volumes: [{name: registry, emptyDir: {}}, {name: tmp, emptyDir: {}}]
YAML
)
  for _ in $(seq 300); do
    state=$(k -n "$RNS" get "$job" -o jsonpath='{.status.succeeded}/{.status.failed}')
    case "$state" in 1/*|*/1) break ;; esac; sleep 2
  done
  [ "${state%%/*}" = 1 ] || fail feast-seed "seed Job did not succeed ($state): $(k -n "$RNS" logs "$job" --tail=2 2>&1 | tail -1 | cut -c1-200)"
  out=$(k -n "$RNS" logs "$job" | tail -1)
  jq -r .registry_b64 <<<"$out" | base64 -d > "$DIR/registry.db" || fail feast-seed "no registry in the seed output"
  if grep -qaF "$(synthetic P1)" "$DIR/registry.db" || grep -qaF "$(synthetic PCUR)" "$DIR/registry.db"; then fail feast-seed "the registry contains the Redis password"; fi
  k -n "$RNS" create configmap feast-registry --from-file=registry.db="$DIR/registry.db" --dry-run=client -o yaml | own \
    | { if k -n "$RNS" get configmap feast-registry >/dev/null 2>&1; then k replace -f -; else k create -f -; fi; } >/dev/null
  jq -c .feature_names <<<"$out"
}

# ======================================================================== phases
phase_preflight() {  # READ-ONLY
  local found base nodes
  for chart in vault-0.34.1.tgz:df4c37fac32496892ff1e1d6fd7954d3a402cc7b1b2b4c2226af11b8a57564c4 \
               external-secrets-2.14.0.tgz:0a6d9e91b1ff50adfdbf24b50b582babfa7c5b7df42b21974ca4b3fb88ee5e34; do
    [ "$(sha256sum "${CHARTS:?CHARTS}/${chart%%:*}" | cut -d' ' -f1)" = "${chart#*:}" ] || fail chart-digest "${chart%%:*}"
  done
  record chart-digests '{"result": "PASS", "charts": ["vault-0.34.1", "external-secrets-2.14.0"]}'
  nodes=$(k get nodes -o json | jq '.items | length') || fail cluster "nodes unreadable"
  [ "$nodes" -gt 0 ] || fail cluster "cluster asleep (0 nodes): wake it with the approved cloud-lab procedure first"
  k get crd certificates.cert-manager.io issuers.cert-manager.io >/dev/null || fail cert-manager "cert-manager CRDs missing"
  [ "$(k -n cert-manager get deploy -o json | jq '[.items[] | select((.status.availableReplicas // 0) > 0)] | length')" -ge 3 ] \
    || fail cert-manager "cert-manager deployments not available"
  record cluster "$(jq -cn --arg c "$CTX" --argjson n "$nodes" '{result: "PASS", context: $c, nodes: $n, cert_manager: "available"}')"
  found=$(collisions) || fail collisions "a query failed"
  none_found "$found" || fail collisions "already present: $found"
  record no-collisions "$(jq -c '{result: "PASS"} + .' <<<"$found")"
  if [ -z "$KIND" ]; then
    python3 "$root/scripts/vault/gke_preflight.py" --mode preinstall --kubeconfig "$KUBECONFIG" --context "$CTX" --out "$DIR/preflight" >/dev/null \
      || fail gke-preflight "not READY: see $DIR/preflight/gates-preinstall.jsonl"
    record gke-preflight '{"result": "PASS", "verdict": "VAULT_GKE_PREFLIGHT_READY"}'
  fi
  base=$(snapshot) || fail baseline "snapshot query failed"
  printf '%s\n' "$base" > "$DIR/baseline.json"
  record baseline "$(jq -c '{result: "RECORDED", releases: (.helm_releases | length), loadbalancers: .loadbalancer_services,
    feature_api: .feature_api, edge_gateway_service_type: .edge_gateway_service_type}' <<<"$base")"
}

unsealer_up() {  # kind only: the throwaway transit Vault that stands in for Cloud KMS (SIMULATED_AUTO_UNSEAL)
  local out pf
  create <<YAML
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: {name: vault-unsealer-tls, namespace: $VNS}
spec:
  secretName: vault-unsealer-tls
  duration: 24h
  privateKey: {algorithm: ECDSA, size: 256}
  dnsNames: [vault-unsealer, vault-unsealer.$VNS.svc, vault-unsealer.$VNS.svc.cluster.local, localhost]
  ipAddresses: ["127.0.0.1"]
  issuerRef: {name: vault-ca, kind: Issuer}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: vault-unsealer-config, namespace: $VNS}
data:
  unsealer.hcl: |
    ui = false
    disable_mlock = true
    listener "tcp" {
      address         = "[::]:8200"
      tls_cert_file   = "/tls/tls.crt"
      tls_key_file    = "/tls/tls.key"
      tls_min_version = "tls12"
    }
    storage "inmem" {}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: vault-unsealer, namespace: $VNS}
spec:
  replicas: 1
  selector: {matchLabels: {app.kubernetes.io/name: vault-unsealer}}
  template:
    metadata: {labels: {app.kubernetes.io/name: vault-unsealer}}
    spec:
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 100, runAsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: vault
          image: hashicorp/vault:2.0.4@sha256:5be49781ecf78bfe775c5309c6a4d9f4e9e040b6c885c99eb2b12fb69855e1a2
          command: [vault, server, -config=/config/unsealer.hcl]
          $container_security
          ports: [{containerPort: 8200}]
          readinessProbe: {tcpSocket: {port: 8200}, periodSeconds: 2}
          resources: {requests: {cpu: 20m, memory: 64Mi}, limits: {cpu: 200m, memory: 128Mi}}
          volumeMounts: [{name: config, mountPath: /config}, {name: tls, mountPath: /tls}, {name: tmp, mountPath: /tmp}]
      volumes:
        - {name: config, configMap: {name: vault-unsealer-config}}
        - {name: tls, secret: {secretName: vault-unsealer-tls}}
        - {name: tmp, emptyDir: {}}
---
apiVersion: v1
kind: Service
metadata: {name: vault-unsealer, namespace: $VNS}
spec:
  selector: {app.kubernetes.io/name: vault-unsealer}
  ports: [{port: 8200, targetPort: 8200}]
YAML
  k -n "$VNS" wait --for=condition=Ready certificate/vault-unsealer-tls --timeout=120s >/dev/null || fail unsealer "certificate not issued"
  k -n "$VNS" rollout status deploy/vault-unsealer --timeout=180s >/dev/null || fail unsealer "unsealer not running"
  kubectl --context "$CTX" -n "$VNS" port-forward deploy/vault-unsealer "$UPORT:8200" >/dev/null 2>&1 & pf=$!
  for _ in $(seq 20); do curl -s --cacert "$PRIV/ca.crt" -o /dev/null "https://127.0.0.1:$UPORT/v1/sys/health" && break; sleep 1; done
  out=$(python3 "$root/scripts/vault/rehearsal/unsealer.py" bootstrap --address "https://127.0.0.1:$UPORT" --ca-file "$PRIV/ca.crt" \
    --keys-out "$PRIV/unsealer-keys.json" --token-out "$PRIV/unsealer-token.json") || { kill "$pf"; fail unsealer "bootstrap failed"; }
  kill "$pf" 2>/dev/null || true
  jq -j .token "$PRIV/unsealer-token.json" | k -n "$VNS" create secret generic vault-unsealer-token --from-file=token=/dev/stdin \
    --dry-run=client -o yaml | create
  record unsealer "$(jq -c '{result: "PASS"} + . + {note: "kind stand-in for Cloud KMS: transit key autounseal, token limited to encrypt/decrypt"}' <<<"$out")"
}

phase_vault() {  # MUTATING (Kubernetes): RB step 2 with the official files, in a namespace proven new
  local found pvcs pvs image sans out
  # GKE needs the P4 Cloud KMS key ring, key, GSA and bindings and their read-only preflight gates (not implemented):
  # without them the gcpckms seal cannot start Vault. Refuse before creating anything.
  [ -n "$KIND" ] || fail kms-prerequisite "Vault V2 on GKE needs the P4 Cloud KMS resources and preflight gates; nothing was created"
  found=$(collisions) || fail collisions "a query failed"; none_found "$found" || fail collisions "already present: $found"
  create < "$root/infra/kubernetes/vault/namespace.yaml"
  create < "$root/infra/kubernetes/vault/certificate.yaml"
  k -n "$VNS" wait --for=condition=Ready certificate/vault-ca certificate/vault-server-tls --timeout=180s >/dev/null \
    || fail certificates "cert-manager did not issue vault-ca and vault-server-tls"
  k -n "$VNS" get secret vault-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > "$PRIV/ca.crt"
  k -n "$VNS" get secret vault-server-tls -o jsonpath='{.data.tls\.crt}' | base64 -d > "$PRIV/server.crt"
  openssl verify -CAfile "$PRIV/ca.crt" "$PRIV/server.crt" >/dev/null 2>&1 || fail certificates "server certificate not signed by vault-ca"
  sans=$(openssl x509 -in "$PRIV/server.crt" -noout -ext subjectAltName | tail -1 | tr -d ' ')
  case ",$sans," in *",DNS:vault.$VNS.svc.cluster.local,"*) ;; *) fail certificates "SANs miss the ESO server name" ;; esac
  record certificates "$(jq -cn --arg s "$sans" '{result: "PASS", chain: "vault-server-tls <- vault-ca", sans: ($s | split(","))}')"
  [ -z "$KIND" ] || unsealer_up
  h install vault "$CHARTS/vault-0.34.1.tgz" -n "$VNS" -f "$root/infra/kubernetes/vault/values-gke.yaml" \
    ${KIND:+-f "$root/infra/kubernetes/vault/values-local.yaml"} >/dev/null
  create < "$root/infra/kubernetes/vault/network-policy.yaml"
  create < "$root/infra/kubernetes/vault/admin-serviceaccount.yaml"
  k -n "$VNS" wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=300s >/dev/null || fail vault-pod "vault-0 not Running"
  image=$(k -n "$VNS" get pod vault-0 -o jsonpath='{.status.containerStatuses[0].imageID}')
  case "$image" in *sha256:5be49781ecf78bfe775c5309c6a4d9f4e9e040b6c885c99eb2b12fb69855e1a2) ;; *) fail vault-image "unexpected image $image" ;; esac
  pvcs=$(k -n "$VNS" get pvc -o json | jq -c '[.items[] | {name: .metadata.name, phase: .status.phase, storage_class: .spec.storageClassName,
    size: .status.capacity.storage, volume: .spec.volumeName}]')
  [ "$(jq '[.[] | select(.phase == "Bound")] | length' <<<"$pvcs")" = 2 ] || fail vault-volumes "expected 2 Bound PVCs: $pvcs"
  pvs=$(for v in $(jq -r '.[].volume' <<<"$pvcs"); do k get pv "$v" -o json; done | jq -cs '[.[] | {name: .metadata.name,
    reclaim: .spec.persistentVolumeReclaimPolicy, disk: (.spec.csi.volumeHandle // null), claim: .spec.claimRef.name}]')
  printf '%s\n' "$pvs" > "$DIR/volumes.json"       # names only; cleanup checks these disks are gone
  record vault-runtime "$(jq -cn --argjson pvcs "$pvcs" --argjson pvs "$pvs" \
    --argjson pdb "$(k -n "$VNS" get pdb -o json | jq '.items | length')" \
    --argjson svc "$(k -n "$VNS" get svc -o json | jq -c '[.items[] | {name: .metadata.name, type: .spec.type}]')" \
    '{result: "PASS", image: "hashicorp/vault 2.0.4 (pinned digest)", pvcs: $pvcs, volumes: $pvs, pdbs: $pdb, services: $svc}')"
  jq -e '.services | all(.type == "ClusterIP")' <<<"$(tail -1 "$EV")" >/dev/null || fail vault-services "a non-ClusterIP Vault Service"
  [ "$(k -n "$VNS" get pdb -o json | jq '.items | length')" = 0 ] || fail vault-pdb "a PodDisruptionBudget exists"
  open_forward
  out=$(status_is false true)
  [ "$(jq -r .seal_type <<<"$out")" = "$(seal_kind)" ] || fail vault-seal "seal type $(jq -r .seal_type <<<"$out"), expected $(seal_kind)"
  k -n "$VNS" wait --for=condition=Ready pod/vault-0 --timeout=120s >/dev/null || fail vault-ready "an uninitialised Vault must be Ready (uninitcode=204)"
  record vault-uninitialised "$(jq -c --arg l "$AUTO" --arg r "$(pod_ready)" '. + {pod_ready: $r, label: $l}' <<<"$out")"
  out=$(curl -s --cacert "$PRIV/ca.crt" -o /dev/null -w '{"https_status": %{http_code}, "ssl_verify_result": %{ssl_verify_result}}' \
    "https://127.0.0.1:$PORT/v1/sys/health?uninitcode=200&sealedcode=200") || fail vault-tls "HTTPS with the vault-ca CA failed"
  jq -e '.https_status == 200 and .ssl_verify_result == 0' <<<"$out" >/dev/null || fail vault-tls "$out"
  record vault-tls "$out"
}

phase_init() {  # MUTATING (Vault): RB step 3 with SYNTHETIC recovery material in REHEARSAL_DIR/private; no manual unseal
  local out s
  open_forward
  status_is false true >/dev/null
  out=$(vault init --keys-out "$PRIV/vault-keys.json") || fail vault-init "init failed: read the REFUSED line; never re-run init blindly"
  jq -e '.key_type == "recovery" and .shares == 3 and .threshold == 2' <<<"$out" >/dev/null || fail vault-init "not a 3/2 recovery-key init: $out"
  record vault-init "$(jq -c --arg l "$AUTO" '. + {label: $l}' <<<"$out")"
  [ "$(jq '.recovery_keys_base64 | length' "$PRIV/vault-keys.json")" = 3 ] && [ "$(jq '.keys_base64 | length' "$PRIV/vault-keys.json")" = 0 ] \
    || fail vault-init "the key file does not hold exactly 3 recovery keys and no unseal key"
  s=$(wait_unsealed 120) || fail auto-unseal "Vault did not unseal itself within 120 s of init"
  k -n "$VNS" wait --for=condition=Ready pod/vault-0 --timeout=120s >/dev/null || fail auto-unseal "unsealed but the pod is not Ready"
  record auto-unsealed "$(jq -c --arg l "$AUTO" --arg r "$(pod_ready)" '{result: "PASS", initialized, sealed, seal_type, recovery_seal, pod_ready: $r,
    manual_unseal: "none", label: $l}' <<<"$s")"
  if vault unseal --keys-file "$PRIV/vault-keys.json" >/dev/null 2>"$PRIV/probe.err"; then fail unseal-refused "manual unseal ran on an auto-unseal Vault"; fi
  grep -q "vault_auto_unseal" "$PRIV/probe.err" || fail unseal-refused "unexpected failure: $(head -c 200 "$PRIV/probe.err")"
  record manual-unseal-refused '{"result": "denied", "cli": "REFUSED vault_auto_unseal", "keys_submitted": 0}'
}

phase_ceremony() {  # MUTATING (Vault): K15, the authenticated root-generation ceremony with RECOVERY keys
  local out K=$PRIV/vault-keys.json
  open_forward; status_is true false >/dev/null
  k -n "$VNS" create token vault-admin --audience vault --duration 10m \
    | vault login --role vault-admin --token-out "$PRIV/admin-login.json" >/dev/null || fail admin-login "vault-admin login failed"
  (umask 077; jq '{seal_type, keys_base64: [], recovery_keys_base64: [.recovery_keys_base64[0]]}' "$K" > "$PRIV/one-recovery-key.json"
   # A forged share is only detectable at the threshold: one forged share plus one genuine share.
   jq '{seal_type, keys_base64: [], recovery_keys_base64: [(.recovery_keys_base64[0] | @base64d | explode | map(255 - .) | implode | @base64),
     .recovery_keys_base64[1]]}' "$K" > "$PRIV/invalid-recovery-key.json")
  if vault generate-root --keys-file "$PRIV/one-recovery-key.json" --token-file "$PRIV/admin-login.json" \
       --token-out "$PRIV/root-one.json" >/dev/null 2>"$PRIV/probe.err"; then fail one-key "one recovery key generated a root token"; fi
  grep -q vault_generate_root_incomplete "$PRIV/probe.err" && [ ! -e "$PRIV/root-one.json" ] || fail one-key "unexpected: $(head -c 200 "$PRIV/probe.err")"
  record one-recovery-key-insufficient '{"result": "denied", "keys": 1, "threshold": 2, "attempt": "cancelled", "token": "none"}'
  if vault generate-root --keys-file "$PRIV/invalid-recovery-key.json" --token-file "$PRIV/admin-login.json" \
       --token-out "$PRIV/root-bad.json" >/dev/null 2>"$PRIV/probe.err"; then fail invalid-key "an invalid recovery key generated a root token"; fi
  [ ! -e "$PRIV/root-bad.json" ] && grep -qE "returned (400|500)" "$PRIV/probe.err" || fail invalid-key "unexpected: $(head -c 200 "$PRIV/probe.err")"
  record invalid-recovery-key-rejected "$(jq -cn --arg s "$(grep -oE 'returned [0-9]+' "$PRIV/probe.err" | head -1)" \
    '{result: "denied", shares: "1 forged + 1 genuine (threshold reached)", vault: $s, token: "none"}')"
  out=$(vault generate-root --keys-file "$K" --token-file "$PRIV/admin-login.json" --token-out "$PRIV/root-ceremony.json") \
    || fail ceremony "two recovery keys did not complete the ceremony"
  vault describe --token-file "$PRIV/root-ceremony.json" > "$PRIV/describe-ceremony.json" || fail ceremony "the generated root cannot read the configuration"
  out=$(vault retire-root --token-file "$PRIV/root-ceremony.json") || fail ceremony "retire-root failed"
  if vault configure --token-file "$PRIV/root-ceremony.json" >/dev/null 2>"$PRIV/probe.err"; then fail ceremony "the retired root still works"; fi
  grep -q "returned 403" "$PRIV/probe.err" || fail ceremony "unexpected: $(head -c 200 "$PRIV/probe.err")"
  rm -f "$PRIV/one-recovery-key.json" "$PRIV/invalid-recovery-key.json" "$PRIV/root-ceremony.json" "$PRIV/describe-ceremony.json" "$PRIV/admin-login.json"
  record recovery-key-ceremony '{"result": "PASS", "recovery_keys": 3, "threshold": 2, "login": "vault-admin (Kubernetes auth)",
    "generated_root": "read the configuration, then retired and rejected (403)", "temporary_files": "removed", "label": "'"$AUTO"'"}'
}

restart_vault() {  # [noforward]: delete vault-0; reopen the port-forward once the new pod runs (Ready is NOT awaited: it means unsealed)
  stop_forward
  k -n "$VNS" delete pod vault-0 --wait=true >/dev/null
  k -n "$VNS" wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=300s >/dev/null || fail restart "vault-0 not Running again"
  [ "${1:-}" = noforward ] || open_forward
}
unsealer_offline() {  # kind only: the stand-in for "Cloud KMS unreachable" (the unsealer pod and its key stay alive)
  k -n "$VNS" patch service vault-unsealer -p '{"spec":{"selector":{"app.kubernetes.io/name":"vault-unsealer-offline"}}}' >/dev/null
  [ -z "$(k -n "$VNS" get endpointslice -l kubernetes.io/service-name=vault-unsealer -o jsonpath='{.items[*].endpoints[*].addresses[*]}')" ] \
    || fail unsealer-offline "the unsealer Service still has endpoints"
}
unsealer_online() { k -n "$VNS" patch service vault-unsealer -p '{"spec":{"selector":{"app.kubernetes.io/name":"vault-unsealer"}}}' >/dev/null; }
restarts() { k -n "$VNS" get pod vault-0 -o jsonpath='{.status.containerStatuses[0].restartCount}'; }
seal_down_observed() {  # <seconds>: with its seal unreachable Vault never serves unsealed and never reports Ready
  # Observed on kind (Vault 2.0.4, transit seal): the listener never opens; after ~110 s the process exits with
  # "error parsing Seal configuration: context deadline exceeded" and kubelet restarts it with back-off.
  local before seen_exit=false api=unreachable s
  before=$(restarts)
  for _ in $(seq "$1"); do
    [ "$(pod_ready)" != True ] || fail seal-down "Vault reports Ready although its seal is unreachable"
    if s=$(curl -s --max-time 2 --cacert "$PRIV/ca.crt" "https://127.0.0.1:$PORT/v1/sys/seal-status" 2>/dev/null) && [ -n "$s" ]; then
      [ "$(jq -r .sealed <<<"$s")" = true ] || fail seal-down "Vault unsealed although its seal is unreachable"; api=sealed
    fi
    sleep 1
  done
  if [ "$(restarts)" -gt "$before" ] && k -n "$VNS" logs vault-0 --previous 2>/dev/null | grep -q "error parsing Seal configuration"; then seen_exit=true; fi
  jq -cn --arg a "$api" --arg r "$(pod_ready)" --argjson b "$before" --argjson n "$(restarts)" --argjson x "$seen_exit" \
    '{api: $a, pod_ready: $r, container_restarts: ($n - $b), exited_on_seal_setup: $x}'
}
recover_from_outage() {  # after unsealer_online: kubelet's next start must unseal by itself; one pod restart only if it never does.
  # Sets RECOVERED (JSON). Never call it inside $(...): it opens the port-forward, whose PID must stay in this shell.
  local s restarted=false before
  before=$(restarts)
  if ! k -n "$VNS" wait --for=condition=Ready pod/vault-0 --timeout=360s >/dev/null 2>&1; then
    restarted=true; restart_vault noforward
    k -n "$VNS" wait --for=condition=Ready pod/vault-0 --timeout=360s >/dev/null || fail seal-recovered "Vault did not unseal after the seal came back"
  fi
  open_forward
  s=$(status_is true false)
  RECOVERED=$(jq -c --argjson r "$restarted" --argjson n "$(( $(restarts) - before ))" '{initialized, sealed, manual_pod_delete: $r,
    kubelet_restarts_while_recovering: $n, manual_unseal: "none"}' <<<"$s")
}

phase_seal() {  # MUTATING (Vault, kind: the unsealer Service): K13/K14 at the Vault level, before any consumer exists
  local out s OP=$PRIV/operator-token.json
  [ -n "$KIND" ] || fail seal-kind-only "the unsealer outage drill is kind-only; never applied to Cloud KMS"
  open_forward; status_is true false >/dev/null
  out=$(printf 'synthetic-canary-%s' "$(openssl rand -hex 8)" | vault put --token-file "$OP" --path ml-platform/rehearsal-seal-canary --key value --stdin) \
    || fail canary "put failed"
  restart_vault
  s=$(wait_unsealed 120) || fail restart-auto-unseal "Vault did not unseal itself after a restart"
  k -n "$VNS" wait --for=condition=Ready pod/vault-0 --timeout=120s >/dev/null || fail restart-auto-unseal "unsealed but not Ready"
  [ "$(vault versions --token-file "$OP" --path ml-platform/rehearsal-seal-canary | jq -r .current_version)" = 1 ] \
    || fail restart-auto-unseal "data or operator token lost across the restart"
  record restart-auto-unsealed "$(jq -c --arg l "$AUTO" --arg r "$(pod_ready)" '{result: "PASS", initialized, sealed, pod_ready: $r,
    manual_unseal: "none", raft_data: "canary version 1 and operator token intact", label: $l}' <<<"$s")"
  unsealer_offline
  restart_vault noforward
  open_forward_nowait
  s=$(seal_down_observed 150)
  [ "$(jq -r .exited_on_seal_setup <<<"$s")" = true ] || fail seal-down "Vault did not fail its seal setup within 150 s: $s"
  record seal-unreachable-never-unsealed "$(jq -c --arg l "$AUTO" '{result: "PASS"} + . + {observed_seconds: 150, label: $l}' <<<"$s")"
  unsealer_online
  recover_from_outage; out=$RECOVERED
  [ "$(vault versions --token-file "$OP" --path ml-platform/rehearsal-seal-canary | jq -r .current_version)" = 1 ] || fail seal-recovered "data lost"
  record seal-back-auto-unsealed "$(jq -c --arg l "$AUTO" '{result: "PASS"} + . + {raft_data: "canary version 1", label: $l}' <<<"$out")"
}

phase_configure() {  # MUTATING (Vault): RB step 4, then the rehearsal role; root retired at the end
  local out K=$PRIV/vault-keys.json OP=$PRIV/operator-token.json
  open_forward; status_is true false >/dev/null
  out=$(vault configure --keys-file "$K") || fail configure-official "configure failed"; record configure-official "$out"
  out=$(vault configure --keys-file "$K" --config "$root/scripts/vault/rehearsal/bootstrap.json") || fail configure-rehearsal "configure failed"
  record configure-rehearsal "$out"
  vault describe --keys-file "$K" > "$DIR/vault-describe.json" || fail describe "describe failed"
  jq -e '.secret_engines["ewallet/"] == {type: "kv", version: "2"} and .auth_methods["kubernetes/"] == "kubernetes"
         and (.kubernetes_roles | keys) == ["eso-ewallet-reader", "eso-vault-rehearsal", "vault-admin"]
         and .kubernetes_roles["eso-vault-rehearsal"].bound_service_account_namespaces == ["external-secrets"]
         and .kubernetes_roles["eso-ewallet-reader"].bound_service_account_namespaces == ["external-secrets"]
         and .kubernetes_roles["eso-ewallet-reader"].audience == "vault"' "$DIR/vault-describe.json" >/dev/null \
    || fail describe "live configuration differs from the bootstrap"
  record describe "$(jq -c '{result: "PASS", engines: .secret_engines, auth: .auth_methods, audit: .audit_devices,
    roles: (.kubernetes_roles | keys), policies: (.acl_policies | keys)}' "$DIR/vault-describe.json")"
  out=$(vault operator-token --keys-file "$K" --token-out "$OP") || fail operator-token "operator-token failed"; record operator-token "$out"
  out=$(vault retire-root --keys-file "$K") || fail retire-root "retire-root failed"; record retire-root "$out"
  if vault configure --keys-file "$K" >/dev/null 2>"$PRIV/probe.err"; then fail root-rejected "the retired root token still works"; fi
  grep -q "returned 403" "$PRIV/probe.err" || fail root-rejected "unexpected failure: $(head -c 200 "$PRIV/probe.err")"
  record root-rejected '{"result": "denied", "http_status": 403, "probe": "configure"}'
  if vault revoke-role --token-file "$OP" --role eso-vault-rehearsal >/dev/null 2>"$PRIV/probe.err"; then fail operator-scope "operator token administered a role"; fi
  grep -q "returned 403" "$PRIV/probe.err" || fail operator-scope "unexpected failure"
  record operator-cannot-administer '{"result": "denied", "http_status": 403, "attempt": "delete auth role"}'
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj /CN=untrusted \
    -keyout "$PRIV/untrusted.key" -out "$PRIV/untrusted.crt" 2>/dev/null
  if curl -s --cacert "$PRIV/untrusted.crt" -o /dev/null "https://127.0.0.1:$PORT/v1/sys/health"; then fail tls-wrong-ca "an untrusted CA was accepted"; fi
  record tls-wrong-ca-rejected '{"result": "denied"}'
}

phase_eso() {  # MUTATING (Kubernetes): RB step 6 with the official values; the consumer namespace is new
  local deploys objs
  printf 'apiVersion: v1\nkind: Namespace\nmetadata: {name: %s, labels: {pod-security.kubernetes.io/enforce: restricted, pod-security.kubernetes.io/warn: restricted}}\n' "$RNS" | create
  k -n "$RNS" create configmap vault-ca --from-file=ca.crt="$PRIV/ca.crt" --dry-run=client -o yaml | create
  create < "$root/infra/kubernetes/external-secrets/namespace.yaml"
  h install external-secrets "$CHARTS/external-secrets-2.14.0.tgz" -n "$ENS" -f "$root/infra/kubernetes/external-secrets/values.yaml" \
    --wait --timeout 5m >/dev/null || fail eso-install "helm install failed"
  deploys=$(k -n "$ENS" get deploy -o json | jq -c '[.items[] | {name: .metadata.name, available: (.status.availableReplicas // 0)}]')
  [ "$(jq '[.[] | select(.available > 0)] | length' <<<"$deploys")" = 3 ] || fail eso-runtime "not 3 available deployments: $deploys"
  objs=$(k get "$CLUSTER_KINDS" -l app.kubernetes.io/instance=external-secrets -o json | jq -c '[.items[] | "\(.kind)/\(.metadata.name)"]')
  jq -cn --argjson o "$objs" --argjson c "$(k get crd -o json | jq -c '[.items[].metadata.name | select(endswith(".external-secrets.io"))]')" \
    '{cluster_objects: $o, crds: $c}' > "$DIR/eso-cluster-objects.json"
  record eso-runtime "$(jq -cn --argjson d "$deploys" --argjson o "$objs" --argjson n "$(jq '.crds | length' "$DIR/eso-cluster-objects.json")" \
    --argjson cs "$(k get clustersecretstore,clusterexternalsecret -o json | jq '.items | length')" \
    '{result: "PASS", deployments: $d, crds: $n, cluster_objects: $o, cluster_scoped_stores: $cs}')"
}

phase_consumer() {  # MUTATING: synthetic Vault entry -> ESO -> Secret -> real Redis authentication
  local p1 out owner keys
  open_forward; status_is true false >/dev/null
  [ -f "$PRIV/synthetic.env" ] && fail synthetic "consumer phase already ran"
  p1=$(openssl rand -hex 20); (umask 077; printf 'P1=%s\nP2=%s\n' "$p1" "$(openssl rand -hex 20)" > "$PRIV/synthetic.env")
  out=$(conn "$p1" | vault put --token-file "$PRIV/operator-token.json" --path feature-api/rehearsal-redis --key connection_string --stdin) \
    || fail seed "put failed"; record seed "$out"
  printf '%s' "$p1" | k -n "$RNS" create secret generic rehearsal-redis-server --from-file=password=/dev/stdin --dry-run=client -o yaml | create
  create <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: redis, namespace: $RNS}
spec:
  replicas: 1
  selector: {matchLabels: {app.kubernetes.io/name: rehearsal-redis}}
  template:
    metadata: {labels: {app.kubernetes.io/name: rehearsal-redis}}
    spec:
      automountServiceAccountToken: false
      $restricted
      containers:
        - name: redis
          image: $REDIS_IMAGE
          $container_security
          env: [{name: REDIS_PASSWORD, valueFrom: {secretKeyRef: {name: rehearsal-redis-server, key: password}}}]
          command: [sh, -c, 'exec redis-server --requirepass "\$REDIS_PASSWORD" --save "" --appendonly no']
          ports: [{containerPort: 6379}]
          readinessProbe: {tcpSocket: {port: 6379}, periodSeconds: 2}
          resources: {requests: {cpu: 10m, memory: 16Mi}, limits: {cpu: 100m, memory: 64Mi}}
          volumeMounts: [{name: data, mountPath: /data}]
      volumes: [{name: data, emptyDir: {}}]
---
apiVersion: v1
kind: Service
metadata: {name: redis, namespace: $RNS}
spec:
  selector: {app.kubernetes.io/name: rehearsal-redis}
  ports: [{port: 6379, targetPort: 6379}]
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: eso-vault-reader, namespace: $RNS}
automountServiceAccountToken: false
YAML
  secret_store vault eso-vault-reader vault eso-vault-rehearsal vault-ca
  external_secret feature-api-redis-vault vault feature-api/rehearsal-redis connection_string
  k -n "$RNS" wait --for=condition=Ready secretstore/vault --timeout=120s >/dev/null || fail store "SecretStore not Ready: $(es_error vault)"
  es_wait "$RNS" feature-api-redis-vault True 120 || fail sync "not synced: $(es_error feature-api-redis-vault)"
  owner=$(k -n "$RNS" get secret feature-api-redis-vault -o jsonpath='{.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}')
  keys=$(k -n "$RNS" get secret feature-api-redis-vault -o json | jq -c '.data | keys')
  [ "$owner" = ExternalSecret/feature-api-redis-vault ] || fail sync "target owned by $owner"
  [ "$(secret_sha "$RNS" feature-api-redis-vault connection_string)" = "$(sha "$(conn "$p1")")" ] || fail sync "synced value differs from Vault v1"
  record secret-synced "$(jq -cn --arg c "$(es_condition "$RNS" feature-api-redis-vault)" --arg o "$owner" --argjson k "$keys" \
    --arg s "$(k -n "$RNS" get secretstore vault -o jsonpath='{.status.conditions[0].reason}')" \
    '{result: "PASS", store: $s, externalsecret: $c, owner: $o, keys: $k, value: "equal to Vault v1 (hash prefix)"}')"
  k -n "$RNS" rollout status deploy/redis --timeout=180s >/dev/null || fail redis "synthetic Redis not Ready"
  out=$(consume); [ "$out" = OK ] || fail consumer "consumer result $out with the ESO Secret"
  record consumer '{"result": "PASS", "check": "redis-cli PING + SET/GET round trip with the ESO-managed connection_string"}'
}

phase_negative() {  # MUTATING (test objects only, label ewallet.io/vault-rehearsal-negative=true)
  local code
  k -n "$RNS" create configmap rehearsal-untrusted-ca --from-file=ca.crt="$PRIV/untrusted.crt" --dry-run=client -o yaml | create
  printf 'apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: rehearsal-intruder, namespace: %s}\nautomountServiceAccountToken: false\n' "$RNS" | create
  secret_store neg-wrong-sa rehearsal-intruder vault eso-vault-rehearsal vault-ca true
  secret_store neg-wrong-role eso-vault-reader vault eso-ml-platform vault-ca true
  secret_store neg-wrong-aud eso-vault-reader kubernetes eso-vault-rehearsal vault-ca true
  secret_store neg-wrong-ca eso-vault-reader vault eso-vault-rehearsal rehearsal-untrusted-ca true
  external_secret neg-path vault feature-api/feast-redis connection_string ewallet.io/vault-rehearsal-negative
  external_secret neg-missing-key vault feature-api/rehearsal-redis no_such_property ewallet.io/vault-rehearsal-negative
  for s in wrong-sa wrong-role wrong-aud wrong-ca; do
    external_secret "neg-$s" "neg-$s" feature-api/rehearsal-redis connection_string ewallet.io/vault-rehearsal-negative
  done
  negative unauthorized-path neg-path vault-permission-denied
  negative missing-key neg-missing-key vault-key-missing
  negative wrong-service-account neg-wrong-sa service-account-not-bound-to-role
  negative role-of-another-namespace neg-wrong-role role-bound-to-other-namespace
  negative wrong-audience neg-wrong-aud token-audience-rejected
  negative untrusted-ca neg-wrong-ca tls-untrusted-ca
  # NetworkPolicy: a pod in the rehearsal namespace must not reach Vault (only the ESO controller may).
  # BusyBox nc has no -z: `nc -w 5 HOST PORT </dev/null` exits 0 on connect, 1 on refusal, timeout or DNS error.
  # The same pod must first reach the rehearsal Redis, so a DNS or tooling failure never counts as BLOCKED.
  create <<YAML
apiVersion: v1
kind: Pod
metadata: {name: neg-network-probe, namespace: $RNS, labels: {ewallet.io/vault-rehearsal-negative: "true"}}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  $restricted
  containers:
    - name: probe
      image: $REDIS_IMAGE
      $container_security
      command: [sh, -c, 'nc -w 5 redis.$RNS.svc.cluster.local 6379 </dev/null >/dev/null 2>&1 || { echo CONTROL_FAILED; exit 0; }; nc -w 5 vault.$VNS.svc.cluster.local 8200 </dev/null >/dev/null 2>&1 && echo REACHABLE || echo BLOCKED']
YAML
  k -n "$RNS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/neg-network-probe --timeout=120s >/dev/null || fail network "probe did not finish"
  code=$(k -n "$RNS" logs neg-network-probe | tail -1)
  [ "$code" = BLOCKED ] || fail network "a non-ESO pod reached Vault: $code"
  record networkpolicy-blocks-other-pods '{"result": "denied", "from": "vault-rehearsal pod", "to": "vault:8200", "observed": "BLOCKED", "control": "same pod reached redis:6379"}'
  record no-public-endpoint "$(jq -cn --argjson v "$(k -n "$VNS" get svc -o json | jq -c '[.items[].spec.type] | unique')" \
    '{result: (if $v == ["ClusterIP"] then "PASS" else "FAIL" end), vault_service_types: $v}')"
  k -n "$RNS" delete externalsecret,secretstore,pod,serviceaccount,configmap -l ewallet.io/vault-rehearsal-negative=true --wait=true >/dev/null
  k -n "$RNS" delete serviceaccount rehearsal-intruder configmap rehearsal-untrusted-ca --ignore-not-found >/dev/null
  record negative-objects-removed '{"result": "PASS"}'
}

phase_lifecycle() {  # MUTATING (Vault: synthetic versions; Kubernetes: one Vault pod restart)
  local rv out p1 p2 before
  open_forward; status_is true false >/dev/null
  p1=$(synthetic P1); p2=$(synthetic P2); OP=$PRIV/operator-token.json
  # 1. A new KV version reaches the Secret and the consumer (v2: a wrong password, so the effect is visible).
  rv=$(es_synced_rv "$RNS" feature-api-redis-vault)
  out=$(conn "$p2" | vault put --token-file "$OP" --path feature-api/rehearsal-redis --key connection_string --stdin) || fail kv-v2 "put failed"
  force_sync feature-api-redis-vault; rv=$(synced_after feature-api-redis-vault "$rv") || fail kv-v2 "no new reconciliation"
  [ "$(secret_sha "$RNS" feature-api-redis-vault connection_string)" = "$(sha "$(conn "$p2")")" ] || fail kv-v2 "Secret not at v2"
  [ "$(consume)" = AUTH_FAILED ] || fail kv-v2 "the consumer did not use the new version"
  record kv-v2-propagated "$(jq -c '{result: "PASS", version: .version, consumer: "AUTH_FAILED (v2 is deliberately wrong: the new value was used)"}' <<<"$out")"
  out=$(conn "$p1" | vault put --token-file "$OP" --path feature-api/rehearsal-redis --key connection_string --stdin) || fail kv-v3 "put failed"
  force_sync feature-api-redis-vault; rv=$(synced_after feature-api-redis-vault "$rv") || fail kv-v3 "no new reconciliation"
  [ "$(consume)" = OK ] || fail kv-v3 "the consumer did not recover with v3"
  record kv-v3-restored "$(jq -cn --argjson o "$out" --argjson m "$(vault versions --token-file "$OP" --path feature-api/rehearsal-redis)" \
    '{result: "PASS", version: $o.version, metadata: $m, consumer: "OK"}')"
  # 2. Restart: Vault comes back sealed (Pod still Ready), ESO fails but keeps the Secret, manual unseal restores sync.
  before=$(secret_sha "$RNS" feature-api-redis-vault connection_string)
  stop_forward
  k -n "$VNS" delete pod vault-0 --wait=true >/dev/null
  k -n "$VNS" wait --for=condition=Ready pod/vault-0 --timeout=300s >/dev/null || fail restart "vault-0 not back"
  open_forward
  record restarted-sealed "$(jq -cn --arg r "$(pod_ready)" --argjson s "$(status_is true true)" '{result: "PASS", pod_ready: $r, sealed: $s.sealed}')"
  force_sync feature-api-redis-vault
  es_wait "$RNS" feature-api-redis-vault False 90 || fail sealed-sync "ESO did not report the sealed Vault"
  for _ in $(seq 30); do [ "$(es_error feature-api-redis-vault 3m)" = vault-sealed ] && break; sleep 2; done
  [ "$(secret_sha "$RNS" feature-api-redis-vault connection_string)" = "$before" ] || fail sealed-sync "the Secret changed while sealed"
  [ "$(consume)" = OK ] || fail sealed-sync "the consumer lost access while Vault was sealed"
  if h status "$SERVING" -n "$RNS" >/dev/null 2>&1; then open_feature_forward; expect_lookup while-sealed; fi
  record sealed-keeps-secret "$(jq -cn --arg c "$(es_condition "$RNS" feature-api-redis-vault)" --arg e "$(es_error feature-api-redis-vault 3m)" \
    '{result: "PASS", externalsecret: $c, error_class: $e, target_secret: "unchanged", consumer: "OK"}')"
  vault unseal --keys-file "$PRIV/vault-keys.json" >/dev/null || fail unseal "unseal failed"
  status_is true false >/dev/null
  rv=$(es_synced_rv "$RNS" feature-api-redis-vault); force_sync feature-api-redis-vault
  synced_after feature-api-redis-vault "$rv" >/dev/null || fail unseal-sync "ESO did not recover after unseal"
  [ "$(consume)" = OK ] || fail unseal-sync "consumer failed after unseal"
  record unsealed-sync-recovered "$(jq -cn --arg c "$(es_condition "$RNS" feature-api-redis-vault)" '{result: "PASS", externalsecret: $c, consumer: "OK"}')"
  compare_production production-unchanged
}

phase_feature_api() {  # MUTATING (rehearsal namespace only): the "before" state of the official G1
  local p1 names rv out
  [[ ${FEATURE_IMAGE:-} =~ ^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] \
    || fail feature-image "FEATURE_IMAGE must be the Feature API image pinned by digest"
  p1=$(synthetic P1)
  # The rehearsal's "existing" Secret, the stand-in for ml-platform/feast-online-redis: created
  # directly, holding the same value as Vault v1 (what G5 copies into Vault).
  printf '%s' "$(conn "$p1")" | k -n "$RNS" create secret generic "$LEGACY" --from-file=connection_string=/dev/stdin \
    --dry-run=client -o yaml | create
  rv=$(k -n "$RNS" get secret "$LEGACY" -o jsonpath='{.metadata.resourceVersion}'); printf '%s\n' "$rv" > "$DIR/legacy-resource-version"
  out=$(bash "$root/scripts/vault/shadow_compare.sh" "$CTX" "$RNS" "$LEGACY" feature-api-redis-vault) \
    || fail shadow-compare "shadow_compare.sh: $out"
  record shadow-compare "$(jq -cn --arg s "$out" '{result: "PASS", status: $s, script: "scripts/vault/shadow_compare.sh"}')"
  names=$(seed_registry)
  jq -cn --argjson n "$names" '{user_id: "u1", account_id: "a1", device_id: "d1", merchant_id: "m1",
    event_timestamp: "2026-05-31T23:57:00+00:00", feature_names: $n}' > "$DIR/lookup-request.json"
  record feast-seeded "$(jq -cn --argjson n "$names" '{result: "PASS", feature_names: $n, registry: "ConfigMap feast-registry (no credential inside)"}')"
  printf 'apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: rehearsal-feature-api, namespace: %s}\nautomountServiceAccountToken: false\n' "$RNS" | create
  serving install -f "$root/scripts/vault/rehearsal/serving-values.yaml" --set "featureApi.image=$FEATURE_IMAGE" \
    ${KIND:+--set featureApi.nodeSelector=null} || fail serving-install "helm install $SERVING failed"
  [ "$(secret_ref)" = "$LEGACY" ] || fail serving-install "Feature API reads $(secret_ref), expected $LEGACY"
  open_feature_forward
  expect_lookup baseline
  record feature-api-baseline "$(jq -cn --arg r "$(h -n "$RNS" history "$SERVING" --max 1 -o json | jq -r '.[0].revision')" \
    '{result: "PASS", release: "rehearsal-serving", revision: $r, secret: "rehearsal-feast-online-redis", class: "BASELINE_OK"}')"
}

phase_switch() {  # MUTATING: the official G8 command, an upgrade with --reuse-values and the unchanged overlay, then G9
  local before
  before=$(h -n "$RNS" history "$SERVING" --max 1 -o json | jq -r '.[0].revision')
  printf '%s\n' "$before" > "$DIR/serving-revision-before-switch"
  serving upgrade --reuse-values -f "$root/infra/helm/ewallet-serving/values-vault-eso.yaml" || fail switch "helm upgrade failed"
  [ "$(secret_ref)" = feature-api-redis-vault ] || fail switch "Feature API reads $(secret_ref)"
  if k -n "$RNS" get deploy feature-api -o json | jq -e '[.spec.template.spec.containers[0].env[].name] | any(startswith("EWALLET_VAULT_"))' >/dev/null; then
    fail switch "the pod would talk to Vault directly"; fi
  [ "$(k -n "$RNS" get secret "$LEGACY" -o jsonpath='{.metadata.resourceVersion}')" = "$(cat "$DIR/legacy-resource-version")" ] \
    || fail switch "the existing Secret changed"
  open_feature_forward
  expect_lookup switch
  record switched "$(jq -cn --arg b "$before" --arg a "$(h -n "$RNS" history "$SERVING" --max 1 -o json | jq -r '.[0].revision')" \
    '{result: "PASS", revision_before: $b, revision_after: $a, secret: "feature-api-redis-vault", g9_class: "BUSINESS_LOOKUP_PASS"}')"
}

phase_rollback() {  # MUTATING: the official G11 drill (RB 9 rollback, RB 8 again) and the final-state checks
  local rev
  rev=$(cat "$DIR/serving-revision-before-switch")
  h rollback "$SERVING" "$rev" -n "$RNS" --wait --timeout 10m >/dev/null || fail rollback "helm rollback to $rev failed"
  [ "$(secret_ref)" = "$LEGACY" ] || fail rollback "after rollback the Feature API reads $(secret_ref)"
  open_feature_forward
  expect_lookup g11-rollback
  serving upgrade --reuse-values -f "$root/infra/helm/ewallet-serving/values-vault-eso.yaml" || fail reupgrade "helm upgrade failed"
  [ "$(secret_ref)" = feature-api-redis-vault ] || fail reupgrade "after re-upgrade the Feature API reads $(secret_ref)"
  open_feature_forward
  expect_lookup g11-reupgrade
  # Final state after G11 (acceptance plan §4)
  [ "$(k -n "$RNS" get secretstore vault -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}')" = Valid ] || fail final-state "SecretStore not Valid"
  [ "$(es_condition "$RNS" feature-api-redis-vault)" = "True SecretSynced" ] || fail final-state "ExternalSecret not SecretSynced"
  [ "$(k -n "$RNS" get secret "$LEGACY" -o jsonpath='{.metadata.resourceVersion}')" = "$(cat "$DIR/legacy-resource-version")" ] \
    || fail final-state "the existing Secret changed"
  [ -z "$(k -n "$RNS" get externalsecret,secretstore,serviceaccount -l ewallet.io/vault-rehearsal-negative=true -o name)" ] \
    || fail final-state "negative-test objects remain"
  record final-state "$(jq -cn --argjson h "$(h -n "$RNS" history "$SERVING" -o json | jq -c '[.[] | {revision, description}]')" \
    '{result: "PASS", consumer: "feature-api-redis-vault", store: "Valid", externalsecret: "SecretSynced", existing_secret: "unchanged",
      negative_objects: "none", lookup: "BASELINE_OK", serving_history: $h}')"
  compare_production production-unchanged
}

phase_retain() {  # MUTATING: Retain, an accidental PVC deletion, and the runbook's Released-PV recovery
  local kv v claim rv
  open_forward; status_is true false >/dev/null
  check_volumes_before_delete bound
  kv=$(vault versions --token-file "$PRIV/operator-token.json" --path feature-api/rehearsal-redis | jq -r .current_version)
  printf '%s\n' "$kv" > "$DIR/kv-version"
  for v in $(jq -r '.[].name' "$DIR/volumes.json"); do
    k patch pv "$v" -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}' >/dev/null
    [ "$(k get pv "$v" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}')" = Retain ] || fail retain "PV $v not Retain"
  done
  record retain-set "$(jq -cn --arg kv "$kv" '{result: "PASS", reclaim: "Retain", kv_current_version: ($kv | tonumber)}')"
  fault retain-set
  stop_forward
  k -n "$VNS" scale statefulset vault --replicas=0 >/dev/null
  k -n "$VNS" wait --for=delete pod/vault-0 --timeout=300s >/dev/null 2>&1 || true
  k -n "$VNS" get pod vault-0 >/dev/null 2>&1 && fail retain "vault-0 still running"
  for claim in $(jq -r '.[].claim' "$DIR/volumes.json" | sort); do   # one by one: the tests stop between the two
    k -n "$VNS" delete pvc "$claim" --wait=true --timeout=300s >/dev/null
    fault "pvc-deleted:$claim"
  done
  for v in $(jq -r '.[].name' "$DIR/volumes.json"); do
    for _ in $(seq 60); do [ "$(k get pv "$v" -o jsonpath='{.status.phase}')" = Released ] && break; sleep 2; done
    [ "$(k get pv "$v" -o jsonpath='{.status.phase}')" = Released ] || fail retain "PV $v not Released after its PVC was deleted"
  done
  if [ -z "$KIND" ]; then
    for handle in $(jq -r '.[].disk' "$DIR/volumes.json"); do
      [ "$(disk_state "$handle")" = present ] || fail retain "disk ${handle##*/} not confirmed present after the PVC deletion"
    done
  fi
  record pvc-deleted-disk-kept "$(jq -cn --arg d "$([ -z "$KIND" ] && echo 'present (gcloud compute disks describe)' || echo 'kind local-path: not a cloud disk; data proven by the re-bound Vault below')" \
    '{result: "PASS", pv_phase: "Released", disks: $d}')"
  fault pvcs-released
  # Runbook step 2, *Recovery of a Released PV*: clear the claimRef, recreate the PVC with volumeName.
  for claim in $(jq -r '.[].claim' "$DIR/volumes.json" | sort); do
    rebind_volume "$(jq -r --arg c "$claim" '.[] | select(.claim == $c) | .name' "$DIR/volumes.json")" "$claim"
    fault "rebound:$claim"
  done
  k -n "$VNS" scale statefulset vault --replicas=1 >/dev/null
  k -n "$VNS" wait --for=condition=Ready pod/vault-0 --timeout=300s >/dev/null || fail retain "vault-0 not back"
  open_forward
  # An uninitialised Vault here would mean the data did not come back.
  record rebound-initialised-sealed "$(jq -c '{result: "PASS", initialized, sealed}' <<<"$(status_is true true)")"
  vault unseal --keys-file "$PRIV/vault-keys.json" >/dev/null || fail retain "unseal failed"
  status_is true false >/dev/null
  [ "$(vault versions --token-file "$PRIV/operator-token.json" --path feature-api/rehearsal-redis | jq -r .current_version)" = "$kv" ] \
    || fail retain "KV version differs after the recovery"
  rv=$(es_synced_rv "$RNS" feature-api-redis-vault); force_sync feature-api-redis-vault
  synced_after feature-api-redis-vault "$rv" >/dev/null || fail retain "ESO did not sync after the recovery"
  [ "$(consume)" = OK ] || fail retain "consumer failed after the recovery"
  open_feature_forward; expect_lookup after-retain-recovery
  record released-pv-recovered "$(jq -cn --arg kv "$kv" '{result: "PASS", kv_current_version: ($kv | tonumber), operator_token: "valid",
    externalsecret: "SecretSynced (new reconciliation)", consumer: "OK", feature_api: "BASELINE_OK", reclaim: "Retain"}')"
}

phase_after_wake() {  # MUTATING (unseal, reseed): after the approved GKE sleep and wake (on kind: after pod restarts)
  local s kv rv
  k -n "$VNS" wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=600s >/dev/null || fail after-wake "vault-0 not Running"
  open_forward
  s=$(vault status) || fail after-wake "status failed"
  record vault-after-wake "$(jq -c '{result: "PASS", initialized, sealed}' <<<"$s")"
  [ "$(jq -r .initialized <<<"$s")" = true ] || fail after-wake "Vault is not initialised: the data did not survive"
  if [ "$(jq -r .sealed <<<"$s")" = true ]; then vault unseal --keys-file "$PRIV/vault-keys.json" >/dev/null || fail after-wake "unseal failed"; fi
  status_is true false >/dev/null
  kv=$(cat "$DIR/kv-version")
  [ "$(vault versions --token-file "$PRIV/operator-token.json" --path feature-api/rehearsal-redis | jq -r .current_version)" = "$kv" ] \
    || fail after-wake "KV version differs after the wake"
  for v in $(jq -r '.[].name' "$DIR/volumes.json"); do
    [ "$(k get pv "$v" -o jsonpath='{.status.phase}/{.spec.persistentVolumeReclaimPolicy}')" = Bound/Retain ] || fail after-wake "PV $v not Bound/Retain"
  done
  rv=$(es_synced_rv "$RNS" feature-api-redis-vault); force_sync feature-api-redis-vault
  synced_after feature-api-redis-vault "$rv" >/dev/null || fail after-wake "ESO did not sync after the wake"
  [ "$(secret_sha "$RNS" feature-api-redis-vault connection_string)" = "$(sha "$(conn "$(synthetic P1)")")" ] || fail after-wake "synced value differs"
  # The synthetic Redis keeps nothing across a restart: seed it again, then restart the Feature API on the new registry.
  seed_registry >/dev/null
  k -n "$RNS" rollout restart deploy/feature-api >/dev/null
  k -n "$RNS" rollout status deploy/feature-api --timeout=600s >/dev/null || fail after-wake "Feature API rollout failed"
  [ "$(consume)" = OK ] || fail after-wake "consumer failed"
  open_feature_forward; expect_lookup after-wake
  record data-survived "$(jq -cn --arg kv "$kv" '{result: "PASS", kv_current_version: ($kv | tonumber), pv: "Bound/Retain",
    externalsecret: "SecretSynced (new reconciliation)", consumer: "OK", feature_api: "BASELINE_OK"}')"
  compare_production production-unchanged
}

phase_cleanup() {  # MUTATING, destructive for rehearsal objects only: synthetic Vault data, ESO CRDs, test namespaces
  local pvs left claim st unfinished
  for ns in "$RNS" "$ENS" "$VNS"; do
    if k get ns "$ns" >/dev/null 2>&1 && ! mine "$ns"; then fail ownership "namespace $ns exists without $LABEL=$ID: not this rehearsal's"; fi
  done
  record ownership "$(jq -cn --argjson v "$(cat "$DIR/volumes.json" 2>/dev/null || echo null)" --argjson e "$(cat "$DIR/eso-cluster-objects.json" 2>/dev/null || echo null)" \
    '{result: "PASS", namespaces: "labelled with this rehearsal id or absent", volumes: $v, eso_cluster_objects: ($e.cluster_objects // [])}')"
  # Everything is checked before the first deletion; a resumed cleanup skips what is already gone.
  if h status external-secrets -n "$ENS" >/dev/null 2>&1; then check_eso_scope; fi
  if [ -e "$DIR/volumes.json" ]; then
    # Any point of the Retain drill: both PVCs bound, one or both deleted (PV Released), a claimRef already
    # cleared (PV Available), one PVC recreated, or a cleanup retried after some volumes are gone.
    # An Available PV is never reclaimed by the provisioner, so an interrupted recovery is finished first
    # (the runbook's own steps); a PV object or disk is never deleted directly, and never by a selector.
    check_volumes_before_delete "bound binding unclaimed released gone"
    st=$(volume_states)     # an assignment, so a failed query stops here (set -e); never inside a here-string or for list
    unfinished=$(jq -r '.volumes[] | select(.state == "unclaimed" or .state == "binding") | .claim' <<<"$st")
    if [ -n "$unfinished" ]; then
      mine "$VNS" || fail volumes-identity "an interrupted PVC recovery needs $VNS (labelled with this id) to finish; nothing deleted"
      for claim in $unfinished; do
        rebind_volume "$(jq -r --arg c "$claim" '.[] | select(.claim == $c) | .name' "$DIR/volumes.json")" "$claim"
      done
      record recovery-completed '{"result": "PASS", "detail": "the interrupted Released-PV recovery was finished before cleanup"}'
      check_volumes_before_delete "bound released gone"
    fi
    # A+ leaves the PVs on Retain. Only the recorded, identity-checked PVs that still exist go back to Delete:
    # a Released one is then deleted with its disk by the provisioner, a Bound one with its PVC below,
    # as in Campaign A. No gcloud delete.
    for v in $(jq -r '.[].name' "$DIR/volumes.json"); do
      [ -n "$(k get pv "$v" -o name --ignore-not-found)" ] || continue
      k patch pv "$v" -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}' >/dev/null
      [ "$(k get pv "$v" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}' --ignore-not-found)" != Retain ] || fail cleanup "PV $v not back to Delete"
    done
    record reclaim-delete-restored '{"result": "PASS", "scope": "the recorded rehearsal PVs still present"}'
    fault cleanup-reclaim-restored
  else
    left=$( { k -n "$VNS" get pvc -o json; k get pv -o json; } | jq -rs --arg ns "$VNS" \
      '[.[0].items[] | "PVC \(.metadata.name)"] + [.[1].items[] | select(.spec.claimRef.namespace == $ns) | "PV \(.metadata.name)"] | join(", ")') \
      || fail volumes-identity "PVC/PV query failed; nothing deleted"
    [ -z "$left" ] || fail volumes-identity "no volumes.json, but $VNS has volumes ($left): not identified, nothing deleted"
    record volumes-identity '{"result": "PASS", "volumes": "none recorded and none in ewallet-vault"}'
  fi
  stop_forward; stop_ffwd
  if mine "$RNS"; then
    if h status "$SERVING" -n "$RNS" >/dev/null 2>&1; then h uninstall "$SERVING" -n "$RNS" --wait --timeout 5m >/dev/null; fi
    k -n "$RNS" delete externalsecret,secretstore --all --wait=true >/dev/null 2>&1 || true   # owned Secrets are collected
    k delete ns "$RNS" --wait=true --timeout=300s >/dev/null
  fi
  if mine "$ENS"; then
    if h status external-secrets -n "$ENS" >/dev/null 2>&1; then h uninstall external-secrets -n "$ENS" --wait --timeout 5m >/dev/null; fi
    k delete ns "$ENS" --wait=true --timeout=300s >/dev/null
  fi
  if mine "$VNS"; then
    if h status vault -n "$VNS" >/dev/null 2>&1; then h uninstall vault -n "$VNS" --wait --timeout 5m >/dev/null; fi
    # Only the two recorded claims, identity checked above; reclaim Delete removes their synthetic disks.
    k -n "$VNS" delete pvc $(jq -r '.[].claim' "$DIR/volumes.json") --ignore-not-found --wait=true --timeout=300s >/dev/null
    k delete ns "$VNS" --wait=true --timeout=300s >/dev/null
  fi
  for _ in $(seq 60); do
    pvs=$(jq -r '.[].name' "$DIR/volumes.json" 2>/dev/null | while read -r v; do k get pv "$v" -o name --ignore-not-found; done)
    [ -z "$pvs" ] && break; sleep 5
  done
  [ -z "$pvs" ] || fail volumes "PersistentVolumes still present: $pvs"
  if [ -z "$KIND" ]; then
    for handle in $(jq -r '.[].disk' "$DIR/volumes.json"); do
      for _ in $(seq 24); do [ "$(disk_state "$handle")" = absent ] && break; sleep 5; done
      case "$(disk_state "$handle")" in
        absent) ;;
        present) fail disks "Persistent Disk ${handle##*/} still present after its PV was deleted; the script never deletes a disk itself: owner decision (VAULT_GKE_REHEARSAL.md §9)" ;;
        *) fail disks "Persistent Disk ${handle##*/}: state unknown (gcloud query failed); not counted as deleted" ;;
      esac
    done
  fi
  rm -rf "$PRIV"
  record cleanup "$(jq -cn --arg d "$([ -z "$KIND" ] && echo 'confirmed absent (gcloud compute disks describe: HTTP 404 naming each recorded disk)' || echo 'kind local-path: removed')" \
    '{result: "PASS", namespaces_deleted: ["vault-rehearsal", "external-secrets", "ewallet-vault"], releases_uninstalled: ["rehearsal-serving", "external-secrets", "vault"],
      vault_volumes: "deleted", disks: $d, synthetic_private_material: "removed"}')"
}

phase_verify_clean() {  # READ-ONLY: back to the pre-rehearsal state
  local found
  found=$(collisions) || fail collisions "a query failed"
  none_found "$found" || fail clean "left behind: $found"
  record nothing-left "$(jq -c '{result: "PASS"} + .' <<<"$found")"
  [ ! -e "$PRIV" ] || fail private-material "$PRIV still exists: run cleanup"
  # Anchored at ^kubectl: a shell whose command line merely mentions the pattern must not match.
  if pgrep -f "^kubectl .*port-forward pod/vault-0 $PORT:8200" >/dev/null; then fail port-forward "a port-forward to vault-0 on $PORT is still running"; fi
  if pgrep -f "^kubectl .*port-forward svc/feature-api $FPORT:80" >/dev/null; then fail port-forward "a port-forward to the Feature API on $FPORT is still running"; fi
  record local-state-clean '{"result": "PASS", "synthetic_private_material": "absent", "port_forwards": "none"}'
  if [ -z "$KIND" ]; then
    python3 "$root/scripts/vault/gke_preflight.py" --mode preinstall --kubeconfig "$KUBECONFIG" --context "$CTX" --out "$DIR/preflight-after" >/dev/null \
      || fail gke-preflight-after "not READY: see $DIR/preflight-after/gates-preinstall.jsonl"
    record gke-preflight-after '{"result": "PASS", "verdict": "VAULT_GKE_PREFLIGHT_READY"}'
  fi
  compare_production production-unchanged
  record clean-verified '{"result": "PASS"}'
}

# ---- fail-fast: what the run may do next (VAULT_GKE_REHEARSAL.md §9)
halt_reason() {  # empty while every mutating phase so far passed; else why only inspect, cleanup and verify-clean may run
  [ -s "$EV" ] || return 0
  jq -rs '([.[] | select(.result == "FAIL" and (.phase | IN("preflight", "verify-clean", "inspect", "gate") | not)) | "\(.phase) failed at \(.step)"][0])
    // (if any(.[]; .phase == "cleanup") then "cleanup has started" else null end)
    // ([.[] | select(.step == "phase-start" or .step == "phase-done")] | last | select(.step == "phase-start") | "\(.phase) was interrupted (no PASS or FAIL)")
    // empty' "$EV"
}
last_done() { [ -s "$EV" ] && jq -rs '[.[] | select(.step == "phase-done") | .phase] | last // ""' "$EV" || true; }
phase_gate() {  # READ-ONLY: exit 0 only if the run may go on to the next cloud step
  local why ok= last
  why=$(halt_reason); last=$(last_done)
  case "$arg" in
    retain|after-wake) if [ -z "$why" ] && [ "$last" = "$arg" ]; then ok=1; fi ;;
    clean) ok=$(jq -rs '([.[] | select(.phase == "cleanup")] | last | select(.step == "cleanup" and .result == "PASS")) as $c
      | if $c and ([.[] | select(.phase == "verify-clean")] | last | (.step == "clean-verified" and .result == "PASS")) then 1 else empty end' "$EV" 2>/dev/null || true) ;;
  esac
  if [ -n "$ok" ]; then record "gate-$arg" '{"result": "PASS"}'; return; fi
  fail "gate-$arg" "NOT READY for the step after $arg: ${why:-last passed phase is ${last:-none}}; do not continue: run inspect and follow VAULT_GKE_REHEARSAL.md §9 Failure"
}
phase_inspect() {  # READ-ONLY: the state after any stop, and the decision it leaves to the owner
  local ns rel vols disks why next
  ns=$(for n in "$VNS" "$ENS" "$RNS"; do
    o=$(k get ns "$n" -o json --ignore-not-found) || fail inspect "namespace query failed"
    jq -cn --arg n "$n" --arg id "$ID" --argjson o "${o:-null}" '{($n): (if $o == null then "absent" elif $o.metadata.labels["ewallet.io/vault-rehearsal"] == $id then "this rehearsal" else "NOT this rehearsal" end)}'
  done | jq -cs add)
  rel=$(h list -A -o json | jq -c --arg v "$VNS" --arg e "$ENS" --arg r "$RNS" '[.[] | select(.namespace | IN($v, $e, $r)) | "\(.namespace)/\(.name) r\(.revision) \(.status)"]') \
    || fail inspect "helm query failed"
  vols=null; disks=null
  if [ -e "$DIR/volumes.json" ]; then
    vols=$(volume_states)
    if [ -z "$KIND" ]; then disks=$(for h in $(jq -r '.[].disk' "$DIR/volumes.json"); do jq -cn --arg d "${h##*/}" --arg s "$(disk_state "$h")" '{($d): $s}'; done | jq -cs add); fi
  fi
  why=$(halt_reason)
  if [ -z "$why" ]; then next="no failure: continue with the next step of §9 after its gate (last passed phase: $(last_done))"
  elif [ "$vols" != null ] && [ "$(jq '.problems | length' <<<"$vols")" != 0 ]; then next="STOPPED; cleanup would refuse: owner decision (§9 Failure, F2: guarded sleep as-is)"
  else next="STOPPED; owner decision: F1 cleanup (VAULT_REHEARSAL_CLEANUP_APPROVED) then the final guarded sleep, or F2 guarded sleep as-is"; fi
  record inspect "$(jq -cn --arg why "$why" --arg next "$next" --argjson ns "$ns" --argjson rel "$rel" --argjson vols "$vols" --argjson disks "$disks" \
    '{result: "PASS", stopped: (if $why == "" then null else $why end), namespaces: $ns, releases: $rel, volumes: $vols, disks: $disks, next: $next}')"
}

case "$phase" in
  vault|init|ceremony|seal|configure|eso|consumer|negative|lifecycle|feature-api|switch|rollback|agents|retain|after-wake)
    why=$(halt_reason)
    [ -z "$why" ] || refuse "this run has stopped ($why): only inspect, cleanup and verify-clean may run (VAULT_GKE_REHEARSAL.md §9 Failure)"
    record phase-start '{"result": "STARTED"}'
    "phase_${phase//-/_}"
    record phase-done '{"result": "PASS"}' ;;
  cleanup) record phase-start '{"result": "STARTED"}'; phase_cleanup ;;
  *) "phase_${phase//-/_}" ;;
esac
