#!/usr/bin/env bash
# Vault + ESO technical rehearsal (Campaign A), one phase per call.
# Procedure, approvals and PASS conditions: docs/runbooks/edai2/VAULT_GKE_REHEARSAL.md.
#
#   REHEARSAL_CONTEXT=<kind-* | the pinned GKE context>  REHEARSAL_ID=vault-rehearsal-<UTC yyyymmddThhmmssZ>
#   REHEARSAL_DIR=<directory outside the repository>     CHARTS=<directory with the two pinned chart archives>
#   bash scripts/vault/gke_rehearsal.sh <preflight|vault|init|configure|eso|consumer|negative|lifecycle|cleanup|verify-clean>
#
# What it deploys: the official Vault and ESO charts with the repository values, certificate,
# NetworkPolicy and bootstrap (the same names as the official campaign: ewallet-vault,
# external-secrets, releases vault and external-secrets). The consumer side lives only in the
# temporary namespace vault-rehearsal: a synthetic Redis, a SecretStore and ExternalSecret, and
# a redis-cli consumer Job. No object is created in ml-platform, ewallet-agents or any other
# existing namespace; feast-online-redis and the Feature API are never read or changed.
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
# It never wakes or sleeps the cluster: that is cloud_lab.py, approved separately.
set -euo pipefail
case $- in *x*) echo "refusing to run with xtrace: it would print secret values" >&2; exit 2 ;; esac

phase=${1:?phase}
GKE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
GKE_KUBECONFIG=$HOME/.cache/ewallet-cloud-lab/kubeconfig
PROJECT=ewallet-data-ai-platform-hg26
CTX=${REHEARSAL_CONTEXT:?REHEARSAL_CONTEXT} ID=${REHEARSAL_ID:?REHEARSAL_ID} DIR=${REHEARSAL_DIR:?REHEARSAL_DIR}
VNS=ewallet-vault ENS=external-secrets RNS=vault-rehearsal LABEL=ewallet.io/vault-rehearsal PORT=${REHEARSAL_PORT:-18233}
CLUSTER_KINDS=clusterrole,clusterrolebinding,validatingwebhookconfiguration,mutatingwebhookconfiguration
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
case "$phase" in preflight|verify-clean) ;;
  cleanup) [ -n "$KIND" ] || [ "${VAULT_REHEARSAL_CLEANUP_APPROVED:-}" = "$ID" ] || refuse "cleanup needs VAULT_REHEARSAL_CLEANUP_APPROVED=$ID" ;;
  vault|init|configure|eso|consumer|negative|lifecycle)
    [ -n "$KIND" ] || [ "${VAULT_REHEARSAL_APPROVED:-}" = "$ID" ] || refuse "$phase needs VAULT_REHEARSAL_APPROVED=$ID" ;;
  *) refuse "unknown phase $phase" ;;
esac
DIR=$(realpath -m "$DIR")                       # resolve first: nothing is created inside the repository
case "$DIR/" in "$root"/*) refuse "REHEARSAL_DIR must be outside the repository" ;; esac
mkdir -p "$DIR"
PRIV=$DIR/private
case "$phase" in preflight|verify-clean) ;; *) mkdir -p -m 700 "$PRIV"; chmod 700 "$PRIV" ;; esac   # synthetic material only
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
mine() { [ "$(k get ns "$1" -o jsonpath="{.metadata.labels.ewallet\.io/vault-rehearsal}" 2>/dev/null)" = "$ID" ]; }

forward=
stop_forward() { [ -z "$forward" ] || kill "$forward" 2>/dev/null || true; forward=; }
trap stop_forward EXIT
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

# ---- ExternalSecret helpers (same classes as scripts/vault/eso_local_validate.sh)
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
force_sync() { k -n "$RNS" annotate externalsecret "$1" "force-sync=$(date +%s%N)" --overwrite >/dev/null; }
synced_after() {  # <es> <previous syncedResourceVersion>: a NEW reconciliation, not a stale Ready=True
  for _ in $(seq 120); do
    local rv; rv=$(es_synced_rv "$RNS" "$1")
    [ -n "$rv" ] && [ "$rv" != "$2" ] && [ "$(es_condition "$RNS" "$1")" = "True SecretSynced" ] && { printf '%s' "$rv"; return 0; }
    sleep 1
  done
  return 1
}
negative() {  # <step> <externalsecret> <expected class>: unsynced, no target Secret, and the specific cause
  local class=
  es_wait "$RNS" "$2" False 90 || fail "$1" "ExternalSecret $2 did not report a failure"
  if k -n "$RNS" get secret "$2" >/dev/null 2>&1; then fail "$1" "target Secret $2 exists"; fi
  for _ in $(seq 30); do class=$(es_error "$2"); [ "$class" = "$3" ] && break; sleep 2; done
  [ "$class" = "$3" ] || fail "$1" "expected $3, observed $class"
  record "$1" "$(jq -cn --arg c "$(es_condition "$RNS" "$2")" --arg e "$class" \
    '{result: "denied", condition: $c, error_class: $e, target_secret: "absent"}')"
}
external_secret() {  # <name> <store> <key> <property> [extra label]: target Secret has the same name
  own <<YAML | python3 -c 'import sys,yaml; d=yaml.safe_load(sys.stdin); l=sys.argv[1]; (l and d["metadata"]["labels"].update({l: "true"})); yaml.safe_dump(d, sys.stdout)' "${5:-}" | k create -f - >/dev/null
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata: {name: $1, namespace: $RNS}
spec:
  refreshInterval: 1h
  secretStoreRef: {kind: SecretStore, name: $2}
  target: {name: $1, creationPolicy: Owner, deletionPolicy: Retain}
  data:
    - secretKey: $4
      remoteRef: {key: $3, property: $4}
YAML
}
secret_store() {  # <name> <service account> <audience> <role> <ca configmap>
  create <<YAML
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata: {name: $1, namespace: $RNS, labels: {ewallet.io/vault-rehearsal-negative: "${6:-false}"}}
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
restricted='securityContext: {runAsNonRoot: true, runAsUser: 999, runAsGroup: 999, seccompProfile: {type: RuntimeDefault}}'
container_security='securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}'
consume() {  # one real authenticated Redis round trip with the ESO-managed Secret; prints OK, AUTH_FAILED or UNREACHABLE
  local job state
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
          env: [{name: CONN, valueFrom: {secretKeyRef: {name: rehearsal-redis-vault, key: connection_string}}}]
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
  ns=$(k get ns -o json | jq -c --arg a "$VNS" --arg b "$ENS" --arg c "$RNS" '[.items[].metadata.name | select(. == $a or . == $b or . == $c)]') || return 1
  crds=$(k get crd -o json | jq -c '[.items[].metadata.name | select(endswith(".external-secrets.io"))] | length') || return 1
  rel=$(h list -A -a -o json | jq -c '[.[] | select(.name == "vault" or .name == "external-secrets") | "\(.namespace)/\(.name)"]') || return 1
  objs=$(k get "$CLUSTER_KINDS" -l 'app.kubernetes.io/instance in (vault,external-secrets)' -o json | jq -c '[.items[] | "\(.kind)/\(.metadata.name)"]') || return 1
  pvs=$(k get pv -o json | jq -c --arg a "$VNS" --arg c "$RNS" '[.items[] | select(.spec.claimRef.namespace == $a or .spec.claimRef.namespace == $c) | .metadata.name]') || return 1
  jq -cn --argjson ns "$ns" --argjson crds "$crds" --argjson rel "$rel" --argjson objs "$objs" --argjson pvs "$pvs" \
    '{namespaces: $ns, eso_crds: $crds, releases: $rel, chart_cluster_objects: $objs, vault_volumes: $pvs}'
}
none_found() { jq -e '(.namespaces + .releases + .chart_cluster_objects + .vault_volumes | length) == 0 and .eso_crds == 0' <<<"$1" >/dev/null; }
snapshot() {  # production state the rehearsal must not change (names and versions only; its own releases excluded)
  local rel feast fapi lbs gw
  rel=$(h list -A -a -o json | jq -c '[.[] | select(.name != "vault" and .name != "external-secrets") | {name, namespace, revision, status}]
    | sort_by(.namespace, .name)') || return 1
  feast=$(k -n ml-platform get secret feast-online-redis --ignore-not-found -o jsonpath='{.metadata.resourceVersion}') || return 1
  fapi=$(k -n ml-platform get deploy feature-api --ignore-not-found -o json | jq -cs '.[0] | if . == null then null else
    {generation: .metadata.generation, redis_secret: ([.spec.template.spec.containers[0].env[]? | select(.name == "EWALLET_REDIS_CONNECTION_STRING") | .valueFrom.secretKeyRef.name][0])} end') || return 1
  lbs=$(k get svc -A -o json | jq -c '[.items[] | select(.spec.type == "LoadBalancer") | "\(.metadata.namespace)/\(.metadata.name)"] | sort') || return 1
  gw=$(k -n ewallet-ingress get svc ewallet-edge-gateway --ignore-not-found -o jsonpath='{.spec.type}') || return 1
  jq -cn --argjson rel "$rel" --arg feast "$feast" --argjson fapi "$fapi" --argjson lbs "$lbs" --arg gw "$gw" \
    '{helm_releases: $rel, feast_online_redis_resource_version: $feast, feature_api: $fapi, loadbalancer_services: $lbs, edge_gateway_service_type: $gw}'
}

compare_production() {  # <step>: the production snapshot must equal baseline.json; any drift stops the run
  local now
  [ -f "$DIR/baseline.json" ] || fail "$1" "no baseline.json: run preflight first"
  now=$(snapshot) || fail "$1" "snapshot query failed"
  if jq -e --argjson now "$now" '. == $now' "$DIR/baseline.json" >/dev/null; then
    record "$1" '{"result": "PASS", "compared": ["helm releases and revisions", "feast-online-redis resourceVersion", "feature-api generation and Secret", "LoadBalancer Services", "edge gateway Service type"]}'
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
check_volumes_before_delete() {  # the live PVC -> PV -> disk chain must be exactly the recorded rehearsal one
  local recorded live
  recorded=$(jq -c 'sort_by(.claim)' "$DIR/volumes.json") || fail volumes-identity "volumes.json missing or invalid"
  [ "$(jq 'length' <<<"$recorded")" = 2 ] || fail volumes-identity "expected 2 recorded volumes"
  live=$(k -n "$VNS" get pvc -o json | jq -c '[.items[] | {claim: .metadata.name, name: .spec.volumeName}] | sort_by(.claim)') \
    || fail volumes-identity "PVC query failed"
  [ "$(jq -c '[.[] | {claim, name}]' <<<"$recorded")" = "$live" ] || fail volumes-identity "live PVCs $live differ from the recorded $recorded"
  for v in $(jq -r '.[].name' <<<"$recorded"); do
    k get pv "$v" -o json | jq -e --arg ns "$VNS" --argjson r "$(jq -c --arg v "$v" '.[] | select(.name == $v)' <<<"$recorded")" \
      '.spec.persistentVolumeReclaimPolicy == "Delete" and .spec.claimRef.namespace == $ns and .spec.claimRef.name == $r.claim
       and (.spec.csi.volumeHandle // null) == $r.disk' >/dev/null || fail volumes-identity "PV $v: not reclaim Delete, or claim or disk differs from the record"
  done
  if [ -z "$KIND" ]; then
    for handle in $(jq -r '.[].disk' <<<"$recorded"); do
      [ "$(disk_state "$handle")" = present ] || fail volumes-identity "disk ${handle##*/} not confirmed present before deletion"
    done
  fi
  record volumes-identity "$(jq -c '{result: "PASS", volumes: ., reclaim: "Delete", disks: "confirmed present"}' <<<"$recorded")"
}
check_eso_scope() {  # ESO may only be serving the rehearsal: no store or ExternalSecret elsewhere, no inventory drift
  local where objs
  where=$(k get externalsecret,secretstore,pushsecret -A -o json | jq -c --arg r "$RNS" '[.items[] | select(.metadata.namespace != $r) | "\(.kind) \(.metadata.namespace)/\(.metadata.name)"]') \
    || fail eso-scope "ExternalSecret/SecretStore query failed"
  [ "$where" = "[]" ] || fail eso-scope "ESO objects outside $RNS: $where"
  objs=$(k get "$CLUSTER_KINDS" -l app.kubernetes.io/instance=external-secrets -o json | jq -c '[.items[] | "\(.kind)/\(.metadata.name)"] | sort') \
    || fail eso-scope "cluster object query failed"
  [ "$objs" = "$(jq -c '.cluster_objects | sort' "$DIR/eso-cluster-objects.json")" ] || fail eso-scope "ESO cluster objects differ from the recorded inventory: $objs"
  record eso-scope '{"result": "PASS", "externalsecrets_and_stores": "only in vault-rehearsal", "cluster_objects": "as recorded"}'
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

phase_vault() {  # MUTATING (Kubernetes): RB step 2 with the official files, in a namespace proven new
  local found pvcs pvs image sans out
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
  record vault-uninitialised "$(status_is false true)"
  out=$(curl -s --cacert "$PRIV/ca.crt" -o /dev/null -w '{"https_status": %{http_code}, "ssl_verify_result": %{ssl_verify_result}}' \
    "https://127.0.0.1:$PORT/v1/sys/health?uninitcode=200&sealedcode=200") || fail vault-tls "HTTPS with the vault-ca CA failed"
  jq -e '.https_status == 200 and .ssl_verify_result == 0' <<<"$out" >/dev/null || fail vault-tls "$out"
  record vault-tls "$out"
}

phase_init() {  # MUTATING (Vault): RB step 3 with SYNTHETIC recovery material in REHEARSAL_DIR/private
  local out
  open_forward
  status_is false true >/dev/null
  out=$(vault init --keys-out "$PRIV/vault-keys.json") || fail vault-init "init failed: read the REFUSED line; never re-run init blindly"
  record vault-init "$out"
  record pod-ready-while-sealed "$(jq -cn --arg r "$(pod_ready)" --argjson s "$(status_is true true)" \
    '{pod_ready: $r, initialized: $s.initialized, sealed: $s.sealed, note: "Pod Ready does not mean unsealed"}')"
  out=$(vault unseal --keys-file "$PRIV/vault-keys.json") || fail vault-unseal "unseal failed"
  record vault-unsealed "$(status_is true false)"
}

phase_configure() {  # MUTATING (Vault): RB step 4, then the rehearsal role; root retired at the end
  local out K=$PRIV/vault-keys.json OP=$PRIV/operator-token.json
  open_forward; status_is true false >/dev/null
  out=$(vault configure --keys-file "$K") || fail configure-official "configure failed"; record configure-official "$out"
  out=$(vault configure --keys-file "$K" --config "$root/scripts/vault/rehearsal/bootstrap.json") || fail configure-rehearsal "configure failed"
  record configure-rehearsal "$out"
  vault describe --keys-file "$K" > "$DIR/vault-describe.json" || fail describe "describe failed"
  jq -e '.secret_engines["ewallet/"] == {type: "kv", version: "2"} and .auth_methods["kubernetes/"] == "kubernetes"
         and .kubernetes_roles["eso-vault-rehearsal"].bound_service_account_namespaces == ["vault-rehearsal"]
         and .kubernetes_roles["eso-ml-platform"].audience == "vault"' "$DIR/vault-describe.json" >/dev/null \
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
  external_secret rehearsal-redis-vault vault feature-api/rehearsal-redis connection_string
  k -n "$RNS" wait --for=condition=Ready secretstore/vault --timeout=120s >/dev/null || fail store "SecretStore not Ready: $(es_error vault)"
  es_wait "$RNS" rehearsal-redis-vault True 120 || fail sync "not synced: $(es_error rehearsal-redis-vault)"
  owner=$(k -n "$RNS" get secret rehearsal-redis-vault -o jsonpath='{.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}')
  keys=$(k -n "$RNS" get secret rehearsal-redis-vault -o json | jq -c '.data | keys')
  [ "$owner" = ExternalSecret/rehearsal-redis-vault ] || fail sync "target owned by $owner"
  [ "$(secret_sha "$RNS" rehearsal-redis-vault connection_string)" = "$(sha "$(conn "$p1")")" ] || fail sync "synced value differs from Vault v1"
  record secret-synced "$(jq -cn --arg c "$(es_condition "$RNS" rehearsal-redis-vault)" --arg o "$owner" --argjson k "$keys" \
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
  rv=$(es_synced_rv "$RNS" rehearsal-redis-vault)
  out=$(conn "$p2" | vault put --token-file "$OP" --path feature-api/rehearsal-redis --key connection_string --stdin) || fail kv-v2 "put failed"
  force_sync rehearsal-redis-vault; rv=$(synced_after rehearsal-redis-vault "$rv") || fail kv-v2 "no new reconciliation"
  [ "$(secret_sha "$RNS" rehearsal-redis-vault connection_string)" = "$(sha "$(conn "$p2")")" ] || fail kv-v2 "Secret not at v2"
  [ "$(consume)" = AUTH_FAILED ] || fail kv-v2 "the consumer did not use the new version"
  record kv-v2-propagated "$(jq -c '{result: "PASS", version: .version, consumer: "AUTH_FAILED (v2 is deliberately wrong: the new value was used)"}' <<<"$out")"
  out=$(conn "$p1" | vault put --token-file "$OP" --path feature-api/rehearsal-redis --key connection_string --stdin) || fail kv-v3 "put failed"
  force_sync rehearsal-redis-vault; rv=$(synced_after rehearsal-redis-vault "$rv") || fail kv-v3 "no new reconciliation"
  [ "$(consume)" = OK ] || fail kv-v3 "the consumer did not recover with v3"
  record kv-v3-restored "$(jq -cn --argjson o "$out" --argjson m "$(vault versions --token-file "$OP" --path feature-api/rehearsal-redis)" \
    '{result: "PASS", version: $o.version, metadata: $m, consumer: "OK"}')"
  # 2. Restart: Vault comes back sealed (Pod still Ready), ESO fails but keeps the Secret, manual unseal restores sync.
  before=$(secret_sha "$RNS" rehearsal-redis-vault connection_string)
  stop_forward
  k -n "$VNS" delete pod vault-0 --wait=true >/dev/null
  k -n "$VNS" wait --for=condition=Ready pod/vault-0 --timeout=300s >/dev/null || fail restart "vault-0 not back"
  open_forward
  record restarted-sealed "$(jq -cn --arg r "$(pod_ready)" --argjson s "$(status_is true true)" '{result: "PASS", pod_ready: $r, sealed: $s.sealed}')"
  force_sync rehearsal-redis-vault
  es_wait "$RNS" rehearsal-redis-vault False 90 || fail sealed-sync "ESO did not report the sealed Vault"
  for _ in $(seq 30); do [ "$(es_error rehearsal-redis-vault 3m)" = vault-sealed ] && break; sleep 2; done
  [ "$(secret_sha "$RNS" rehearsal-redis-vault connection_string)" = "$before" ] || fail sealed-sync "the Secret changed while sealed"
  [ "$(consume)" = OK ] || fail sealed-sync "the consumer lost access while Vault was sealed"
  record sealed-keeps-secret "$(jq -cn --arg c "$(es_condition "$RNS" rehearsal-redis-vault)" --arg e "$(es_error rehearsal-redis-vault 3m)" \
    '{result: "PASS", externalsecret: $c, error_class: $e, target_secret: "unchanged", consumer: "OK"}')"
  vault unseal --keys-file "$PRIV/vault-keys.json" >/dev/null || fail unseal "unseal failed"
  status_is true false >/dev/null
  rv=$(es_synced_rv "$RNS" rehearsal-redis-vault); force_sync rehearsal-redis-vault
  synced_after rehearsal-redis-vault "$rv" >/dev/null || fail unseal-sync "ESO did not recover after unseal"
  [ "$(consume)" = OK ] || fail unseal-sync "consumer failed after unseal"
  record unsealed-sync-recovered "$(jq -cn --arg c "$(es_condition "$RNS" rehearsal-redis-vault)" '{result: "PASS", externalsecret: $c, consumer: "OK"}')"
  compare_production production-unchanged
}

phase_cleanup() {  # MUTATING, destructive for rehearsal objects only: synthetic Vault data, ESO CRDs, test namespaces
  local pvs left
  for ns in "$RNS" "$ENS" "$VNS"; do
    if k get ns "$ns" >/dev/null 2>&1 && ! mine "$ns"; then fail ownership "namespace $ns exists without $LABEL=$ID: not this rehearsal's"; fi
  done
  record ownership "$(jq -cn --argjson v "$(cat "$DIR/volumes.json" 2>/dev/null || echo null)" --argjson e "$(cat "$DIR/eso-cluster-objects.json" 2>/dev/null || echo null)" \
    '{result: "PASS", namespaces: "labelled with this rehearsal id or absent", volumes: $v, eso_cluster_objects: ($e.cluster_objects // [])}')"
  # Everything is checked before the first deletion; a resumed cleanup skips what is already gone.
  if h status external-secrets -n "$ENS" >/dev/null 2>&1; then check_eso_scope; fi
  if k -n "$VNS" get pvc -o name 2>/dev/null | grep -q .; then check_volumes_before_delete; fi
  stop_forward
  if mine "$RNS"; then
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
        present) fail disks "Persistent Disk ${handle##*/} still present" ;;
        *) fail disks "Persistent Disk ${handle##*/}: state unknown (gcloud query failed); not counted as deleted" ;;
      esac
    done
  fi
  rm -rf "$PRIV"
  record cleanup "$(jq -cn --arg d "$([ -z "$KIND" ] && echo 'confirmed absent (gcloud compute disks describe: HTTP 404 naming each recorded disk)' || echo 'kind local-path: removed')" \
    '{result: "PASS", namespaces_deleted: ["vault-rehearsal", "external-secrets", "ewallet-vault"], releases_uninstalled: ["external-secrets", "vault"],
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
  record local-state-clean '{"result": "PASS", "synthetic_private_material": "absent", "port_forwards": "none"}'
  if [ -z "$KIND" ]; then
    python3 "$root/scripts/vault/gke_preflight.py" --mode preinstall --kubeconfig "$KUBECONFIG" --context "$CTX" --out "$DIR/preflight-after" >/dev/null \
      || fail gke-preflight-after "not READY: see $DIR/preflight-after/gates-preinstall.jsonl"
    record gke-preflight-after '{"result": "PASS", "verdict": "VAULT_GKE_PREFLIGHT_READY"}'
  fi
  compare_production production-unchanged
}

"phase_${phase//-/_}"
