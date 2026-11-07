# Session A approval packages

Six packages, each approved and reversible on its own. **None is approved yet, and none has run.** GKE is asleep at zero nodes. `SOURCE_SHA` is the commit images are built from; digests are recorded at build time.

Node counts add Session A workloads to the Cloud Lab figures by hand, because the recovery manifest predates them. Costs use list prices and include the $0.107/hour sleep cost.

## Shared setup

```bash
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export M=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
export REG=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform
export PATH=$PATH:$HOME/miniconda3/envs/fintech/bin
```

## P-A0: Preflight (read-only)

| Item | Detail |
|---|---|
| Target | None changed |
| Permissions | Read only |
| Cost | $0 beyond sleep |
| Duration | 15 minutes |

```bash
python3 ops/gcp/cloud_lab.py status
python3 ops/gcp/cloud_lab.py cost-status
gcloud compute project-info describe --project ewallet-data-ai-platform-hg26 --format=json | jq '.quotas[]|select(.metric=="CPUS_ALL_REGIONS")'
kubectl --context "$KUBE_CONTEXT" get crd | grep -E 'keda|gateway.networking' || echo "no KEDA or Gateway API CRDs"
helm list -A --kube-context "$KUBE_CONTEXT"
python3 scripts/ci/release_state.py drift
```

Stop if billing is unresolved or the cluster has changed since the recovery manifest.

## P-A1: Serving deployment

| Item | Detail |
|---|---|
| Objective | Deploy the Feature API and a Fraud API image that includes it |
| Target | `ml-platform`: Deployment and Service `feature-api`; Deployment `fraud-api` image; `feature-store`: NetworkPolicy `redis-clients-feature-api` |
| Image | One Fraud API image serves both, digest recorded at build |
| Manifest | `infra/helm/ewallet-serving` |
| Permissions | Cloud Build; Helm in `ml-platform`; one NetworkPolicy in `feature-store` |
| Capacity | 2.1 CPU, 4.2 GiB with system pods: 1 node |
| Cost | $0.285/hour |
| Limit | 2 hours |

```bash
gcloud builds submit --project ewallet-data-ai-platform-hg26 --config ai_platform/inference/cloudbuild.yaml \
  --substitutions "_IMAGE=$REG/fraud-api:$(git rev-parse HEAD)" .
export IMAGE=$REG/fraud-api@sha256:<digest from the build>
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-minimal --profile serving-load --manifest "$M" \
  --execute --confirm-cluster ewallet-dev
helm upgrade --install ewallet-serving infra/helm/ewallet-serving --kube-context "$KUBE_CONTEXT" -n ml-platform \
  --set featureApi.enabled=true --set featureApi.image="$IMAGE" \
  --set featureApi.registryUri=gs://ewallet-data-ai-platform-hg26-data/features/registries/canonical-100gb-v1/online/online-20261031-163506/registry.db
kubectl --context "$KUBE_CONTEXT" -n ml-platform set image deployment/fraud-api fraud-api="$IMAGE"
kubectl --context "$KUBE_CONTEXT" -n ml-platform rollout status deployment/feature-api --timeout=300s
```

**Check:** `/health/ready` is 200 on both. **Rollback:** `helm uninstall ewallet-serving -n ml-platform` and `kubectl rollout undo deployment/fraud-api`. **Cleanup:** `cloud_lab.py sleep`, with its approval.

## P-A2: Real API Locust

| Item | Detail |
|---|---|
| Objective | Rows 32 and 46: real throughput and latency |
| Target | No change; read-only API traffic |
| Capacity | As P-A1 |
| Cost | About $0.86 for 3 hours |
| Limit | 2 targets × 3 profiles × 5 minutes, with rests: about 1 hour |
| Stop if | Failure rate over 5%, or Redis or KServe restarts |

```bash
kubectl --context "$KUBE_CONTEXT" -n ml-platform port-forward svc/feature-api 18081:80 &
kubectl --context "$KUBE_CONTEXT" -n ml-platform port-forward svc/fraud-api 18080:80 &
python -m ops.experiments.fraud_entities build --snapshot "$SNAPSHOT" --limit 500 --out entities.jsonl
python -m ops.experiments.fraud_entities prepare --entities entities.jsonl --feature-api http://127.0.0.1:18081 --out fitted.jsonl
python -m ops.experiments.fraud_entities verify --entities fitted.jsonl --url http://127.0.0.1:18080/v1/fraud/predict --out verified.jsonl
export EWALLET_LOAD_ENTITIES=$PWD/verified.jsonl IMAGE_DIGEST=${IMAGE##*@} EWALLET_LOAD_MODEL_VERSION=1
for p in low medium high; do
  LOAD_HOST=http://127.0.0.1:18081 scripts/testing/run_real_load.sh feature internal $p docs/evidence/edai2/load/gke
  sleep 120
  LOAD_HOST=http://127.0.0.1:18080 scripts/testing/run_real_load.sh inference internal $p docs/evidence/edai2/load/gke
  sleep 120
done
```

A port-forward is one tunnel, and it can drop under load. If it saturates or drops, stop: run Locust from a Job in the cluster instead. Do not compare the two paths.

## P-A3: Serving KEDA (A-01 to A-04)

| Item | Detail |
|---|---|
| Objective | Compare fixed replicas with KEDA 1 to 3 |
| Target | KEDA in `keda`; ScaledObjects `feature-api`, `fraud-api`; headless Services; Prometheus scrape jobs; `allow-keda-operator-to-prometheus` |
| Permissions | Cluster CRDs and RBAC for KEDA; `helm upgrade llm-agent` for the scrape jobs |
| Capacity | Up to 3.4 CPU, 5.5 GiB: **2 nodes** |
| Cost | About $0.46/hour; about $1.39 for 3 hours |
| Risk | KEDA adds cluster-wide CRDs and an external metrics API |

```bash
python3 ops/gcp/cloud_lab.py plan-wake --mode experiment --profile serving-keda --manifest "$M"
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-experiment --profile serving-keda --manifest "$M" --nodes 2 \
  --execute --confirm-cluster ewallet-dev
KUBE_CONTEXT=$KUBE_CONTEXT scripts/llm_agent/keda_install.sh --execute
kubectl --context "$KUBE_CONTEXT" apply -f infra/kubernetes/experiments/keda-prometheus-access.yaml
helm upgrade llm-agent infra/helm/llm-agent-platform --kube-context "$KUBE_CONTEXT" -n default --atomic \
  -f "$RELEASE_VALUES" -f infra/helm/llm-agent-platform/session-a/serving-metrics.yaml
helm upgrade ewallet-serving infra/helm/ewallet-serving --kube-context "$KUBE_CONTEXT" -n ml-platform --reuse-values \
  --set metricsDiscovery.enabled=true
# Fixed arms A-01, A-02
EXP_APPROVED=yes scripts/experiments/serving_keda.sh run fixed feature-api "$EVIDENCE"
EXP_APPROVED=yes scripts/experiments/serving_keda.sh run fixed fraud-api "$EVIDENCE"
# R1: hand the Fraud API's replica count to KEDA first (docs/runbooks/edai2/R1_FRAUD_API_KEDA_HANDOVER.md)
scripts/serving/fraud_api_keda_handover.sh plan "$EVIDENCE/r1"
scripts/serving/fraud_api_keda_handover.sh handover "$EVIDENCE/r1" --execute
# Set thresholds from the fixed-arm throughput, then the KEDA arms A-03, A-04
helm upgrade ewallet-serving infra/helm/ewallet-serving --kube-context "$KUBE_CONTEXT" -n ml-platform --reuse-values \
  --set autoscaling.enabled=true \
  --set autoscaling.targets.feature-api.requestsPerSecondPerReplica="$FEATURE_RPS" \
  --set autoscaling.targets.fraud-api.requestsPerSecondPerReplica="$FRAUD_RPS"
scripts/serving/fraud_api_keda_handover.sh verify "$EVIDENCE/r1"
EXP_APPROVED=yes scripts/experiments/serving_keda.sh run keda feature-api "$EVIDENCE"
EXP_APPROVED=yes scripts/experiments/serving_keda.sh run keda fraud-api "$EVIDENCE"
python -m ops.experiments.serving_compare --fixed "$EVIDENCE/fixed/feature-api/arm-summary.json" \
  --keda "$EVIDENCE/keda/feature-api/arm-summary.json" --out "$EVIDENCE/feature-api-comparison.json"
```

**Thresholds are starting values** (150 and 25 requests per second per replica). Set them to about 70% of the fixed arm's stable throughput, or the scaler will never react or never rest.

**Rollback:** `--set autoscaling.enabled=false`, then `scripts/serving/fraud_api_keda_handover.sh rollback "$EVIDENCE/r1" --execute`, which re-applies the fixed manifest at the current count. Delete ScaledObjects before removing KEDA, or their finalizer holds the namespace.

## P-A4: Agent KEDA (A-05, A-06)

| Item | Detail |
|---|---|
| Objective | Rows 19, 22, 25: agents scale on their own signals |
| Target | ScaledObjects `fraud-coordinator`, `feature-rag-specialist`, `drift-specialist`, replacing their HPAs |
| Prerequisite | Rebuilt agent images exporting `ewallet_agent_inflight_tasks` |
| Capacity | `agent-keda` profile: **3 nodes** |
| Cost | About $0.64/hour; $1.92 for 3 hours |
| Token budget | Typed requests use no LLM. Full LLM tasks need `c7/experiment-limits.yaml` and a separate decision |

```bash
python scripts/experiments/keda_preflight.py --context "$KUBE_CONTEXT" --stage before-scaledobject
EXP_APPROVED=yes scripts/experiments/agent_experiment.sh exp-c5-keda baseline smoke "$EVIDENCE"
EXP_APPROVED=yes scripts/experiments/agent_experiment.sh exp-c5-keda baseline run "$EVIDENCE"
helm upgrade llm-agent infra/helm/llm-agent-platform --kube-context "$KUBE_CONTEXT" -n default --atomic \
  -f "$RELEASE_VALUES" -f infra/helm/llm-agent-platform/g3/keda.yaml
EXP_APPROVED=yes scripts/experiments/agent_experiment.sh exp-c5-keda optimized run "$EVIDENCE"
python -m ops.experiments.cli compare --spec configs/experiments/exp-c5-keda.json --dir "$EVIDENCE"
```

**Rollback:** delete the ScaledObjects first, then upgrade without the overlay, then `keda_preflight.py --stage after-rollback`. A plain Helm rollback reports success and leaves no HPA, as C-B found.

## P-A5: External gateway

| Item | Detail |
|---|---|
| Objective | Rows 37 to 43 |
| Target | Namespace `ewallet-edge`, Helm releases `ewallet-edge-gateway` and `ewallet-edge`; NetworkPolicies in `ml-platform` and `kagent` |
| Needs from the owner | A domain, DNS control, an issuer, and a public exposure decision |
| Capacity | 2.7 CPU with `edge`: **2 nodes** |
| Cost | $0.46/hour, plus about $0.03/hour for a load balancer rule and IP (estimate; confirm before use) |
| Risk | Public exposure of authenticated UIs. Basic Auth is one shared credential |

Stage 1 keeps the Service private and tests through a port-forward. Stage 2, public exposure, is a separate decision.

```bash
kubectl --context "$KUBE_CONTEXT" create namespace ewallet-edge
openssl rand -hex 24 | scripts/edge/create_basic_auth.sh edge ewallet-edge "$KUBE_CONTEXT"   # store it in a password manager first
helm upgrade --install ewallet-edge-gateway istio/gateway --version 1.30.7 --kube-context "$KUBE_CONTEXT" -n ewallet-edge \
  -f infra/helm/ewallet-edge/values-gateway.yaml --wait
helm upgrade --install ewallet-edge infra/helm/ewallet-edge --kube-context "$KUBE_CONTEXT" -n ewallet-edge \
  --set enabled=true --set domain="$DOMAIN" --set routes.feature-api.enabled=true --set routes.fraud-api.enabled=true \
  --set routes.a2a.enabled=true --set routes.kagent-ui.enabled=true --set routes.agent-registry.enabled=true \
  --set tls.certificate.enabled=true --set tls.certificate.issuerName="$ISSUER"
python3 scripts/edge/negative_tests.py --address 127.0.0.1 --https-port 18443 --http-port 18480 --domain "$DOMAIN" \
  --credentials-file "$CREDENTIALS_FILE" --out "$EVIDENCE/results.jsonl"
```

**Stage 2:** `--set service.type=LoadBalancer` on the gateway release, then the owner creates DNS records.

**Verify by hand:** the kagent UI and Registry UI load, chat streams, and API calls work behind the gateway. These have only been simulated.

**Rollback and cleanup:** `helm uninstall ewallet-edge -n ewallet-edge`, `helm uninstall ewallet-edge-gateway -n ewallet-edge`, delete the namespace, remove DNS records.

## Return to sleep

```bash
python3 ops/gcp/cloud_lab.py plan-sleep
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py sleep --scale-pdb-guarded --execute --confirm-cluster ewallet-dev
python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest "$M"
```

Vault, if deployed, seals on every restart. A new recovery manifest should be saved before sleeping, because new workloads are not in the old one.
