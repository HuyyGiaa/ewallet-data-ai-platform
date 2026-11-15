#!/usr/bin/env bash
# Local validation of PKG-K and PKG-W: install kagent 0.10.3 (repo values + pilot post-renderer)
# and KEDA 2.21.0 (repo values) from the pinned charts on an existing kind cluster, then run
# scripts/llm_agent/sandbox_watch.sh through preflight, diff, dry-run, a refused upgrade,
# upgrade, verify and rollback for each. Writes summary.json.
#
# usage: SANDBOX_WATCH_KIND_CONTEXT=kind-<name> sandbox_watch_kind.sh <evidence-dir>
# Refuses a cluster that already has kagent, KEDA or Substrate CRDs, or any of the namespaces
# below; cleanup removes only what this run installed (including a temporary node label).
# kagent 0.10.3 exits when it cannot dial ate-api, so the run serves a TLS gRPC stub (no
# services) at api.ate-system.svc:443 with a certificate from a throwaway CA; the CA goes into
# the substrate-ateapi-ca ConfigMap that the controller trusts, as on GKE.
# Limits: no real ate-api or LLM on kind; the controller starts and watches, agents do not run.
set -euo pipefail
out=${1:?evidence directory required}
ctx=${SANDBOX_WATCH_KIND_CONTEXT:-}
[[ "$ctx" == kind-* ]] || { echo "SANDBOX_WATCH_KIND_CONTEXT must name a kind context" >&2; exit 2; }
root=$(cd "$(dirname "$0")/../.." && pwd)
# shellcheck source=../../infra/kubernetes/kagent-pilot/versions.env
. "$root/infra/kubernetes/kagent-pilot/versions.env"
# shellcheck source=../../infra/helm/keda/versions.env
. "$root/infra/helm/keda/versions.env"
mkdir -p "$out"; out=$(cd "$out" && pwd)
work=$(mktemp -d); results=$out/steps.tsv; : > "$results"
namespaces=(kagent keda ate-system ewallet-agents ml-platform kagent-pilot ewallet-sandbox)
k() { kubectl --context "$ctx" --request-timeout=60s "$@"; }
h() { helm --kube-context "$ctx" "$@"; }
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$out/run.log"; }

if k get crd -o name | grep -qE 'kagent\.dev|keda\.sh|ate\.dev'; then echo "cluster already has kagent, KEDA or Substrate CRDs" >&2; exit 2; fi
for ns in "${namespaces[@]}"; do
  if k get namespace "$ns" > /dev/null 2>&1; then echo "namespace $ns already exists" >&2; exit 2; fi
done
node=$(k get nodes -o 'jsonpath={.items[0].metadata.name}')
installed=()
cleanup() {
  set +e
  for release in kagent:kagent keda:keda kagent-crds:kagent substrate-crds:ate-system; do
    [[ " ${installed[*]} " == *" ${release%%:*} "* ]] && h uninstall "${release%%:*}" -n "${release#*:}" --wait --timeout 5m > /dev/null 2>&1
  done
  for crd in $(k get crd -o name | grep -E 'kagent\.dev|keda\.sh|ate\.dev'); do k delete "$crd" --wait=false > /dev/null 2>&1; done
  for ns in "${namespaces[@]}"; do
    [[ "$(k get namespace "$ns" -o 'jsonpath={.metadata.labels.ewallet\.io/kind-test}' 2>/dev/null)" == sandbox-watch ]] \
      && k delete namespace "$ns" --wait=true --timeout=5m > /dev/null 2>&1
  done
  k label node "$node" ewallet.io/g3-llm- > /dev/null 2>&1
  rm -rf "$work"
  log "cleanup of $ctx done"
}
trap cleanup EXIT

for ns in "${namespaces[@]}"; do
  k create namespace "$ns" > /dev/null && k label namespace "$ns" ewallet.io/kind-test=sandbox-watch > /dev/null
done
# The pilot post-renderer pins kagent PostgreSQL to the G3 node, as on GKE.
k label node "$node" ewallet.io/g3-llm=true --overwrite > /dev/null
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$work/ca.key" -out "$work/ca.crt" -days 1 -subj /CN=kind-ateapi-ca > /dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -keyout "$work/tls.key" -out "$work/tls.csr" -subj /CN=api.ate-system.svc > /dev/null 2>&1
printf 'subjectAltName=DNS:api.ate-system.svc,DNS:api.ate-system.svc.cluster.local\n' > "$work/san.ext"
openssl x509 -req -in "$work/tls.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial -days 1 \
  -extfile "$work/san.ext" -out "$work/tls.crt" > /dev/null 2>&1
k -n kagent create configmap substrate-ateapi-ca --from-file=ca.crt="$work/ca.crt" > /dev/null
k -n ate-system create secret tls ateapi-stub-tls --cert="$work/tls.crt" --key="$work/tls.key" > /dev/null
cat <<'YAML' | k apply -f - > /dev/null
apiVersion: v1
kind: ConfigMap
metadata: {name: ateapi-stub, namespace: ate-system}
data:
  stub.py: |
    from concurrent import futures
    import grpc
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    key, cert = open("/tls/tls.key", "rb").read(), open("/tls/tls.crt", "rb").read()
    server.add_secure_port("[::]:8443", grpc.ssl_server_credentials([(key, cert)]))
    server.start()
    print("ate-api stub listening", flush=True)
    server.wait_for_termination()
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: ateapi-stub, namespace: ate-system}
spec:
  replicas: 1
  selector: {matchLabels: {app: ateapi-stub}}
  template:
    metadata: {labels: {app: ateapi-stub}}
    spec:
      containers:
        - name: stub
          image: python:3.12.12-slim-trixie@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c
          command: ["sh", "-c", "pip install --quiet --only-binary=:all: --target /tmp/py grpcio==1.85.1 && PYTHONPATH=/tmp/py exec python /stub/stub.py"]
          ports: [{containerPort: 8443}]
          readinessProbe: {tcpSocket: {port: 8443}, periodSeconds: 2}
          volumeMounts: [{name: tls, mountPath: /tls}, {name: stub, mountPath: /stub}]
      volumes:
        - {name: tls, secret: {secretName: ateapi-stub-tls}}
        - {name: stub, configMap: {name: ateapi-stub}}
---
apiVersion: v1
kind: Service
metadata: {name: api, namespace: ate-system}
spec:
  selector: {app: ateapi-stub}
  ports: [{port: 443, targetPort: 8443}]
YAML
k -n ate-system rollout status deployment/ateapi-stub --timeout=300s > /dev/null || { echo "ate-api stub did not start" >&2; exit 1; }

h pull "$SUBSTRATE_CRDS_CHART" --version "$SUBSTRATE_VERSION" --destination "$work" 2>&1 | grep -q "Digest: $SUBSTRATE_CRDS_DIGEST" \
  || { echo "substrate-crds digest mismatch" >&2; exit 1; }
h install substrate-crds "$work/substrate-crds-$SUBSTRATE_VERSION.tgz" -n ate-system > /dev/null; installed+=(substrate-crds)
h pull "$KAGENT_CRDS_CHART" --version "$KAGENT_VERSION" --destination "$work" 2>&1 | grep -q "Digest: $KAGENT_CRDS_DIGEST" \
  || { echo "kagent-crds digest mismatch" >&2; exit 1; }
h install kagent-crds "$work/kagent-crds-$KAGENT_VERSION.tgz" -n kagent > /dev/null; installed+=(kagent-crds)
h pull "$KAGENT_CHART" --version "$KAGENT_VERSION" --destination "$work" 2>&1 | grep -q "Digest: $KAGENT_CHART_DIGEST" \
  || { echo "kagent chart digest mismatch" >&2; exit 1; }
h install kagent "$work/kagent-$KAGENT_VERSION.tgz" -n kagent \
  -f "$root/infra/kubernetes/kagent-platform/kagent-values.yaml" \
  -f "$root/infra/kubernetes/kagent-pilot/values/kagent-runtime-overlay.yaml" \
  --post-renderer "$root/infra/kubernetes/kagent-pilot/scripts/kagent_runtime_post_renderer.py" > /dev/null
installed+=(kagent)
h pull keda --repo "$KEDA_CHART_REPO" --version "$KEDA_VERSION" --destination "$work" > /dev/null
[[ "$(sha256sum "$work/keda-$KEDA_VERSION.tgz" | cut -d' ' -f1)" == "$KEDA_CHART_TGZ_SHA256" ]] || { echo "keda chart hash mismatch" >&2; exit 1; }
h install keda "$work/keda-$KEDA_VERSION.tgz" -n keda -f "$root/infra/helm/keda/values.yaml" --wait --timeout 5m > /dev/null
installed+=(keda)
k -n kagent rollout status deployment/kagent-controller --timeout=420s > "$out/kagent-controller-rollout.txt" 2>&1 \
  || log "kagent-controller did not become Ready (see kagent-controller-rollout.txt)"
log "installed: ${installed[*]}"

step() {   # step <target> <label> <expect pass|fail> <command...>
  local target=$1 label=$2 expect=$3; shift 3
  local status=pass
  "$@" > "$out/$target-$label.log" 2>&1 || status=fail
  printf '%s\t%s\t%s\t%s\n' "$target" "$label" "$expect" "$status" >> "$results"
  log "$target $label: $status (expected $expect)"
}
for target in keda kagent; do
  package=PKG-W; [[ $target == kagent ]] && package=PKG-K
  run=(env KUBE_CONTEXT="$ctx" "$root/scripts/llm_agent/sandbox_watch.sh" "$target")
  step "$target" preflight pass "${run[@]}" preflight "$work/pkg"
  step "$target" diff pass "${run[@]}" diff "$work/pkg"
  step "$target" dry-run pass "${run[@]}" dry-run "$work/pkg"
  step "$target" upgrade-without-approval fail "${run[@]}" upgrade "$work/pkg"
  other=PKG-K; [[ $package == PKG-K ]] && other=PKG-W
  step "$target" upgrade-with-other-package-approval fail env WATCH_APPROVAL="$other:$ctx" "${run[@]}" upgrade "$work/pkg"
  step "$target" upgrade pass env WATCH_APPROVAL="$package:$ctx" "${run[@]}" upgrade "$work/pkg"
  step "$target" verify pass "${run[@]}" verify "$work/pkg"
  step "$target" rollback-without-approval fail "${run[@]}" rollback "$work/pkg"
  step "$target" rollback pass env WATCH_APPROVAL="$package-ROLLBACK:$ctx" "${run[@]}" rollback "$work/pkg"
  cp "$work/pkg/$target/package.log" "$out/$target-package.log" 2>/dev/null || true
  cp "$work/pkg/$target/diff.txt" "$out/$target-diff.txt" 2>/dev/null || true
done
python3 - "$results" "$out/summary.json" <<'PY'
import json, sys
rows = [line.rstrip("\n").split("\t") for line in open(sys.argv[1]) if line.strip()]
steps = [{"target": t, "step": s, "expected": e, "observed": o, "ok": e == o} for t, s, e, o in rows]
summary = {"verdict": "PACKAGES_VALIDATED" if steps and all(x["ok"] for x in steps) else "NOT_VALIDATED", "steps": steps,
           "limits": ["kind: no ate-api, LLM or real agents; the kagent controller starts with the new watch scope",
                      "values: repo kagent-values.yaml + pilot runtime overlay, not the live values"]}
json.dump(summary, open(sys.argv[2], "w"), indent=1)
print(json.dumps({"verdict": summary["verdict"], "failed": [x for x in steps if not x["ok"]]}, indent=1))
PY
