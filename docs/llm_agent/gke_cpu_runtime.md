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

## G1 operator runbook (`scripts/llm_agent/g1_operator.sh`)

Guarantees (tests: `tests/llm_agent/test_gke_cpu_profile.py::OperatorTests`):
- `KUBE_CONTEXT` must be the ewallet-dev context **and** its kubeconfig server
  must be one of the cluster's endpoints; every gcloud call names the project.
- `G1_APPROVED_STAGES` lists mutating stages by exact name; an unknown name
  fails. Mutating stages: netpol-enable, image-push, scale-up, istio,
  namespace, secret, deploy, netpol-verify (test pods), failover.
- A mutating stage runs once per evidence dir; a retry needs `G1_RERUN=<stage>`.
  Nothing retries a cloud mutation by itself.
- `scale-up` (max 3) needs NetworkPolicy enforcement and reads back exactly
  that many pool instances and Ready nodes.
- `failover` refuses without a Ready fallback and passes only if the primary is
  gone, the response carries `x-ewallet-backend: fallback` and the fallback pod's
  `vllm:request_success_total` increased.
- `session` runs the node-bound stages in a time box and scales the pool to 0
  on success, failure, Ctrl-C, SIGTERM, SIGHUP and at the time box; each stage
  runs in its own process group, which is killed on interruption.
- `cleanup` needs no approval and no working kubeconfig; it reads back 0
  instances.

### One approval-ready execution

```bash
cd ~/Develop/ewallet-data-ai-platform            # canonical checkout at the approved HEAD
export KUBECONFIG=<scratch>/gke-dns.kubeconfig    # IAM DNS endpoint, default context untouched
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export E=docs/evidence/llm_agent/g1_$(date -u +%Y%m%dT%H%M%SZ) OP=scripts/llm_agent/g1_operator.sh
export AR_REPO=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform
export DIGESTS=$E/image-digests.json UV=$(command -v uv)
export MODEL_DIR=~/Develop/ewallet-llm-agent-local/models/Qwen3-0.6B/c1899de289a04d12100db370d81485cdf75e47ca
export G1_APPROVED_STAGES=netpol-enable,image-push,scale-up,istio,namespace,secret,deploy,netpol-verify,failover
export G1_SESSION_MINUTES=180

$OP status $E                # read-only: pool 0, NetworkPolicy off
$OP netpol-enable $E         # 1. Calico enforcement, pool must be 0, read back
$OP image-build $E           # 2. uv contract + checksums + local build (no push)
$OP image-push $E            #    vuln + secret gate on the final image, then push, digest recorded
$OP session $E               # 3-8. scale-up(3) istio namespace secret deploy netpol-verify
                             #      smoke-direct smoke-gateway failover benchmark evidence -> cleanup (0 nodes)
$OP status $E                # 9. read back pool 0 / instances 0
```

| Step | Expected time | Cost |
|---|---|---|
| netpol-enable | 10-20 min (two cluster updates) | none beyond the existing cluster fee |
| image-build | ≈5 min (measured locally) | none |
| image-push | 2.92 GB upload, bandwidth-bound (≈10-30 min) | Artifact Registry ≈0.10 USD/GB-month ≈ 0.30 USD/month |
| session | expected 75-110 min, hard stop at 180 | 3 × 0.1729 USD/h: ≈0.65-0.95 USD, at most ≈1.56 USD |

### Recovery after host or session loss

The session's traps cannot run if the host dies. From any authenticated
machine (or Cloud Shell), with no kubeconfig needed:

```bash
gcloud container clusters resize ewallet-dev --node-pool cpu-pool --num-nodes 0 \
  --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26 --quiet
gcloud compute instance-groups managed list --project ewallet-data-ai-platform-hg26 \
  --filter='name~^gke-ewallet-dev-cpu-pool-' --format='value(name,targetSize)'   # expect 0
```

Exposure until then: ≈0.52 USD/h (≈12.4 USD/day). A server-side dead-man
switch (e.g. a Cloud Scheduler job that resizes to 0) would need new IAM and
is not part of this approval. The release, Istio and NetworkPolicy stay
installed at 0 nodes and cost nothing extra; `cleanup --uninstall` also
removes the release.

## After G1 (unchanged order, each with its own approval and preflight)

3. agentgateway benchmark beyond the smoke latencies; 4. llm-d: GAIE v1.6.2
CRDs + router v0.9.0 (`router-values-local.yaml` CPU budget); 5. kagent +
Substrate + native A2A (ModelConfig readback moves here); 6. MCP identity gate;
7. business E2E (Feast Online/Redis, KServe, RAG, drift).

## Image vulnerability and secret gate (vllm-cpu-model)

Evidence: `docs/evidence/llm_agent/gke_cpu/trivy_vllm_cpu_20261031.json`
(Trivy 0.75.0 by digest, `scan_and_push.sh` flags `--scanners vuln,secret
--exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed`; local builds only).

| Image | HIGH/CRITICAL vulns | Secrets |
|---|---|---|
| upstream `vllm-openai-cpu` v0.32.0 @827ef381 | 10 (2 CRITICAL), 8 packages | 11 |
| `remediated` stage | 0 | 11 (one docstring, 3 phrasings) |
| **final image with model**, gate with `trivy-secret.yaml` | **0** | **0** (exit 0) |

Python inputs come from the `infra/docker/vllm-cpu-model` uv project
(`pyproject.toml` + `uv.lock`; `uv lock --check`, `uv sync --locked`,
`uv run --locked` in the build script; pinned uv 0.13.5 runs `uv lock --check`
and `uv export --locked` inside the build and installs with `--require-hashes
--no-deps`). Recorded deviations (tests/llm_agent/test_vllm_model_image.py):
the upstream vLLM venv is not re-resolved; libbrotli1 -> 1.0.9-2ubuntu0.1 (apt);
setuptools stays 78.1.1 (vLLM pins it) with jaraco.context 6.1.0 / wheel 0.46.3
from the locked setuptools 82.0.1 wheel, exercised in the build (jaraco.text,
`build_meta.build_wheel` through bdist_wheel, imports of setuptools,
pkg_resources, torch.utils.cpp_extension, vllm); pip is removed everywhere.

Secret findings: rule `gcp-service-account` matches `"type": "service_account"`
inside the anthropic SDK's `execution_identity` docstring. `trivy-secret.yaml`
disables nothing and allows no path: an exclude-block of three literal phrases
read verbatim from the image. Each of the 11 excluded findings is listed in the
evidence file. `tests/llm_agent/test_trivy_secret_config.py` (Trivy run with
`EWALLET_RUN_TRIVY=1`) proves on a fixture laid out like the image that
synthetic service-account keys (gcp-service-account and private-key), AWS and
GitHub tokens are still reported, including in the docstring files and right
after the docstring, while only the docstring findings disappear. Verdict:
**narrow, independently tested; secret gate closed**.
