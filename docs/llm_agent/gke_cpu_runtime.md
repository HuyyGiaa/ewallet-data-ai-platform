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

## G1 capacity at 3 nodes (requests)

Per node: GKE DaemonSets 0.41 CPU / 0.78 GiB + ztunnel/istio-cni 0.30 / 0.60 +
Calico (readable only after enabling; budgeted 0.25 / 0.30) = 0.96 / 1.68.
Cluster: GKE system 0.33 / 0.54, Kubeflow + MLflow 1.12 / 4.32, istiod
0.10 / 0.25, Calico typha/autoscalers (budgeted 0.40 / 0.50), G1 4.20 / 8.25.
Total 9.03 CPU of 11.76 allocatable (77 %) and ≈19 GiB of ≈39 GiB: **GO** under
the 85 % headroom. Packing: a node has 2.96 CPU free after its DaemonSets, so
each vLLM pod (2 CPU) takes its own node with 0.96 left; the remaining 2.15 CPU
of small pods fit in 0.96 + 0.96 + 2.96. The Calico figures are budgets, not
readings: `g1_operator.sh scale-up` reads the real calico-node rollout and
`gke_preflight --stage g1` re-checks capacity before `deploy`.

## G1 operator runbook (`scripts/llm_agent/g1_operator.sh <stage> <evidence-dir>`)

Every stage checks `KUBE_CONTEXT` is the ewallet-dev context, reads its result
back and exits non-zero otherwise. A stage that changes GKE or Artifact
Registry runs only when listed in `G1_APPROVED_STAGES`; `cleanup` always runs.

```bash
export KUBECONFIG=<scratch>/gke-dns.kubeconfig
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
E=docs/evidence/llm_agent/g1_$(date -u +%Y%m%dT%H%M%SZ); OP=scripts/llm_agent/g1_operator.sh
export DIGESTS=$E/image-digests.json AR_REPO=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform
export G1_APPROVED_STAGES=<the stages the owner approved, comma-separated>
```

| # | Stage | Changes | Verified by (fails closed) |
|---|---|---|---|
| 0 | `status` | nothing | cluster JSON, pool size |
| 1 | `netpol-enable` | 2 cluster updates (pool must be 0) | `networkPolicy.enabled` and addon read back |
| 2 | `image-build` (local, `MODEL_DIR=<staged Qwen3 dir>`) | local image only | SHA256SUMS before build and inside the image |
| 3 | `image-push` | Artifact Registry push | Trivy HIGH/CRITICAL gate first; pushed digest recorded |
| 4 | `scale-up` (`NODES=3`, max 3) | resize 0 -> 3 | NetworkPolicy enabled first; 3 Ready nodes; calico-node rolled out |
| 5 | `istio` | Istio 1.30.7 ambient | chart sha256; CNI bin dir `/home/kubernetes/bin` in render and on the DaemonSet; cni + ztunnel rolled out |
| 6 | `namespace` | the chart's Namespace | `istio.io/dataplane-mode=ambient` read back |
| 7 | `secret` | Secret `vllm-api-key` | fresh 64-hex token via a 0600 file, never printed or on argv; refuses to overwrite |
| 8 | `deploy` | Helm release (G1 overlay, no kagent) | `gke_preflight --stage g1` GO; `--atomic --wait`; pod image IDs |
| 9 | `netpol-verify` | throwaway curl pods | matrix below; any mismatch fails |
| 10 | `smoke-direct` | nothing | tool call 200, stream 200, wrong key 401 |
| 11 | `smoke-gateway` | nothing | same through agentgateway (`ewallet-fraud-llm`) |
| 12 | `failover` | primary 0 -> back to 1 | request served by `fallback` |
| 13 | `evidence` | nothing | nodes, pods, image IDs, helm history, top, redacted vLLM log |
| 14 | `cleanup [--uninstall]` | resize to 0 (optional uninstall) | pool size 0 read back |

`netpol-verify` matrix (pinned `curlimages/curl` by digest, unprivileged, no SA token):

| Row | Expect |
|---|---|
| unlabelled pod -> Internet (huggingface.co) | deny |
| unlabelled pod -> vllm-primary:8000 | deny |
| unlabelled pod -> agentgateway:4000 | deny |
| pod in another namespace -> vllm-primary:8000 | deny |
| pod with the agentgateway label but another ServiceAccount -> vLLM | deny (NetworkPolicy allows, Istio AuthorizationPolicy refuses) |
| vLLM pod -> Internet | deny |
| kubelet probes (vLLM Ready) | allow |
| DNS (a deny must be a timeout/refusal, never curl exit 6) | allow |
| agentgateway -> vLLM | allow (proven by `smoke-gateway`) |

Offline, `tests/llm_agent/test_gke_cpu_profile.py::G1ReachabilityTests` evaluates
the rendered policies with Kubernetes semantics: among the G1 workloads only
agentgateway -> vLLM on 8000 and 15008 is allowed; 169.254.7.127 reaches only
probe ports; DNS (kube-dns and 169.254.20.10) is the only other egress; the
metadata server is closed unless `networkPolicy.metadataAccess` names a workload.

## Cost exposure

3 × e2-standard-4 ≈ 0.52 USD/h while nodes exist (nothing else in G1 scales
with time except the pre-existing cluster fee). A 3-hour G1 session ≈ 1.56 USD;
a forgotten pool ≈ 12.4 USD/day. Artifact Registry: the model image (≈7.5 GB)
≈ 0.75 USD/month until deleted. No budget is enforced by the platform: the
operator runs `cleanup` at the end of every session and after any failed stage.

## After G1 (unchanged order, each with its own approval and preflight)

3. agentgateway benchmark beyond the smoke latencies; 4. llm-d: GAIE v1.6.2
CRDs + router v0.9.0 (`router-values-local.yaml` CPU budget); 5. kagent +
Substrate + native A2A (ModelConfig readback moves here); 6. MCP identity gate;
7. business E2E (Feast Online/Redis, KServe, RAG, drift).

## Image vulnerability gate (vllm-cpu-model)

Evidence: `docs/evidence/llm_agent/gke_cpu/trivy_vllm_cpu_20261031.json`
(Trivy 0.75.0 with the `scan_and_push.sh` flags; local builds, no push).

| Image | HIGH/CRITICAL fixed vulns | Secret findings |
|---|---|---|
| upstream `vllm-openai-cpu` v0.32.0 @827ef381 | 10 (2 CRITICAL) in 8 packages | 11 |
| `remediated` stage, round 1 | 6 (pip copies outside the venv) | 11 |
| `remediated` stage, final | **0** | 11 |
| final + `trivy-secret-allow.yaml` | 0 | **0** (exit 0) |

Remediation (`infra/docker/vllm-cpu-model/remediate.py`, every download by sha256):
libbrotli1 -> 1.0.9-2ubuntu0.1; venv certifi -> 2026.10.18 (`--require-hashes`);
setuptools stays 78.1.1 (vLLM 0.32.0 pins `setuptools==78.1.1`) and only its
vendored jaraco.context 5.3.0 / wheel 0.45.1 are replaced by 6.1.0 / 0.46.3 from
the setuptools 82.0.1 wheel; pip is removed everywhere it was found (venv, the
uv-managed base interpreter, the ensurepip wheel, uv's cache), which removes
its vendored certifi/urllib3/msgpack/pkg_resources. Import checks in the build:
certifi, setuptools(.command.bdist_wheel), pkg_resources, torch.utils.cpp_extension, vllm.
The vendored-package swap is the one non-standard step; the G1 startup probe
(a real tool-shaped completion) is its runtime proof.

**Owner decision (secret scanner).** The 11 secret findings are one false
positive: rule `gcp-service-account` matches the anthropic SDK docstring
``sending `{"type": "service_account"}` `` (no `private_key`; vLLM depends on the
SDK). `trivy-secret-allow.yaml` lists exactly those 7 files by anchored path.
It is **not** wired into `scan_and_push.sh`: approving it means passing
`--secret-config` for this image only. Without that approval `image-push`
fails closed and G1 stays NO-GO. Alternatives: an upstream vLLM release whose
SDK no longer matches (re-pin and re-validate), or removing the SDK (breaks
vLLM's Anthropic-compatible endpoint imports; not recommended).
