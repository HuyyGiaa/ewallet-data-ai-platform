# kagent bounded pilot runbook

Status: prepared only; not deployed. This is a compatibility pilot, not a production design.

## Purpose and boundary

The pilot proves one path only:

```text
SandboxAgent (kagent.dev/v1alpha2)
  -> ModelConfig
  -> mock-openai.kagent.svc.cluster.local:8080/v1/chat/completions
```

It contains no MCP, A2A tools, Istio, Gateway API, Feast, KServe, RAG, vLLM,
external ingress, external model API, or GCP IAM binding. The controller's A2A
HTTP endpoint is reached only by local `kubectl port-forward`.

## Immutable version inventory

The executable pins and OCI manifest digests are in
`infra/kubernetes/kagent-pilot/versions.env`:

| Component | OCI chart | Version |
| --- | --- | --- |
| kagent CRDs | `ghcr.io/kagent-dev/kagent/helm/kagent-crds` | `0.10.3` |
| kagent | `ghcr.io/kagent-dev/kagent/helm/kagent` | `0.10.3` |
| Substrate CRDs | `ghcr.io/kagent-dev/substrate/helm/substrate-crds` | `0.0.9` |
| Agent Substrate | `ghcr.io/kagent-dev/substrate/helm/substrate` | `0.0.9` |

`scripts/render.sh` fails if Helm reports a chart digest different from the
recorded digest. The WorkerPool image is pinned to the approved `v0.0.9` tag.
The mock base image is pinned by multi-architecture manifest digest.

## Helm reductions and resource envelope

| Change | Value | Support status / reason |
| --- | --- | --- |
| JWT auth | `auth.mode=jwt` | Native Substrate value; mTLS is not enabled. |
| JWT issuer | exact GKE issuer | Native Substrate value. |
| Valkey | 6 replicas, 256 Mi PVC each | Replica count is a value, but 0.0.9's init Job hard-codes pods 0..5 and one replica per primary. Six is the smallest safe value; lowering it would hang. PVC reduction is native. |
| RustFS | 1 replica, 256 Mi PVC | Native storage value; retains session snapshot functionality. |
| PostgreSQL | 1 replica, 256 Mi PVC | Native bundled development DB values. |
| kagent UI | 0 replicas | Native replica value; API and controller remain available. |
| bundled tools/agents | disabled | Native `enabled=false` values. |
| WorkerPool | 1 replica | Native kagent chart value and the requested bounded capacity. |
| Substrate control plane | 1 replica each | Chart fixed defaults. Version 0.0.9 exposes no resource values. A deterministic Helm post-renderer adds requests/limits without deleting or renaming chart resources. |

The requested resource envelope is:

| Workload | CPU request / limit | Memory request / limit |
| --- | --- | --- |
| kagent controller | 100m / 750m | 128 Mi / 512 Mi |
| bundled PostgreSQL | 100m / 500m | 128 Mi / 384 Mi |
| WorkerPool | 500m / 1500m | 1 Gi / 2 Gi |
| SandboxAgent actor declaration | 100m / 500m | 128 Mi / 512 Mi |
| mock endpoint | 10m / 100m | 32 Mi / 64 Mi |
| Substrate API and atelet, each | 100m / 500m | 128 Mi / 512 Mi |
| RustFS | 50m / 500m | 128 Mi / 512 Mi |
| each Valkey pod | 25m / 250m | 64 Mi / 256 Mi |
| remaining Substrate containers | 25m / 250m | 64 Mi / 256 Mi |

These fit as a bounded request envelope on one `e2-standard-4`; actual startup
must still be observed because privileged atelet/gVisor and six Valkey pods are
preview software with non-trivial overhead.

## Security properties

- `kagent` and `ate-system` are the only pilot namespaces.
- kagent watches only `kagent`; its chart renders namespaced Role/RoleBinding
  resources through `rbac.namespaces=[kagent]`.
- Substrate's own chart still needs its upstream cluster-scoped CRDs and least
  privilege controller/node RBAC. No `cluster-admin` binding is added here.
- Mock, SandboxAgent and WorkerPool use dedicated ServiceAccounts. Mock and
  SandboxAgent tokens are not mounted; the WorkerPool token is retained because
  Substrate requires it.
- Mock is ClusterIP only, has a same-namespace ingress NetworkPolicy, no egress,
  and runs non-root with a read-only filesystem and all Linux capabilities dropped.
- Sandbox networking allows only the mock Service DNS name.
- The API key Secret contains a fixed non-sensitive compatibility placeholder.
  The OpenAI client interface expects an API-key field; the mock does not verify it.
- No GCP service-account key or IAM binding is required.

## Future USER_RUN deployment

Run from repository root only after explicitly approving the pilot and accepting
the cost of one GKE node. None of these commands have been executed while
preparing this runbook.

### 1. Select cluster and scale 0 -> 1

```bash
export PROJECT_ID=ewallet-data-ai-platform-hg26
export CLUSTER=ewallet-dev
export ZONE=asia-southeast1-b
export NODE_POOL=cpu-pool
export PILOT_DIR="$PWD/infra/kubernetes/kagent-pilot"
export RENDER_DIR="$(mktemp -d /tmp/kagent-pilot-render.XXXXXX)"

gcloud config set project "$PROJECT_ID"
gcloud container clusters get-credentials "$CLUSTER" \
  --zone "$ZONE" --project "$PROJECT_ID"
gcloud container clusters resize "$CLUSTER" \
  --node-pool "$NODE_POOL" --num-nodes 1 \
  --zone "$ZONE" --project "$PROJECT_ID" --quiet

kubectl wait --for=condition=Ready nodes --all --timeout=15m
kubectl get nodes -o wide
```

### 2. Verify local render and create isolated namespaces

```bash
"$PILOT_DIR/scripts/render.sh" "$RENDER_DIR"
source "$PILOT_DIR/versions.env"

kubectl create namespace ate-system
kubectl create namespace kagent
kubectl apply -f "$PILOT_DIR/manifests/security.yaml"
```

### 3. Install pinned Substrate CRDs and control plane

```bash
helm upgrade --install substrate-crds "$RENDER_DIR/charts/substrate-crds" \
  --namespace ate-system --wait --timeout 10m

helm upgrade --install substrate "$RENDER_DIR/charts/substrate" \
  --namespace ate-system \
  --values "$PILOT_DIR/values/substrate.yaml" \
  --post-renderer "$PILOT_DIR/scripts/substrate_resource_post_renderer.py" \
  --wait --timeout 20m

kubectl rollout status deployment/ate-api-server-deployment -n ate-system --timeout=10m
kubectl rollout status deployment/ate-controller -n ate-system --timeout=10m
kubectl rollout status deployment/atenet-router -n ate-system --timeout=10m
kubectl rollout status deployment/dns -n ate-system --timeout=10m
kubectl rollout status deployment/rustfs -n ate-system --timeout=10m
kubectl rollout status daemonset/atelet -n ate-system --timeout=10m
kubectl rollout status statefulset/valkey-cluster -n ate-system --timeout=15m
kubectl wait --for=condition=complete job/rustfs-bucket-init -n ate-system --timeout=10m
kubectl wait --for=condition=complete job/valkey-cluster-init -n ate-system --timeout=15m
```

### 4. Install pinned kagent CRDs and core controller

```bash
helm upgrade --install kagent-crds "$RENDER_DIR/charts/kagent-crds" \
  --namespace kagent --wait --timeout 10m

helm upgrade --install kagent "$RENDER_DIR/charts/kagent" \
  --namespace kagent \
  --values "$PILOT_DIR/values/kagent.yaml" \
  --wait --timeout 20m

kubectl rollout status deployment/kagent-controller -n kagent --timeout=10m
kubectl rollout status deployment/kagent-postgresql -n kagent --timeout=10m
kubectl wait --for=jsonpath='{.status.replicas}'=1 workerpool/kagent-pilot \
  -n kagent --timeout=15m
kubectl get workerpool kagent-pilot -n kagent -o yaml
```

### 5. Deploy the internal mock, ModelConfig and SandboxAgent

```bash
kubectl apply -f "$PILOT_DIR/manifests/mock-openai.yaml"
kubectl rollout status deployment/mock-openai -n kagent --timeout=5m

kubectl apply -f "$PILOT_DIR/manifests/model-config.yaml"
kubectl apply -f "$PILOT_DIR/manifests/sandbox-agent.yaml"

kubectl wait --for=condition=Ready sandboxagent/pilot-sandbox-agent \
  -n kagent --timeout=20m
kubectl get modelconfig,workerpool,sandboxagent,actortemplate -n kagent -o wide
```

If the `SandboxAgent` does not become Ready, stop. Do not add replicas during
this bounded pilot. Collect the evidence commands below, especially messages
containing `no free workers available` or golden snapshot/image-pull failures.

### 6. Port-forward and invoke the SandboxAgent

Terminal A:

```bash
kubectl port-forward -n kagent service/kagent-controller 8083:8083
```

Terminal B:

```bash
cat >/tmp/kagent-pilot-request.json <<'JSON'
{
  "jsonrpc": "2.0",
  "id": "pilot-request-1",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Return the fixed bounded response."}],
      "messageId": "pilot-message-1",
      "contextId": "pilot-session-1"
    }
  }
}
JSON

curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/kagent-pilot-request.json \
  http://127.0.0.1:8083/api/a2a/kagent/pilot-sandbox-agent | tee /tmp/kagent-pilot-response.json

grep -F 'KAGENT_PILOT_OK' /tmp/kagent-pilot-response.json
kubectl logs -n kagent deployment/mock-openai --since=10m | \
  grep -F '"event": "mock_completion"'
```

## Session/suspend-resume evidence

Substrate may snapshot and suspend an idle actor. Reuse the exact same
`contextId` with a new request/message ID after the first response:

```bash
sed 's/pilot-request-1/pilot-request-2/; s/pilot-message-1/pilot-message-2/' \
  /tmp/kagent-pilot-request.json >/tmp/kagent-pilot-request-2.json
sleep 60
curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/kagent-pilot-request-2.json \
  http://127.0.0.1:8083/api/a2a/kagent/pilot-sandbox-agent | \
  tee /tmp/kagent-pilot-response-2.json
grep -F 'KAGENT_PILOT_OK' /tmp/kagent-pilot-response-2.json
```

This verifies deterministic session reuse. A true suspend/resume claim is made
only if the Substrate status/logs below show a snapshot followed by resume; two
successful requests alone are insufficient evidence.

## Evidence collection

```bash
mkdir -p /tmp/kagent-pilot-evidence
kubectl version -o yaml > /tmp/kagent-pilot-evidence/kubernetes-version.yaml
kubectl get pods -A -o wide > /tmp/kagent-pilot-evidence/pods.txt
kubectl get events -A --sort-by=.lastTimestamp > /tmp/kagent-pilot-evidence/events.txt
kubectl get modelconfig,workerpool,sandboxagent,actortemplate -n kagent -o yaml \
  > /tmp/kagent-pilot-evidence/custom-resources.yaml
kubectl describe sandboxagent pilot-sandbox-agent -n kagent \
  > /tmp/kagent-pilot-evidence/sandboxagent-describe.txt
kubectl logs -n kagent deployment/kagent-controller --since=30m \
  > /tmp/kagent-pilot-evidence/kagent-controller.log
kubectl logs -n kagent deployment/mock-openai --since=30m \
  > /tmp/kagent-pilot-evidence/mock-openai.log
kubectl logs -n ate-system deployment/ate-controller --since=30m \
  > /tmp/kagent-pilot-evidence/ate-controller.log
kubectl logs -n ate-system deployment/ate-api-server-deployment --since=30m \
  > /tmp/kagent-pilot-evidence/ate-api-server.log
kubectl logs -n ate-system daemonset/atelet --all-containers --since=30m \
  > /tmp/kagent-pilot-evidence/atelet.log
kubectl top pods -A > /tmp/kagent-pilot-evidence/pod-usage.txt || true
```

## Exact cleanup and rollback

First stop port-forwarding. Then remove application CRs before controllers:

```bash
export PILOT_DIR="$PWD/infra/kubernetes/kagent-pilot"

kubectl delete -f "$PILOT_DIR/manifests/sandbox-agent.yaml" --ignore-not-found --wait
kubectl delete -f "$PILOT_DIR/manifests/model-config.yaml" --ignore-not-found --wait
kubectl delete -f "$PILOT_DIR/manifests/mock-openai.yaml" --ignore-not-found --wait

helm uninstall kagent --namespace kagent --wait
helm uninstall substrate --namespace ate-system --wait
kubectl delete -f "$PILOT_DIR/manifests/security.yaml" --ignore-not-found --wait
```

CRDs are cluster-scoped. Remove their releases only after confirming no other
resources use them:

```bash
kubectl get modelconfig,sandboxagent,agent,agentharness --all-namespaces
kubectl get workerpool,actortemplate --all-namespaces

helm uninstall kagent-crds --namespace kagent --wait
helm uninstall substrate-crds --namespace ate-system --wait
```

Inspect namespace contents before deleting the pilot namespaces:

```bash
kubectl get all,pvc,secret,configmap -n kagent
kubectl get all,pvc,secret,configmap -n ate-system
kubectl delete namespace kagent ate-system --wait
```

Finally return the CPU pool to zero:

```bash
gcloud container clusters resize ewallet-dev \
  --node-pool cpu-pool --num-nodes 0 \
  --zone asia-southeast1-b \
  --project ewallet-data-ai-platform-hg26 --quiet
gcloud container node-pools describe cpu-pool \
  --cluster ewallet-dev --zone asia-southeast1-b \
  --project ewallet-data-ai-platform-hg26 \
  --format='yaml(autoscaling.enabled,initialNodeCount,status)'
```

## Known assumptions and stop conditions

1. Compatibility is **B — pilot possible with risks**. Kubernetes 1.36 is
   expected compatible with the rendered APIs, but this combination is not a
   production support claim.
2. Substrate 0.0.9 runs privileged atelet and uses host state for gVisor. This
   is core functionality and is not weakened or removed by this pilot.
3. One WorkerPool replica is deliberately bounded. Golden actor creation may
   temporarily consume its only worker. `no free workers available` is a STOP
   condition, not permission to scale automatically.
4. `ui.replicas=0` is valid and supported by the chart value, but the pilot is
   tested through the controller A2A endpoint only.
5. The resource post-renderer uses Helm's supported post-renderer interface;
   the actual resource knobs are absent from Substrate chart 0.0.9. Rendered
   manifests must be revalidated when the chart version changes.
6. RustFS and bundled PostgreSQL are pilot-only state. Cleanup deletes it after
   evidence is collected.
