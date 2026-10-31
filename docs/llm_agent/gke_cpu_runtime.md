# GKE CPU runtime (owner decision 2026-10-31: CPU vLLM on GKE, no GPU)

Target: cluster `ewallet-dev` (project `ewallet-data-ai-platform-hg26`, zone
`asia-southeast1-b`, GKE 1.36.4), context
`gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev`.
The architecture is unchanged: ModelConfig -> agentgateway -> (llm-d) -> vLLM
CPU (`vllm-openai-cpu:v0.32.0` by digest, Qwen3-0.6B @ `c1899de2…`).

## Cluster facts (read 2026-10-31, read-only)

| Fact | Value | Consequence |
|---|---|---|
| Node pool | `cpu-pool`, e2-standard-4, pd-balanced 50 GB, no autoscaling, **0 nodes** | Every stage needs a paid resize (owner approval) |
| Pending workloads | 15 Kubeflow deployments + MLflow + Postgres (≈1.1 CPU / 4.3 GiB requests), no ScheduledWorkflow | They start as soon as nodes exist; included in the capacity plan |
| NetworkPolicy | legacy dataplane, network policy **disabled** | The chart's default-deny NetworkPolicies are not enforced on GKE; only Istio ambient L4 policy would be |
| Control plane | authorized networks = one /32 that is no longer this host's IP; DNS endpoint with external traffic allowed | Use the IAM-authenticated DNS endpoint with a separate kubeconfig; do not edit authorized networks |
| Mesh / CRDs | no Istio, Gateway API, kagent, Substrate, GAIE or KEDA CRDs | Installed per stage below |
| Quota (asia-southeast1) | E2_CPUS 24, CPUS 100, CPUS_ALL_REGIONS 32, all 0 used | 3 × e2-standard-4 = 12 E2 CPUs fits |
| CPU ISA | E2 guarantees AVX2 only | The pinned CPU image already served this model on an AVX2-only host |

Access without changing the default context:

```bash
export KUBECONFIG=<scratch>/gke-dns.kubeconfig
gcloud container clusters get-credentials ewallet-dev --zone asia-southeast1-b --dns-endpoint
CTX=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
```

## Cost gate (Cloud Billing Catalog, USD, on-demand, Singapore, read 2026-10-31)

| Item | Unit price | Per node-hour |
|---|---|---|
| E2 core | 0.026909 / vCPU-h | 4 × = 0.107636 |
| E2 RAM | 0.003606 / GiB-h | 16 × = 0.057696 |
| Balanced PD | 0.110 / GiB-month | 50 GiB / 730 h = 0.007534 |
| **e2-standard-4 node** | | **≈ 0.1729** |

| Stage | Nodes | Hours (proposed max) | Cost |
|---|---|---|---|
| G0 Istio + G1 vLLM/agentgateway + benchmark | 2 | 3 | ≈ 1.04 |
| G2 llm-d + G3 kagent/agents/MCP/A2A + business E2E | 3 | 6 | ≈ 3.11 |
| **Total** | max 3 | 9 | **≈ 4.15** (proposed ceiling 10 USD) |

Not included: the zonal cluster management fee, which is charged today with 0
nodes and does not change. Image and model downloads are ingress (free).
Budgets could not be read (Billing Budgets API disabled, no billing-account
permission). No autoscaler: nodes bill until the pool is resized to 0, which is
the cleanup after every session:

```bash
gcloud container clusters resize ewallet-dev --node-pool cpu-pool --num-nodes 0 --zone asia-southeast1-b
```

## Stages (each one: owner approval, then `gke_preflight` GO, then apply)

```bash
python3 -m ops.llm_agent.gke_preflight --stage <g0|g1|g2|g3> --context "$CTX" --kubeconfig "$KUBECONFIG" \
    --json-output docs/evidence/llm_agent/gke_cpu/preflight_<stage>.json
```

Approvals needed, in order:

1. **Resize** `cpu-pool` 0 -> 2 (G0-G1), later 2 -> 3 (G2-G3), ceiling above.
2. **NetworkPolicy enforcement**: `gcloud container clusters update ewallet-dev --update-addons=NetworkPolicy=ENABLED`
   then `--enable-network-policy` (cheapest while the pool has 0 nodes; it recreates nodes otherwise), or an
   explicit owner waiver recorded with `--waive-unenforced-networkpolicy`.
3. **G0** Istio ambient (cluster-scoped): `KUBE_CONTEXT=$CTX scripts/llm_agent/istio_ambient_gke.sh plan <ev>`, review,
   then `apply`. Read back `cni-bin-dir.txt` (GKE autodetection, expected `/home/kubernetes/bin`).
4. **G1** kagent CRDs 0.10.3 by digest (`infra/kubernetes/kagent-pilot/versions.env`) for ModelConfig; Secret
   `ewallet-agents/vllm-api-key` created from a freshly generated token that is never printed; then
   `KUBE_CONTEXT=$CTX scripts/llm_agent/deploy_atomic.sh gke-cpu <digests.json> <ev>`.
   Acceptance: `scripts/llm_agent/smoke_tool_call.py` through `kubectl port-forward` to vLLM (`ewallet-fraud-llm-v1`) and to
   agentgateway (`ewallet-fraud-llm`), non-stream and `--stream`, plus a wrong-key request (401) and one
   fallback test (scale primary to 0). Benchmark: `ai_platform/llm_agent/benchmark.py`.
5. **G2** GAIE v1.6.2 CRDs and the llm-d router v0.9.0 (infra/helm/llm-d, `router-values.yaml` +
   `router-values-local.yaml` CPU budget), then `--set llmd.enabled=true`.
6. **G3** Substrate 0.0.9 + kagent 0.10.3 (`kagent-values.yaml` + `kagent-values-local.yaml`), approved
   `agents.snapshotsLocation`, then agents and the two policy gateways. MCP stays DenyAll until the identity
   gate (docs/llm_agent/mcp_gateway.md) passes.

The CPU budgets are the local ones (ModelConfig 200 s > agentgateway 180/170/160/160 s > llm-d route 155 s;
A2A 660 s < kagent client 700 s), checked by `tests/llm_agent/test_r2_r3_budgets.py` for `gke-cpu`.
