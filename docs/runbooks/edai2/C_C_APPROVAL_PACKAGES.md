# C-C cloud approval packages

These packages list what the owner must approve before each C-C cloud step. **No package is approved yet, and nothing here has run on GKE.** The cluster is asleep at zero nodes; waking it is a separate approval in the [Cloud Lab runbook](CLOUD_LAB_RUNBOOK.md).

`SOURCE_SHA` is the commit the images are built from. Image digests are unknown until those images are built, so each package records them at build time.

Before any package:

1. Finish Git integration, or decide which branch the image is built from.
2. Top up or upgrade billing: the sleeping project alone uses about $0.107 an hour.
3. Wake only the capacity the package lists.

## P7: Vault

| Item | Detail |
|---|---|
| Objective | Deploy Vault and move one Fraud API secret to it (row 54) |
| Target | Namespace `ewallet-vault`, Helm release `vault`, StatefulSet `vault`; Deployment `ml-platform/fraud-api` |
| Source | `SOURCE_SHA`; chart `vault-0.34.1` (sha256 `df4c37fa...64c4`) |
| Images | `hashicorp/vault:2.0.4@sha256:5be49781...e1a2`; new Fraud API image, digest recorded at build |
| Manifests | `infra/kubernetes/vault/*`, `infra/kubernetes/fraud-serving/fraud-api-vault.patch.yaml` |
| Permissions | Create namespace, Issuers and Certificates; ClusterRoleBinding `system:auth-delegator` for Vault (cluster-scoped, from the chart); patch `fraud-api` |
| Secrets | Keys go to one mode-0600 file outside the repository. The Redis value is copied from the existing Secret through `cli.py put --stdin`, never as an argument |
| Dependencies | cert-manager, CSI disks, Fraud API image containing `vault.py` |
| Requirements | 100m CPU, 256 MiB; limit 500m, 512 MiB; 6 GiB of disk |
| Cost | About $0.66/month for disks; no new nodes if run with the `kserve-small` profile. One node is about $0.285/hour in total |
| Risk to production | Fraud API restarts. In Vault mode it fails closed if Vault is sealed, so a cluster wake needs an unseal first |

**Preflight**

```bash
kubectl get crd certificates.cert-manager.io
kubectl get pods -n cert-manager
python3 ops/gcp/cloud_lab.py status
kubectl -n ml-platform get deployment fraud-api -o yaml > "$EVIDENCE/fraud-api-before.yaml"
```

**Deploy and verify**

```bash
kubectl apply -f infra/kubernetes/vault/namespace.yaml -f infra/kubernetes/vault/certificate.yaml
kubectl -n ewallet-vault wait --for=condition=Ready certificate/vault-server-tls --timeout=120s
helm install vault vault-0.34.1.tgz -n ewallet-vault -f infra/kubernetes/vault/values-gke.yaml
kubectl apply -f infra/kubernetes/vault/network-policy.yaml
kubectl -n ewallet-vault port-forward pod/vault-0 8200:8200 &
kubectl -n ewallet-vault get secret vault-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > "$KEYDIR/ca.crt"
V="python3 -m ops.vault.cli --ca-file $KEYDIR/ca.crt"
$V init --keys-out "$KEYDIR/vault-keys.json"
$V unseal --keys-file "$KEYDIR/vault-keys.json"
$V configure --keys-file "$KEYDIR/vault-keys.json"
kubectl -n ml-platform get secret feast-online-redis -o jsonpath='{.data.connection_string}' | base64 -d \
  | $V put --keys-file "$KEYDIR/vault-keys.json" --path fraud-api/feast-redis --key connection_string --stdin
kubectl -n ml-platform create configmap vault-ca --from-file=ca.crt="$KEYDIR/ca.crt"
kubectl -n ml-platform patch deployment fraud-api --type=strategic --patch-file infra/kubernetes/fraud-serving/fraud-api-vault.patch.yaml
kubectl -n ml-platform rollout status deployment/fraud-api --timeout=300s
kubectl -n ml-platform logs deployment/fraud-api | grep vault_secret_loaded
```

`KEYDIR` must be outside the repository. Back the key file up offline. Run `$V retire-root` after verification.

**Rollback:** `kubectl -n ml-platform rollout undo deployment/fraud-api`. The base `secretKeyRef` returns, and Vault is no longer on the request path.

**Cleanup:** `helm uninstall vault -n ewallet-vault`. Deleting the namespace and PVCs destroys all Vault data, so that needs a separate decision.

**Evidence:** `docs/evidence/edai2/vault/gke_<timestamp>/`. Show health, auth config, a loaded version in the logs, a rejected pod and a successful prediction. Never capture a secret value.

## P8: ML A/B

| Item | Detail |
|---|---|
| Objective | Serve v1 and v2 side by side and route real predictions |
| Target | InferenceService `fraud-serving/fraud-detector-v2`; ConfigMap `ml-platform/fraud-api-ab`; Deployment `fraud-api` |
| Source | `SOURCE_SHA` |
| Images | Candidate predictor and Fraud API, digests recorded at build; v1 stays `sha256:cba90c22...b896` |
| Manifests | `infra/kubernetes/fraud-serving/*candidate*`, `fraud-api-ab.patch.yaml`, `configs/experiments/ml-ab/*.json` |
| Permissions | Create the InferenceService and a NetworkPolicy; patch `fraud-api` |
| Secrets | None new |
| Dependencies | **Register v2 first.** Run the KFP pipeline with `--config /workspace/ai_platform/ml/config/c7_candidate_v2.yaml`, then bake with `ops/mlops/bake_serving_image.py`. The pipeline image must contain this config |
| Requirements | 250m CPU, 768 MiB more. Profile `kserve-small` plus v2 fits 1 node; check with `plan-wake` |
| Cost | About $0.29/hour with 1 node; about $0.86 for 3 hours, plus the KFP run on 2 nodes |
| Risk to production | Fraud API restarts per split change. Fallback can add 1 s to a request |

**Preflight:** v2 exists in MLflow with its run, dataset and config; `ml_ab.sh plan` renders; `kubectl get isvc -n fraud-serving` shows v1 Ready.

**Run**

```bash
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev EXP_APPROVED=yes
export CANDIDATE_IMAGE=<registry>/fraud-detector-predictor@sha256:<digest>
scripts/experiments/ml_ab.sh deploy
python -m ops.experiments.fraud_entities verify --entities entities.jsonl --url "$API/v1/fraud/predict" --out verified.jsonl
for split in split-90-10 split-50-50; do
  scripts/experiments/ml_ab.sh split "$split"
  scripts/experiments/ml_ab.sh traffic verified.jsonl "$split" "$EVIDENCE"
done
python -m ops.experiments.ml_ab_evaluate --dataset test_rows.jsonl --arm model-a=$A=1 --arm model-b=$B=2 --out "$EVIDENCE/quality"
```

Reach both predictors for the quality step through port-forwards, with an owner-approved temporary rule: the NetworkPolicy admits only the Fraud API.

**Rollback:** `ml_ab.sh split rollback-100-0`. **Cleanup:** `ml_ab.sh remove`.

**Evidence:** `docs/evidence/edai2/ml_ab/gke_<timestamp>/`, with Grafana panels for `fraud_ab_*`, the three splits, and the fallback test.

## P9: Agent A/B

| Item | Detail |
|---|---|
| Objective | Compare two Coordinator model configurations on real investigations (row 52) |
| Target | Agent `ewallet-agents/fraud-coordinator`; ConfigMap `agentgateway-llm-config` |
| Source | `SOURCE_SHA` |
| Images | New Coordinator image, digest recorded at build; the current one lacks `agent_ab.py` |
| Manifests | `infra/helm/llm-agent-platform/c7/agent-ab-cpu.yaml`, `c7/experiment-limits.yaml` |
| Permissions | `helm upgrade llm-agent`, then restart `agentgateway` because a ConfigMap change does not roll it |
| Secrets | None new; both variants use the same gateway key |
| Dependencies | NL intake enabled, as deployed |
| Requirements | No new pods. Full agent path needs the `agent-keda` profile: 3 nodes |
| Cost | About $0.64/hour; about $1.92 for 3 hours |
| Risk to production | Coordinator restarts. The session token limit is raised and must be removed afterwards |

**Preflight:** read back the live Helm values; confirm `release_state.py drift` is zero; confirm `tokenRate` before the change.

**Run**

```bash
helm upgrade llm-agent infra/helm/llm-agent-platform -n default --atomic -f "$RELEASE_VALUES" \
  -f infra/helm/llm-agent-platform/c7/agent-ab-cpu.yaml -f infra/helm/llm-agent-platform/c7/experiment-limits.yaml
kubectl -n ewallet-agents rollout restart deployment/agentgateway
kubectl -n ewallet-agents port-forward svc/agentgateway 13320:3000 &
EWALLET_LLM_API_KEY=<from the Secret, not typed into a shell history> python -m ops.experiments.agent_ab_eval \
  --config configs/experiments/agent-ab/sampling.json --gateway-url http://127.0.0.1:13320 --out "$EVIDENCE/suite"
```

Then send real natural-language investigations through kagent Chat or `ops/experiments/agent_load.py`, and read `agent_experiment` in the result artifact to prove both variants ran with MCP tools.

**Rollback:** set variant weights to 100/0, or `helm rollback llm-agent <revision>`. **Cleanup:** remove both overlays with a new upgrade, then confirm `tokenRate` is back to 24,000.

**Evidence:** `docs/evidence/edai2/agents/agent_ab_gke_<timestamp>/`, including Grafana `ewallet_agent_ab_*`, a Tempo trace with `ewallet.ab.variant`, and the MCP tool calls.

## P10: LLM and GPU A/B

Four separate approvals. One never implies the next.

| Boundary | What it allows | Gate | Cost |
|---|---|---|---|
| **P10.1 Quota** | Owner requests `GPUS_ALL_REGIONS=1`, likely after a billing upgrade | Owner, in the console | None |
| **P10.2 GPU node** | `llm_ab.sh create-gpu-pool`: one `g2-standard-4` + L4 | `GPU_POOL_APPROVED=yes` | $0.52/hour Spot, $0.87 on demand |
| **P10.3 Model deployment** | Build and push the image; deploy V3B routes | Owner runs the commands below | Image storage about $1.40/month for 14 GiB (estimate) |
| **P10.4 Experiment** | `run-v3a`, `run-v3b` | `EXPERIMENT_APPROVED=yes` | About $1.63 for 3 hours Spot, $3.26 for 6 hours |

| Item | Detail |
|---|---|
| Objective | Compare Qwen3-0.6B and Qwen3-1.7B (row 53) |
| Target | `gpu-pool`; namespace `ewallet-bench` for V3A; `vllm-candidate` and gateway routes for V3B |
| Models | Qwen3-0.6B `c1899de2...`, Qwen3-1.7B `70d244cc...` |
| Image | Base `vllm/vllm-openai:v0.32.0@sha256:befccf0e...5b34`, built by `build_vllm_gpu_image.sh` |
| Manifests | `render_bench.py exp-v3a`, `c7/llm-ab-cpu-gpu.yaml`, `c7/experiment-limits.yaml` |
| Permissions | Create and delete one node pool; create and delete one namespace; `helm upgrade` for V3B |
| Requirements | 2.5 CPU, 10 GiB, 1 GPU. V3B also needs a CPU node for the 0.6B arm |
| Risk to production | V3A: none, it runs in its own namespace. V3B: changes the production gateway config; the production `ewallet-fraud-llm` route stays the same |

**Preflight:** `scripts/experiments/llm_ab.sh preflight` must pass; today it stops on `GPUS_ALL_REGIONS=0`.

**Node checks after P10.2:** the base image needs CUDA 13. Read the driver with `nvidia-smi` on the node and stop if vLLM cannot start. Spot capacity can also fail or be reclaimed; a reclaimed run counts as failed and is not retried.

**V3A:** `llm_ab.sh render`, then `run-v3a`. It alternates arms, restarts vLLM per run, and records GPU memory and utilization. The two arms differ on one line, the model path.

**V3B deploy (P10.3)**

```bash
helm upgrade llm-agent infra/helm/llm-agent-platform -n default --atomic --timeout 30m -f "$RELEASE_VALUES" \
  -f infra/helm/llm-agent-platform/c7/llm-ab-cpu-gpu.yaml -f infra/helm/llm-agent-platform/c7/experiment-limits.yaml \
  --set vllmCandidate.image="$VLLM_GPU_IMAGE"
kubectl -n ewallet-agents rollout restart deployment/agentgateway
```

**Rollback:** `helm rollback llm-agent <revision>`, restart `agentgateway`. **Cleanup:** `llm_ab.sh cleanup` deletes the namespace and the pool and reads back zero GPU nodes. A forgotten Spot node costs about $12.50 a day.

**Evidence:** `docs/evidence/edai2/llm_ab/gke_<timestamp>/`. The screenshot list comes from `python -m ops.experiments.cli checklist --spec configs/experiments/exp-v3a-model-l4.json`.
