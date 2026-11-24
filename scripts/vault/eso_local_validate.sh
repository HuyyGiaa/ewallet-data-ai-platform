#!/usr/bin/env bash
# Validate Vault -> External Secrets Operator -> Kubernetes Secret -> Feature API end to end
# on a disposable kind deployment.
# usage: eso_local_validate.sh <vault-chart.tgz> <eso-chart.tgz> <feature-api-image@digest> <seed-image> <evidence-dir>
#
# Uses the official Vault and ESO charts with the repository values, the repository
# Vault bootstrap, NetworkPolicy, SecretStores and ExternalSecrets, and the real Feature
# API rendered from infra/helm/ewallet-serving with values-vault-eso.yaml. All secret
# values are synthetic and only their SHA-256 prefixes are recorded. Local evidence only;
# it says nothing about GKE. Refuses any non-kind context.
#
# Isolation: a preflight refuses to start if any namespace, CRD, Helm release, cluster-scoped
# chart object or ewallet-agents object it would create already exists. Objects are created
# (never applied over) with ewallet.io/vault-local=<run id> set at creation time, and cleanup,
# registered only after the preflight, deletes only objects carrying this run's id. The
# pre-existing ewallet-agents namespace and everything else in the cluster are left alone.
set -euo pipefail
vault_chart=${1:?vault chart} eso_chart=${2:?eso chart} feature_image=${3:?feature api image} seed_image=${4:?seed image}
evidence=${5:?evidence directory}
CONTEXT=${VAULT_LOCAL_CONTEXT:-kind-llm-agent-local}
[[ "$CONTEXT" == kind-* ]] || { echo "refusing non-kind context $CONTEXT" >&2; exit 2; }
VNS=ewallet-vault ENS=external-secrets APP=ml-platform AGENTS=ewallet-agents PORT=${VAULT_LOCAL_PORT:-18231}
FPORT=${FEATURE_LOCAL_PORT:-18232} LABEL=ewallet.io/vault-local RUN=vl-$(date -u +%Y%m%d%H%M%S)-$$
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d); keys=$work/keys.json; operator=$work/operator-token.json; admin=$work/admin-token.json
forward= fforward= agents_created=
export PYTHONPATH=$root
k() { kubectl --context "$CONTEXT" "$@"; }
h() { helm --kube-context "$CONTEXT" "$@"; }
vault() { python3 -m ops.vault.cli --address "https://127.0.0.1:$PORT" --ca-file "$work/ca.crt" "$@"; }
record() {  # <step> <json>: an empty or non-JSON result is itself a failure, never a silent skip
  local line
  line=$(printf '%s\n' "$2" | jq -c --arg step "$1" '{step: $step} + .' 2>/dev/null) || line=
  if [ -z "$line" ]; then
    jq -cn --arg step "$1" '{step: $step, result: "FAIL", detail: "command produced no JSON result"}' | tee -a "$evidence/results.jsonl"
    exit 1
  fi
  printf '%s\n' "$line" | tee -a "$evidence/results.jsonl"
}
fail() { record "$1" "$(jq -cn --arg detail "$2" '{result: "FAIL", detail: $detail}')"; exit 1; }
own() {  # stdin YAML -> stdout YAML with this run's label on every object (set at creation, never later)
  python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
for d in docs:
    d.setdefault("metadata", {}).setdefault("labels", {})[sys.argv[1]] = sys.argv[2]
yaml.safe_dump_all(docs, sys.stdout)' "$LABEL" "$RUN"
}
create() { own | k create -f - >/dev/null; }   # fails, rather than overwrites, if the name exists
sha() { printf '%s' "$1" | sha256sum | cut -c1-12; }
secret_sha() {  # <namespace> <secret> <key>: hash prefix of a Secret value, never the value
  k -n "$1" get secret "$2" -o go-template="{{index .data \"$3\"}}" 2>/dev/null | base64 -d | sha256sum | cut -c1-12
}

cleanup() {
  set +e
  [ -z "$forward" ] || kill "$forward" 2>/dev/null
  [ -z "$fforward" ] || kill "$fforward" 2>/dev/null
  # ExternalSecrets first, so their owned target Secrets are garbage-collected; then the rest.
  for ns in "$APP" "$AGENTS"; do
    k -n "$ns" delete externalsecret,secretstore -l "$LABEL=$RUN" --wait=true >/dev/null 2>&1
    k -n "$ns" delete pod,serviceaccount,configmap,secret -l "$LABEL=$RUN" --wait=false >/dev/null 2>&1
  done
  [ "$(h status external-secrets -n "$ENS" -o json 2>/dev/null | jq -r .namespace)" = "$ENS" ] && h uninstall external-secrets -n "$ENS" --wait >/dev/null 2>&1
  [ "$(h status vault -n "$VNS" -o json 2>/dev/null | jq -r .namespace)" = "$VNS" ] && h uninstall vault -n "$VNS" >/dev/null 2>&1
  k delete networkpolicy -n "$VNS" -l "$LABEL=$RUN" >/dev/null 2>&1
  for ns in "$VNS" "$ENS" "$APP" ${agents_created:+"$AGENTS"}; do
    [ "$(k get ns "$ns" -o jsonpath="{.metadata.labels.ewallet\.io/vault-local}" 2>/dev/null)" = "$RUN" ] \
      && k delete ns "$ns" --wait=false >/dev/null 2>&1
  done
  rm -rf "$work"
}
forward_vault() {
  k -n "$VNS" wait --for=condition=Ready pod/vault-0 --timeout=180s >/dev/null
  for _ in $(seq 10); do
    [ -z "$forward" ] || kill "$forward" 2>/dev/null || true
    k -n "$VNS" port-forward pod/vault-0 "$PORT:8200" >/dev/null 2>&1 & forward=$!
    sleep 2
    vault status >/dev/null 2>&1 && return
  done
  echo "Vault did not answer through the port-forward" >&2; return 1
}

forward_feature() {
  [ -z "$fforward" ] || kill "$fforward" 2>/dev/null || true
  for _ in $(seq 10); do
    k -n "$APP" port-forward svc/feature-api "$FPORT:80" >/dev/null 2>&1 & fforward=$!
    sleep 2
    curl -sf -o /dev/null "http://127.0.0.1:$FPORT/health/live" && return
    kill "$fforward" 2>/dev/null || true
  done
  echo "Feature API did not answer through the port-forward" >&2; return 1
}

lookup() {  # one real Feature API lookup through Feast Online and Redis; prints "<status> <feature-count>"
  local body
  body=$(jq -cn --argjson names "$feature_names" '{user_id: "u1", account_id: "a1", device_id: "d1",
    merchant_id: "m1", event_timestamp: "2026-05-31T23:57:00+00:00", feature_names: $names}')
  curl -s -o "$work/lookup.json" -w '%{http_code}' -H 'content-type: application/json' -d "$body" \
    "http://127.0.0.1:$FPORT/v1/features/lookup" || true
  printf ' %s\n' "$(jq -r '.features | length' "$work/lookup.json" 2>/dev/null || echo 0)"
}

es_condition() {  # <namespace> <externalsecret>: "<status> <reason>" of the Ready condition
  k -n "$1" get externalsecret "$2" \
    -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status} {.reason}{end}' 2>/dev/null
}

es_wait() {  # <namespace> <externalsecret> <True|False> [seconds]
  for _ in $(seq "${4:-90}"); do
    [ "$(es_condition "$1" "$2" | cut -d' ' -f1)" = "$3" ] && return 0
    sleep 1
  done
  return 1
}

es_error() {  # <namespace> <object> [since]: the Vault/TLS cause, from the ESO controller log
  # ESO events only say "could not get secret data from provider"; the controller log has the
  # Vault response. Only the error class is recorded, never the log line.
  local text
  text=$(k -n "$ENS" logs deploy/external-secrets --since="${3:-10m}" 2>/dev/null | grep -F "\"$2\"" | tail -40)
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

await_class() {  # <step> <namespace> <externalsecret> <expected-class> <since>: the failure must have this cause
  local class
  for _ in $(seq 30); do class=$(es_error "$2" "$3" "$5"); [ "$class" = "$4" ] && return 0; sleep 2; done
  errors_for "$3"; fail "$1" "expected $4, observed $class"
}

force_sync() { k -n "$1" annotate externalsecret "$2" "force-sync=$(date +%s%N)" --overwrite >/dev/null; }

errors_for() {  # <object>: on a failed assertion, keep that object's controller errors (no values) for diagnosis
  k -n "$ENS" logs deploy/external-secrets --since=15m 2>/dev/null | grep -F "\"$1\"" \
    | jq -r '.error // empty' 2>/dev/null | cut -c1-300 | sort -u > "$evidence/failed-$1-controller-errors.txt"
}

negative() {  # <step> <namespace> <externalsecret> <target> <expected-class>
  # Unsynced, no target Secret, AND the specific rejection: an unreachable Vault or another
  # unrelated failure never counts as a passed authorization test.
  local class
  es_wait "$2" "$3" False 60 || fail "$1" "ExternalSecret $3 did not report a failure"
  if k -n "$2" get secret "$4" >/dev/null 2>&1; then fail "$1" "target Secret $4 exists"; fi
  for _ in $(seq 20); do class=$(es_error "$2" "$3"); [ "$class" = "$5" ] && break; sleep 2; done
  [ "$class" = "$5" ] || { errors_for "$3"; fail "$1" "expected $5, observed $class"; }
  record "$1" "$(jq -cn --arg c "$(es_condition "$2" "$3")" --arg e "$class" \
    '{result: "denied", condition: $c, error_class: $e, expected: true, target_secret: "absent"}')"
}

negative_es() {  # <name> <store> <key> <property> [namespace]: a run-owned ExternalSecret
  create <<YAML
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata: {name: $1, namespace: ${5:-$APP}}
spec:
  refreshInterval: 1h
  secretStoreRef: {kind: SecretStore, name: $2}
  target: {name: $1, creationPolicy: Owner}
  data:
    - secretKey: value
      remoteRef: {key: $3, property: $4}
YAML
}

negative_store() {  # <name> <namespace> <service-account> <audience> <role> <ca-configmap> [mount]
  create <<YAML
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata: {name: $1, namespace: $2}
spec:
  provider:
    vault:
      server: https://vault.$VNS.svc.cluster.local:8200
      path: ewallet
      version: v2
      caProvider: {type: ConfigMap, name: $6, key: ca.crt}
      auth:
        kubernetes:
          mountPath: ${7:-kubernetes}
          role: $5
          serviceAccountRef: {name: $3, audiences: [$4]}
YAML
}

vault_client_pod() {  # <name> <app-label>: vault status from ml-platform; prints the exit code
  create <<YAML
apiVersion: v1
kind: Pod
metadata: {name: $1, namespace: $APP, labels: {app.kubernetes.io/name: $2}}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  containers:
    - name: probe
      image: $vault_image
      imagePullPolicy: IfNotPresent
      command: [vault, status, -address=https://vault.$VNS.svc.cluster.local:8200, -ca-cert=/ca/ca.crt]
      env: [{name: VAULT_CLIENT_TIMEOUT, value: "5s"}, {name: VAULT_MAX_RETRIES, value: "0"}]
      volumeMounts: [{name: ca, mountPath: /ca, readOnly: true}]
  volumes: [{name: ca, configMap: {name: vault-ca}}]
YAML
  k -n "$APP" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$1" --timeout=90s >/dev/null 2>&1 \
    || k -n "$APP" wait --for=jsonpath='{.status.phase}'=Failed "pod/$1" --timeout=30s >/dev/null 2>&1 || true
  k -n "$APP" get pod "$1" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}'
}

render_feature_api() {  # [extra values file]: the chart's Feature API, plus the local registry volume
  h template serving "$root/infra/helm/ewallet-serving" -n "$APP" "$@" -f "$work/local-serving.yaml" \
    | python3 -c '
import sys, yaml
out = []
for doc in yaml.safe_load_all(sys.stdin):
    if not doc or doc["metadata"].get("namespace") != "ml-platform":
        continue  # the feature-store Redis NetworkPolicy has no target locally
    if doc["kind"] == "Deployment":
        spec = doc["spec"]["template"]["spec"]
        spec["volumes"] = [{"name": "registry", "persistentVolumeClaim": {"claimName": "feast-registry", "readOnly": True}}]
        spec["containers"][0]["volumeMounts"] = [{"name": "registry", "mountPath": "/registry", "readOnly": True}]
    out.append(doc)
yaml.safe_dump_all(out, sys.stdout)'
}

# ---------------------------------------------------------------- namespaces, TLS, Vault
refuse() { echo "PREFLIGHT_REFUSED: $1 already exists; nothing was created or changed" >&2; rm -rf "$work"; exit 2; }
for ns in "$VNS" "$ENS" "$APP"; do k get ns "$ns" >/dev/null 2>&1 && refuse "namespace $ns"; done
for crd in externalsecrets.external-secrets.io secretstores.external-secrets.io; do
  k get crd "$crd" >/dev/null 2>&1 && refuse "CRD $crd (another ESO installation)"
done
h list -A -q 2>/dev/null | grep -qx -e vault -e external-secrets && refuse "a Helm release named vault or external-secrets"
for object in clusterrolebinding/vault-server-binding clusterrole/external-secrets-controller \
    validatingwebhookconfiguration/secretstore-validate validatingwebhookconfiguration/externalsecret-validate; do
  k get "$object" >/dev/null 2>&1 && refuse "$object"
done
if k get ns "$AGENTS" >/dev/null 2>&1; then
  for object in serviceaccount/eso-vault-reader configmap/vault-ca secret/agentgateway-client-key-vault \
      secret/neg-agents-reads-feature secret/neg-borrowed-role; do
    k -n "$AGENTS" get "$object" >/dev/null 2>&1 && refuse "$AGENTS/$object"
  done
fi
mkdir -p "$evidence"
trap cleanup EXIT   # only now: from here on, everything this run creates carries $RUN
vault_image=$(python3 -c 'import yaml,sys; i=yaml.safe_load(open(sys.argv[1]))["server"]["image"]; print(i["repository"]+":"+i["tag"])' \
  "$root/infra/kubernetes/vault/values-gke.yaml")
printf 'apiVersion: v1\nkind: Namespace\nmetadata: {name: %s, labels: {pod-security.kubernetes.io/enforce: restricted}}\n' "$VNS" | create
create < "$root/infra/kubernetes/external-secrets/namespace.yaml"
printf 'apiVersion: v1\nkind: Namespace\nmetadata: {name: %s}\n' "$APP" | create
if ! k get ns "$AGENTS" >/dev/null 2>&1; then
  printf 'apiVersion: v1\nkind: Namespace\nmetadata: {name: %s}\n' "$AGENTS" | create; agents_created=1
fi

ca() {  # <name>: a disposable CA in $work/<name>.{key,crt}
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=$1" \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' -keyout "$work/$1.key" -out "$work/$1.crt" 2>/dev/null
}
ca local-vault-ca; cp "$work/local-vault-ca.crt" "$work/ca.crt"; ca untrusted-ca
openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj /CN=vault \
  -keyout "$work/tls.key" -out "$work/tls.csr" 2>/dev/null
openssl x509 -req -in "$work/tls.csr" -CA "$work/ca.crt" -CAkey "$work/local-vault-ca.key" -CAcreateserial -days 1 \
  -out "$work/tls.crt" -extfile <(printf 'subjectAltName=DNS:vault,DNS:vault.%s.svc,DNS:vault.%s.svc.cluster.local,DNS:localhost,IP:127.0.0.1\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n' "$VNS" "$VNS") 2>/dev/null
k -n "$VNS" create secret tls vault-server-tls --cert="$work/tls.crt" --key="$work/tls.key" --dry-run=client -o yaml | create
for ns in "$APP" "$AGENTS"; do
  k -n "$ns" create configmap vault-ca --from-file=ca.crt="$work/ca.crt" --dry-run=client -o yaml | create
done
k -n "$APP" create configmap vault-ca-untrusted --from-file=ca.crt="$work/untrusted-ca.crt" --dry-run=client -o yaml | create

h install vault "$vault_chart" -n "$VNS" \
  -f "$root/infra/kubernetes/vault/values-gke.yaml" -f "$root/infra/kubernetes/vault/values-local.yaml" >/dev/null
create < "$root/infra/kubernetes/vault/network-policy.yaml"
create < "$root/infra/kubernetes/vault/admin-serviceaccount.yaml"
k -n "$VNS" wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=240s >/dev/null
forward_vault
record vault-status-uninitialised "$(vault status)"
record vault-init "$(vault init --keys-out "$keys")"
record vault-unseal "$(vault unseal --keys-file "$keys")"
state=$(vault status); record vault-initialised-and-unsealed "$state"
[ "$(jq -r '.initialized' <<<"$state") $(jq -r '.sealed' <<<"$state")" = "true false" ] || fail vault-state "$state"
for _ in $(seq 40); do vault configure --keys-file "$keys" > "$work/configure.json" 2>/dev/null && break; sleep 1; done
record vault-configure "$(cat "$work/configure.json")"
# Token lifecycle: orphan secret-operator token, then the initial root token is revoked.
record operator-token-created "$(vault operator-token --keys-file "$keys" --token-out "$operator")"
record root-token-retired "$(vault retire-root --keys-file "$keys")"
if vault versions --keys-file "$keys" --path feature-api/feast-redis >/dev/null 2>&1; then fail root-token-rejected "root token still works"; fi
record root-token-rejected '{"result": "denied"}'
if vault revoke-role --token-file "$operator" --role fraud-api >/dev/null 2>&1; then fail operator-cannot-administer "operator changed a role"; fi
record operator-token-cannot-administer '{"result": "denied", "attempt": "delete auth role"}'
record vault-tls-verifies "$(curl -s --cacert "$work/ca.crt" -o /dev/null -w '{"https_status": %{http_code}, "ssl_verify_result": %{ssl_verify_result}}' "https://127.0.0.1:$PORT/v1/sys/health")"
if curl -s -o /dev/null --cacert "$work/untrusted-ca.crt" "https://127.0.0.1:$PORT/v1/sys/health"; then
  fail vault-tls-wrong-ca "an untrusted CA was accepted"
fi
record vault-tls-wrong-ca-rejected '{"result": "denied"}'

# ---------------------------------------------------------------- synthetic secrets and Redis
redis_v1=$(openssl rand -hex 20); redis_v2=$(openssl rand -hex 20); agent_key=$(openssl rand -hex 24)
conn() { printf 'redis.%s.svc.cluster.local:6379,password=%s' "$APP" "$1"; }
printf '%s' "$(conn "$redis_v1")" | vault put --token-file "$operator" --path feature-api/feast-redis --key connection_string --stdin >/dev/null
printf '%s' "$(openssl rand -hex 12)" | vault put --token-file "$operator" --path fraud-api/feast-redis --key connection_string --stdin >/dev/null
record vault-kv-seeded "$(vault versions --token-file "$operator" --path feature-api/feast-redis)"

# The existing, out-of-band Secrets: Redis's own password and the Feature API's current Secret.
k -n "$APP" create secret generic redis-local-auth --from-literal=password="$redis_v1" >/dev/null
k -n "$APP" create secret generic feast-online-redis --from-literal=connection_string="$(conn "$redis_v1")" >/dev/null
legacy_version=$(k -n "$APP" get secret feast-online-redis -o jsonpath='{.metadata.resourceVersion}')
k -n "$APP" apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: redis}
spec:
  selector: {matchLabels: {app: redis}}
  template:
    metadata: {labels: {app: redis}}
    spec:
      containers:
        - name: redis
          image: redis:7.4.2-alpine
          command: [sh, -c, 'exec redis-server --requirepass "\$REDIS_PASSWORD" --save "" --appendonly no']
          env: [{name: REDIS_PASSWORD, valueFrom: {secretKeyRef: {name: redis-local-auth, key: password}}}]
          ports: [{containerPort: 6379}]
---
apiVersion: v1
kind: Service
metadata: {name: redis}
spec:
  selector: {app: redis}
  ports: [{port: 6379}]
YAML

# ---------------------------------------------------------------- ESO
h install external-secrets "$eso_chart" -n "$ENS" -f "$root/infra/kubernetes/external-secrets/values.yaml" --wait --timeout 5m >/dev/null
record eso-ready "$(k -n "$ENS" get deploy -o json | jq -c '{deployments: [.items[] | {name: .metadata.name, ready: .status.readyReplicas}], image: .items[0].spec.template.spec.containers[0].image}')"
create < "$root/infra/kubernetes/external-secrets/ml-platform.yaml"
k -n "$APP" wait --for=condition=Ready secretstore/vault --timeout=120s >/dev/null || fail secretstore-ready "SecretStore ml-platform/vault not Ready"
record secretstore-ready "$(k -n "$APP" get secretstore vault -o json | jq -c '{store: .metadata.name, ready: (.status.conditions[] | select(.type=="Ready") | .status), reason: (.status.conditions[] | select(.type=="Ready") | .reason)}')"
es_wait "$APP" feature-api-redis True 120 || fail externalsecret-synced "feature-api-redis did not sync: $(es_condition "$APP" feature-api-redis) $(es_error "$APP" feature-api-redis)"
want=$(sha "$(conn "$redis_v1")"); got=$(secret_sha "$APP" feature-api-redis-vault connection_string)
owner=$(k -n "$APP" get secret feature-api-redis-vault -o jsonpath='{.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}')
keys_present=$(k -n "$APP" get secret feature-api-redis-vault -o json | jq -c '.data | keys')
record externalsecret-synced "$(jq -cn --arg c "$(es_condition "$APP" feature-api-redis)" --arg w "$want" --arg g "$got" \
  --arg o "$owner" --argjson keys "$keys_present" '{condition: $c, target: "feature-api-redis-vault", keys: $keys, owner: $o, value_sha256_prefix: $g, matches_vault: ($w == $g)}')"
[ "$want" = "$got" ] || fail externalsecret-value "target Secret does not match Vault"
[ "$(k -n "$APP" get secret feast-online-redis -o jsonpath='{.metadata.resourceVersion}')" = "$legacy_version" ] \
  || fail legacy-secret-untouched "feast-online-redis changed"
record legacy-secret-untouched '{"secret": "feast-online-redis", "resource_version_unchanged": true, "owner": "none"}'
shadow() { bash "$root/scripts/vault/shadow_compare.sh" "$CONTEXT" "$APP" "$@" || true; }
status=$(shadow feast-online-redis feature-api-redis-vault); missing=$(shadow feast-online-redis feature-api-redis-absent)
record shadow-compare "$(jq -cn --arg s "$status" --arg m "$missing" '{equal_values: $s, absent_shadow: $m}')"
[ "$status" = SHADOW_MATCH ] && [ "$missing" = SHADOW_SHADOW_SECRET_MISSING ] || fail shadow-compare "$status / $missing"

# ---------------------------------------------------------------- real Feature API on the ESO Secret
cat > "$work/local-serving.yaml" <<YAML
featureApi:
  enabled: true
  image: $feature_image
  serviceAccount: {gcpServiceAccount: ewallet-feature-api@local-test.iam.gserviceaccount.com}
  registryUri: /registry/registry.db
  nodeSelector: null
  resources: {requests: {cpu: 100m, memory: 384Mi}, limits: {cpu: "1", memory: 1Gi}}
YAML
k -n "$APP" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: feast-registry}
spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 100Mi}}}
---
apiVersion: v1
kind: Pod
metadata: {name: feast-seed}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext: {runAsUser: 65532, runAsGroup: 65532, fsGroup: 65532}
  containers:
    - name: seed
      image: $seed_image
      imagePullPolicy: IfNotPresent
      command: [python, -c]
      args:
        - |
          # Disposable local Redis: call the operator's apply/materialize directly. Its CLI
          # guard deliberately accepts only the approved production/verification bindings.
          import json
          from datetime import datetime
          from pathlib import Path
          from tests.integration.test_feast_online_operator import _write_offline
          from ai_platform.features.feast import online_operator
          from ai_platform.ml.training.contract import HISTORICAL_FEATURE_COLUMNS
          _write_offline(Path("/tmp/offline"))
          store = online_operator.get_online_store()
          online_operator.apply(store)
          online_operator.materialize(store, datetime.fromisoformat("2026-05-31T22:00:00+00:00"),
                                      datetime.fromisoformat("2026-06-01T00:00:00+00:00"))
          print(json.dumps(list(HISTORICAL_FEATURE_COLUMNS[:3])))
      env:
        - {name: EWALLET_FEAST_REGISTRY, value: /registry/registry.db}
        - {name: EWALLET_FEAST_OFFLINE_DATA_ROOT, value: /tmp/offline}
        - name: EWALLET_REDIS_CONNECTION_STRING
          valueFrom: {secretKeyRef: {name: feature-api-redis-vault, key: connection_string}}
      volumeMounts: [{name: registry, mountPath: /registry}]
  volumes: [{name: registry, persistentVolumeClaim: {claimName: feast-registry}}]
YAML
k -n "$APP" rollout status deploy/redis --timeout=120s >/dev/null
for _ in $(seq 300); do
  phase=$(k -n "$APP" get pod feast-seed -o jsonpath='{.status.phase}')
  case "$phase" in Succeeded) break ;; Failed) fail feast-seed "$(k -n "$APP" logs feast-seed --tail=3 2>&1 | tail -1)" ;; esac
  sleep 2
done
[ "$phase" = Succeeded ] || fail feast-seed "seed did not finish"
feature_names=$(k -n "$APP" logs feast-seed | tail -1)
record feast-materialized-through-eso-secret "$(jq -cn --argjson n "$feature_names" '{result: "materialized", feature_names: $n}')"

rendered=$(render_feature_api -f "$root/infra/helm/ewallet-serving/values-vault-eso.yaml")
env_check=$(python3 -c '
import sys, yaml, json
dep = [d for d in yaml.safe_load_all(sys.stdin) if d and d["kind"] == "Deployment"][0]
env = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
print(json.dumps({"secret_ref": env["EWALLET_REDIS_CONNECTION_STRING"]["valueFrom"]["secretKeyRef"],
                  "direct_vault_env": sorted(n for n in env if n.startswith("EWALLET_VAULT_"))}))' <<<"$rendered")
record feature-api-rendered-with-overlay "$env_check"
[ "$(jq -r '.secret_ref.name + " " + (.direct_vault_env | length | tostring)' <<<"$env_check")" = "feature-api-redis-vault 0" ] \
  || fail feature-api-rendered-with-overlay "$env_check"
k -n "$APP" apply -f - <<<"$rendered" >/dev/null
k -n "$APP" rollout status deploy/feature-api --timeout=600s >/dev/null || fail feature-api-ready "$(k -n "$APP" describe pod -l app.kubernetes.io/name=feature-api | tail -15)"
forward_feature
read -r status count < <(lookup)
record feature-api-lookup-eso-secret "$(jq -cn --arg s "$status" --arg c "$count" '{http_status: ($s|tonumber), features_returned: ($c|tonumber), readiness: "ready (Feast registry and Redis reached)"}')"
[ "$status" = 200 ] && [ "$count" = 3 ] || fail feature-api-lookup "lookup returned $status"

# ---------------------------------------------------------------- rollback to the existing Secret, then forward again
k -n "$APP" apply -f - <<<"$(render_feature_api)" >/dev/null
k -n "$APP" rollout status deploy/feature-api --timeout=600s >/dev/null
ref=$(k -n "$APP" get deploy feature-api -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="EWALLET_REDIS_CONNECTION_STRING")].valueFrom.secretKeyRef.name}')
forward_feature; read -r status count < <(lookup)
record rollback-to-existing-secret "$(jq -cn --arg r "$ref" --arg s "$status" '{secret_ref: $r, http_status: ($s|tonumber)}')"
[ "$ref" = feast-online-redis ] && [ "$status" = 200 ] || fail rollback "rollback lookup $status via $ref"
k -n "$APP" apply -f - <<<"$rendered" >/dev/null
k -n "$APP" rollout status deploy/feature-api --timeout=600s >/dev/null
forward_feature; read -r status count < <(lookup)
record switch-forward-again "$(jq -cn --arg s "$status" '{secret_ref: "feature-api-redis-vault", http_status: ($s|tonumber)}')"
[ "$status" = 200 ] || fail switch-forward "lookup $status"

# ---------------------------------------------------------------- second namespace and isolation
create < "$root/infra/kubernetes/external-secrets/ewallet-agents.yaml"
# The reader may read this path, but nothing is stored there yet: a genuinely missing secret.
negative neg-missing-vault-secret "$AGENTS" agentgateway-client-key-vault agentgateway-client-key-vault vault-secret-missing
printf '%s' "$agent_key" | vault put --token-file "$operator" --path agent-platform/agentgateway-client-key --key api-key --stdin >/dev/null
force_sync "$AGENTS" agentgateway-client-key-vault
es_wait "$AGENTS" agentgateway-client-key-vault True 120 || fail agents-shadow-sync "$(es_error "$AGENTS" agentgateway-client-key-vault)"
got=$(secret_sha "$AGENTS" agentgateway-client-key-vault api-key)
record agents-shadow-secret-synced "$(jq -cn --arg g "$got" --arg w "$(sha "$agent_key")" '{namespace: "ewallet-agents", target: "agentgateway-client-key-vault", consumers: 0, matches_vault: ($g == $w)}')"
[ "$got" = "$(sha "$agent_key")" ] || fail agents-shadow-value "mismatch"

negative_es neg-ml-reads-fraud vault fraud-api/feast-redis connection_string
negative neg-ml-platform-reads-fraud-api-path "$APP" neg-ml-reads-fraud neg-ml-reads-fraud vault-permission-denied
negative_es neg-ml-reads-agents vault agent-platform/agentgateway-client-key api-key
negative neg-ml-platform-reads-agent-path "$APP" neg-ml-reads-agents neg-ml-reads-agents vault-permission-denied
negative_es neg-agents-reads-feature vault feature-api/feast-redis connection_string "$AGENTS"
negative neg-agents-reads-feature-api-path "$AGENTS" neg-agents-reads-feature neg-agents-reads-feature vault-permission-denied
negative_store neg-borrowed-role "$AGENTS" eso-vault-reader vault eso-ml-platform vault-ca
negative_es neg-borrowed-role neg-borrowed-role feature-api/feast-redis connection_string "$AGENTS"
negative neg-agents-reader-uses-ml-platform-role "$AGENTS" neg-borrowed-role neg-borrowed-role role-bound-to-other-namespace

# ---------------------------------------------------------------- other negative cases (ml-platform)
negative_store neg-wrong-sa "$APP" feature-api vault eso-ml-platform vault-ca
negative_es neg-wrong-sa neg-wrong-sa feature-api/feast-redis connection_string
negative neg-application-service-account "$APP" neg-wrong-sa neg-wrong-sa service-account-not-bound-to-role
negative_store neg-wrong-audience "$APP" eso-vault-reader kubernetes eso-ml-platform vault-ca
negative_es neg-wrong-audience neg-wrong-audience feature-api/feast-redis connection_string
negative neg-wrong-token-audience "$APP" neg-wrong-audience neg-wrong-audience token-audience-rejected
negative_store neg-wrong-ca "$APP" eso-vault-reader vault eso-ml-platform vault-ca-untrusted
negative_es neg-wrong-ca neg-wrong-ca feature-api/feast-redis connection_string
negative neg-untrusted-ca "$APP" neg-wrong-ca neg-wrong-ca tls-untrusted-ca
negative_store neg-wrong-mount "$APP" eso-vault-reader vault eso-ml-platform vault-ca kubernetes-missing
negative_es neg-wrong-mount neg-wrong-mount feature-api/feast-redis connection_string
negative neg-wrong-auth-mount "$APP" neg-wrong-mount neg-wrong-mount vault-login-denied
negative_es neg-missing-property vault feature-api/feast-redis no_such_property
negative neg-missing-property "$APP" neg-missing-property neg-missing-property vault-key-missing
if k -n "$APP" apply -f - >/dev/null 2>"$work/invalid.txt" <<YAML
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata: {name: neg-invalid, labels: {$LABEL: "$RUN"}}
spec:
  provider:
    vault: {server: "https://vault.$VNS.svc.cluster.local:8200", path: ewallet, version: v3, auth: {kubernetes: {role: eso-ml-platform, serviceAccountRef: {name: eso-vault-reader}}}}
YAML
then fail neg-invalid-secretstore "an invalid KV version was accepted"; fi
record neg-invalid-secretstore-rejected "$(jq -cn --arg e "$(grep -o 'Unsupported value[^,]*' "$work/invalid.txt" | head -1)" '{result: "rejected at admission", error: $e}')"
k -n "$APP" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: neg-missing-target, labels: {$LABEL: "$RUN"}}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  containers:
    - name: consumer
      image: redis:7.4.2-alpine
      command: [sh, -c, 'true']
      env: [{name: EWALLET_REDIS_CONNECTION_STRING, valueFrom: {secretKeyRef: {name: feature-api-redis-missing, key: connection_string}}}]
YAML
for _ in $(seq 60); do
  reason=$(k -n "$APP" get pod neg-missing-target -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}')
  [ "$reason" = CreateContainerConfigError ] && break; sleep 1
done
record neg-consumer-missing-target-secret "$(jq -cn --arg r "$reason" '{result: (if $r == "CreateContainerConfigError" then "not started" else "unexpected" end), reason: $r}')"
[ "$reason" = CreateContainerConfigError ] || fail neg-missing-target "pod started without its Secret"

allowed=$(vault_client_pod np-allowed fraud-api); denied=$(vault_client_pod np-denied netpol-probe)
record vault-network-policy "$(jq -cn --arg a "$allowed" --arg d "$denied" '{listed_pod_exit: ($a|tonumber), unlisted_pod_exit: ($d|tonumber), enforced: ($a == "0" and $d != "0")}')"
[ "$allowed" = 0 ] && [ "$denied" != 0 ] || fail vault-network-policy "allowed=$allowed denied=$denied"

# ---------------------------------------------------------------- rotation (synthetic Redis password)
record rotate-vault "$(printf '%s' "$(conn "$redis_v2")" | vault put --token-file "$operator" --path feature-api/feast-redis --key connection_string --stdin)"
printf 'AUTH %s\nCONFIG SET requirepass %s\n' "$redis_v1" "$redis_v2" | k -n "$APP" exec -i deploy/redis -- redis-cli >/dev/null
force_sync "$APP" feature-api-redis
for _ in $(seq 60); do [ "$(secret_sha "$APP" feature-api-redis-vault connection_string)" = "$(sha "$(conn "$redis_v2")")" ] && break; sleep 1; done
got=$(secret_sha "$APP" feature-api-redis-vault connection_string)
record rotate-eso-synced "$(jq -cn --arg g "$got" --arg w "$(sha "$(conn "$redis_v2")")" --argjson v "$(vault versions --token-file "$operator" --path feature-api/feast-redis | jq .current_version)" \
  '{vault_version: $v, target_value_sha256_prefix: $g, matches_new_version: ($g == $w)}')"
[ "$got" = "$(sha "$(conn "$redis_v2")")" ] || fail rotate-eso-synced "target did not change"
status=$(shadow feast-online-redis feature-api-redis-vault)
record shadow-compare-after-rotation "$(jq -cn --arg s "$status" '{status: $s, note: "feast-online-redis was not rotated"}')"
[ "$status" = SHADOW_MISMATCH ] || fail shadow-compare-after-rotation "$status"
k -n "$APP" rollout restart deploy/feature-api >/dev/null
k -n "$APP" rollout status deploy/feature-api --timeout=600s >/dev/null || fail rotate-consumer "Feature API not ready on the new credential"
forward_feature; read -r status count < <(lookup)
record rotate-consumer-uses-new-credential "$(jq -cn --arg s "$status" '{restart: "rollout restart", http_status: ($s|tonumber)}')"
[ "$status" = 200 ] || fail rotate-consumer "lookup $status"

# ---------------------------------------------------------------- Vault unavailable, sealed, persistence
k -n "$VNS" scale statefulset vault --replicas=0 >/dev/null
k -n "$VNS" wait --for=delete pod/vault-0 --timeout=120s >/dev/null
force_sync "$APP" feature-api-redis
es_wait "$APP" feature-api-redis False 90 || fail vault-unavailable "ExternalSecret still reports synced"
await_class vault-unavailable "$APP" feature-api-redis vault-unreachable 60s
record vault-unavailable "$(jq -cn --arg c "$(es_condition "$APP" feature-api-redis)" --arg e "$(es_error "$APP" feature-api-redis 60s)" \
  --arg g "$(secret_sha "$APP" feature-api-redis-vault connection_string)" '{externalsecret: $c, error_class: $e, retained_secret_sha256_prefix: $g, note: "Secret retained; ESO is NOT syncing"}')"
k -n "$APP" rollout restart deploy/feature-api >/dev/null
k -n "$APP" rollout status deploy/feature-api --timeout=600s >/dev/null || fail vault-unavailable-consumer "Feature API did not restart on the retained Secret"
forward_feature; read -r status count < <(lookup)
record vault-unavailable-consumer-restart "$(jq -cn --arg s "$status" '{http_status: ($s|tonumber), secret_source: "retained Kubernetes Secret"}')"
[ "$status" = 200 ] || fail vault-unavailable-consumer "lookup $status"

k -n "$VNS" scale statefulset vault --replicas=1 >/dev/null
k -n "$VNS" wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=240s >/dev/null
forward_vault
state=$(vault status); record vault-restart-is-sealed "$state"
[ "$(jq -r .sealed <<<"$state")" = true ] || fail vault-restart-sealed "Vault came back unsealed"
force_sync "$APP" feature-api-redis
await_class sync-while-sealed "$APP" feature-api-redis vault-sealed 30s
record sync-while-sealed "$(jq -cn --arg c "$(es_condition "$APP" feature-api-redis)" --arg e "$(es_error "$APP" feature-api-redis 15s)" '{externalsecret: $c, error_class: $e}')"
[ "$(es_condition "$APP" feature-api-redis | cut -d' ' -f1)" = False ] || fail sync-while-sealed "synced while sealed"
record vault-manual-unseal "$(vault unseal --keys-file "$keys")"
sleep 5
record vault-data-persisted "$(vault versions --token-file "$operator" --path feature-api/feast-redis | jq -c '{current_version}')"
force_sync "$APP" feature-api-redis
es_wait "$APP" feature-api-redis True 120 || fail sync-resumed "ESO did not resume"
record eso-sync-resumed "$(jq -cn --arg c "$(es_condition "$APP" feature-api-redis)" --arg g "$(secret_sha "$APP" feature-api-redis-vault connection_string)" \
  --arg w "$(sha "$(conn "$redis_v2")")" '{externalsecret: $c, matches_version_2: ($g == $w)}')"

# ---------------------------------------------------------------- later administration
# Vault 2.x authenticates root generation (no enable_unauthenticated_access). The owner logs in
# as Kubernetes role vault-admin (policy root-generation only), then feeds 2 unseal shares.
admin_jwt() { k -n "$VNS" create token vault-admin --audience vault --duration 10m; }
code=$(python3 -c '
import sys, urllib.request, urllib.error, ssl
ctx = ssl.create_default_context(cafile=sys.argv[2])
req = urllib.request.Request(sys.argv[1] + "/v1/sys/generate-root/attempt", data=b"{}", method="POST")
try:
    print(urllib.request.urlopen(req, context=ctx, timeout=10).status)
except urllib.error.HTTPError as error:
    print(error.code)' "https://127.0.0.1:$PORT" "$work/ca.crt")   # no X-Vault-Token header
[ "$code" = 403 ] || fail generate-root-unauthenticated "expected 403 without a token, got $code"
record generate-root-unauthenticated-rejected "$(jq -cn --argjson c "$code" '{result: "denied", http_status: $c}')"
if vault generate-root --keys-file "$keys" --token-file "$operator" --token-out "$work/x2.json" >/dev/null 2>&1; then
  fail generate-root-operator-token "the operator token started root generation"
fi
record generate-root-operator-token-rejected '{"result": "denied"}'
if k -n "$APP" create token eso-vault-reader --audience vault --duration 10m \
   | vault login --role vault-admin --token-out "$work/x3.json" >/dev/null 2>&1; then
  fail admin-login-wrong-account "an ESO reader logged in as vault-admin"
fi
record admin-login-wrong-account-rejected '{"result": "denied", "account": "ml-platform/eso-vault-reader"}'
record admin-login "$(admin_jwt | vault login --role vault-admin --token-out "$work/admin-login.json")"
if vault configure --token-file "$work/admin-login.json" >/dev/null 2>&1 \
   || vault versions --token-file "$work/admin-login.json" --path feature-api/feast-redis >/dev/null 2>&1; then
  fail admin-login-scope "the admin login did more than root generation"
fi
record admin-login-limited-to-root-generation '{"configure": "denied", "secret_metadata": "denied"}'
python3 -c 'import json,sys; k=json.load(open(sys.argv[1])); json.dump({"keys_base64": k["keys_base64"][:1]}, open(sys.argv[2], "w"))' "$keys" "$work/one-share.json"
chmod 600 "$work/one-share.json"
if vault generate-root --keys-file "$work/one-share.json" --token-file "$work/admin-login.json" --token-out "$work/x4.json" >/dev/null 2>"$work/one-share.err"; then
  fail generate-root-one-share "one share produced a root token"
fi
grep -q vault_generate_root_incomplete "$work/one-share.err" || fail generate-root-one-share "$(cut -c1-120 "$work/one-share.err")"
[ ! -e "$work/x4.json" ] || fail generate-root-one-share "a token file was written"
record generate-root-below-threshold-rejected '{"result": "incomplete", "shares": 1, "threshold": 2, "attempt": "cancelled"}'
record admin-generate-root "$(vault generate-root --keys-file "$keys" --token-file "$work/admin-login.json" --token-out "$admin")"
record admin-reconfigure "$(vault configure --token-file "$admin")"
# Operator token expiry: a short-lived token expires; the admin path issues a replacement.
record operator-token-short-ttl "$(vault operator-token --token-file "$admin" --ttl 20s --token-out "$work/operator-short.json")"
record operator-token-replacement "$(vault operator-token --token-file "$admin" --token-out "$work/operator-new.json")"
record admin-root-retired "$(vault retire-root --token-file "$admin")"
[ -f "$admin" ] || fail admin-root-rejected "no generated root token to test"
if vault configure --token-file "$admin" >/dev/null 2>&1; then fail admin-root-rejected "generated root still works"; fi
record admin-root-rejected '{"result": "denied"}'
sleep 25
if vault versions --token-file "$work/operator-short.json" --path feature-api/feast-redis >/dev/null 2>&1; then
  fail operator-token-expired "an expired operator token still works"
fi
record operator-token-expired-rejected '{"result": "denied", "ttl": "20s"}'
record operator-token-replacement-works "$(vault versions --token-file "$work/operator-new.json" --path feature-api/feast-redis | jq -c '{current_version}')"
record operator-token-still-valid "$(vault versions --token-file "$operator" --path feature-api/feast-redis | jq -c '{current_version}')"

# ---------------------------------------------------------------- audit
audit=$(k -n "$VNS" exec vault-0 -- sh -c 'wc -l < /vault/audit/audit.log')
leaks=$(k -n "$VNS" exec -i vault-0 -- sh -c 'grep -c -F -f - /vault/audit/audit.log || true' <<<"$(printf '%s\n%s\n%s\n' "$redis_v1" "$redis_v2" "$agent_key")")
logins=$(k -n "$VNS" exec vault-0 -- sh -c 'grep -c "auth/kubernetes/login" /vault/audit/audit.log || true')
record vault-audit-log "$(jq -cn --argjson l "$audit" --argjson x "$leaks" --argjson n "$logins" '{lines: $l, kubernetes_login_entries: $n, plaintext_secret_matches: $x}')"
[ "$leaks" = 0 ] && [ "$audit" -gt 0 ] || fail vault-audit "audit log check failed"
echo "LOCAL_VAULT_ESO_VALIDATION_PASS"
