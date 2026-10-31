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
| NetworkPolicy | legacy dataplane, network policy **disabled** | The chart's default-deny NetworkPolicies are not enforced on GKE; plan below |
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

## NetworkPolicy enforcement (P0, not enabled yet)

Current mode (read 2026-10-31): legacy dataplane (kube-proxy, no Dataplane V2),
NetworkPolicy addon disabled, VPC-native, Workload Identity (GKE_METADATA),
NodeLocal DNSCache on. Dataplane V2 cannot be switched on for an existing
cluster, so the supported in-place path is GKE network policy enforcement
(Calico). Both commands recreate nodes; with `cpu-pool` at 0 nodes they recreate
nothing. A waiver is not equivalent to enforcement and is not part of this plan.

```bash
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --update-addons=NetworkPolicy=ENABLED
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --enable-network-policy
# rollback (same cost profile at 0 nodes)
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --no-enable-network-policy
gcloud container clusters update ewallet-dev --zone asia-southeast1-b --update-addons=NetworkPolicy=DISABLED
```

Chart compatibility with enforcement under Istio ambient 1.30.7 (fixed in source):

| Need | Rendered rule |
|---|---|
| ztunnel HBONE to the destination pod | every enforced flow opens its port **and 15008**, ingress and egress |
| kubelet probes (SNAT 169.254.7.127 in ambient) | `allow-ambient-probes-<workload>`: that /32, probe port only, only for workloads in the release |
| DNS with NodeLocal DNSCache | kube-dns pods **and** 169.254.20.10/32, port 53 |
| model files | no Internet egress at all: the revision is baked into `infra/docker/vllm-cpu-model` (`modelSource.mode: image`) |

Unaffected: `kubeflow`, `ml-platform` and system namespaces have no
NetworkPolicy, so they stay allow-all. Later stages still need explicit rules
for the kube-apiserver (EPP, kagent controller), the metadata server for
Workload Identity (G3 snapshots) and cross-namespace kagent A2A; they are listed
as unresolved flows and stay denied until read back.

Verification after G1 (a throwaway unprivileged curl pod without app labels):

- positive: DNS from `ewallet-agents`; vLLM pods Ready (probes pass); agentgateway -> vLLM tool-call HTTP 200; Kubeflow/MLflow pods Ready;
- negative: test pod -> `vllm:8000` and `agentgateway:4000` time out; a pod in another namespace -> `vllm:8000` times out;
  vLLM and agentgateway -> `https://huggingface.co` time out; agentgateway -> vLLM on another port times out;
  a pod carrying the agentgateway label but another ServiceAccount -> vLLM is refused by the Istio AuthorizationPolicy.
- readback: `describe` shows `networkPolicy.enabled: true` (CALICO); calico-node Ready on every node.
  Istio ambient with GKE Calico enforcement is a runtime check, not assumed.

## Capacity (requests, GiB)

| Item | CPU | Memory | Source |
|---|---|---|---|
| per node: GKE DaemonSets matching e2-standard-4 | 0.41 | 0.78 | DaemonSet specs on the cluster |
| per node: ztunnel + istio-cni | 0.30 | 0.60 | rendered 1.30.7 charts |
| per node: Calico | unknown (est. 0.1-0.25) | unknown | readable only after enabling |
| cluster: GKE system deployments | 0.33 | 0.54 | Pending pods |
| cluster: Kubeflow + MLflow | 1.12 | 4.32 | Pending pods |
| cluster: istiod | 0.10 | 0.25 | values-gke.yaml |
| G1: vLLM primary + fallback, agentgateway ×2 | 4.20 | 8.25 | rendered values-gke-cpu.yaml |

e2-standard-4 allocatable ≈ 3.92 CPU / ≈13 GiB (exact value after a node exists).
2 nodes: 7.2-7.7 CPU of 7.84 (91-98 %): **NO-GO** under the 85 % headroom.
3 nodes: ≈8.6 CPU of 11.76 (73 %): GO on capacity. G2 adds the EPP; G3 adds
≈3.7 CPU / 6.7 GiB (Substrate, WorkerPools, kagent, gateways, MCP): 4 nodes ≈87 %,
so 5 nodes or an owner decision on Kubeflow/MLflow; re-run the preflight.
Quota: E2_CPUS 24 and CPUS_ALL_REGIONS 32 also cover Dataproc; do not run
Dataproc batches while 5 nodes are up.

## Cost gate (Cloud Billing Catalog, USD, on-demand, Singapore, read 2026-10-31)

| Item | Unit price | Per node-hour |
|---|---|---|
| E2 core | 0.026909 / vCPU-h | 4 × = 0.107636 |
| E2 RAM | 0.003606 / GiB-h | 16 × = 0.057696 |
| Balanced PD | 0.110 / GiB-month | 50 GiB / 730 h = 0.007534 |
| **e2-standard-4 node** | | **≈ 0.1729** |

| Stage | Nodes | Hours (max) | Cost |
|---|---|---|---|
| G0-G2: NetworkPolicy, Istio, vLLM, agentgateway, llm-d, benchmark | 3 | 4 | ≈ 2.07 |
| G3 + business E2E | 4-5 | 6 | ≈ 4.15-5.19 |
| **Total** | | 10 | **≈ 6.2-7.3** |

Excluded: the zonal cluster management fee (charged today at 0 nodes, about
0.10/h, unchanged), Cloud Logging/Monitoring above free tiers, Artifact Registry
storage for the ≈7.5 GB model image (≈0.10/GB-month), taxes. The 10 USD ceiling
is **not enforced**: budgets are unreadable (API disabled, no billing-account
permission); enforcement is a time-box plus the resize to 0:

```bash
gcloud container clusters resize ewallet-dev --node-pool cpu-pool --num-nodes 0 --zone asia-southeast1-b
```

## Rollout order (each step: owner approval, `gke_preflight` GO, apply, evidence)

```bash
python3 -m ops.llm_agent.gke_preflight --stage <g0|g1|g2|g3> --context "$CTX" --kubeconfig "$KUBECONFIG" \
    [--set vllm.image=<vllm-cpu-model digest ref>] --json-output docs/evidence/llm_agent/gke_cpu/preflight_<stage>.json
```

1. **Network and resources**: NetworkPolicy enforcement (above); resize `cpu-pool` 0 -> 3; G0 Istio ambient
   (`KUBE_CONTEXT=$CTX scripts/llm_agent/istio_ambient_gke.sh plan|apply <ev>`, read back `cni-bin-dir.txt`).
2. **CPU vLLM direct**: `scripts/llm_agent/build_vllm_model_image.sh <staged model dir> <AR repo> <sha> <digests.json>`,
   `scan_and_push.sh` (Trivy HIGH/CRITICAL gate on the upstream vLLM base: expect findings, owner decision),
   kagent CRDs 0.10.3 by digest, Secret `vllm-api-key` from a generated token (never printed),
   `KUBE_CONTEXT=$CTX scripts/llm_agent/deploy_atomic.sh gke-cpu <digests.json> <ev>`;
   `smoke_tool_call.py` to vLLM via port-forward (non-stream, `--stream`, wrong key -> 401).
3. **agentgateway**: the same smoke against `ewallet-fraud-llm`, fallback test (scale primary to 0), benchmark.
4. **llm-d**: GAIE v1.6.2 CRDs + router v0.9.0 (`router-values-local.yaml` CPU budget), `--set llmd.enabled=true`.
5. **kagent + native A2A**: Substrate 0.0.9 + kagent 0.10.3 (`kagent-values-local.yaml`), approved
   `agents.snapshotsLocation`, agents enabled.
6. **MCP identity**: MCP stays DenyAll until docs/llm_agent/mcp_gateway.md's identity gate passes.
7. **Business E2E**: Feast Online/Redis, KServe, RAG publication, drift baseline.

The CPU budgets are the local ones (ModelConfig 200 s > agentgateway 180/170/160/160 s > llm-d route 155 s;
A2A 660 s < kagent client 700 s), checked by `tests/llm_agent/test_r2_r3_budgets.py` for `gke-cpu`.
