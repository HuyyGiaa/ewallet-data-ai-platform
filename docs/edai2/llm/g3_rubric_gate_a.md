# G3 Gate A: EDAI-2 rubric map and approvals required

| Item | Value |
|---|---|
| Date | 2026-11-01 |
| Source | branch `codex/llm-g2-llmd-routing` at `d597176` (G2 `1979153` + G3-S2 patch) |
| Rubric | `EDAI K11.xlsx`, sheet `edai-2 (50%)`, 55 scored rows, sum E3:E57 = **100** |
| Rubric copy used | `~/Downloads/edai-k11-cw-v0.0.2/edai-k11-cw/rubic/EDAI K11.xlsx` (2026-10-02). The owner's export `EDAI K11(20261101-160659).xlsx` was not found on this host; any difference must be re-checked. |
| Live cluster (read-only, 2026-11-01) | GKE `ewallet-dev`: 2 MLOps nodes (24 healthy pods, KServe `fraud-detector` Ready, `ml-platform/fraud-api`, MLflow, KFP, `feature-store/redis`). Not installed: kagent, Substrate, KEDA, agentregistry, NGINX, Vault, observability stack, Airflow, DataHub, Jenkins |
| Local host | DataHub quickstart stopped; no Airflow or Jenkins container |

This document is a planning artifact. **No row is VERIFIED here unless runtime
evidence already exists.** Points are a conservative estimate.

**Statuses:**
- **VERIFIED:** runtime evidence exists.
- **PARTIAL:** source or some runtime exists, but the rubric proof is incomplete.
- **OPEN:** missing.
- **BLOCKED:** needs an approval listed in §3.

## 1. What the instructor's links pin down

| Row | Linked reference | Consequence |
|---|---|---|
| 3 | llm-d quickstart | llm-d already covered by G2 (standalone mode) |
| 7 | kagent BYO OpenAI provider | ModelConfig → agentgateway (source exists) |
| 8, 21, 26, 41 | agentregistry (`aregistry.ai`) | Agent Registry = agentregistry, not CRDs |
| 19, 25 | KEDA | autoscaling via KEDA ScaledObjects |
| 20 | kagent `agent-sandbox` example | Agent must run in a kagent sandbox |
| 27 | kagent **1.x** Agent Substrate | Substrate WorkerPool / warm-up. The repo pins kagent 0.10.3 + Substrate 0.0.9; the 1.x docs must be matched against 0.10.3 |
| 37 | NGINX Ingress Controller | external routing via NGINX |
| 40 | kagent first-mcp-tool | kagent UI chat |
| 54 | HashiCorp Vault (or similar) | centralized secret management |

## 2. Row-by-row map

| Row | Pts | Requirement (short) | Current source / runtime | Status | Est. pts | Needed for full credit |
|---|---|---|---|---|---|---|
| 3 | 2 | LLM inference platform + benchmark doc | G1 GKE runtime; O1 + G2 reports | VERIFIED (branch evidence, not integrated) | 2 | integration into canonical |
| 4 | 2 | custom model | Qwen3-0.6B pinned, model-in-image, G1 runtime | VERIFIED (branch) | 2 | — |
| 5 | 1 | agentgateway security & policy | G1/G2: 401s, rate limits, failover | VERIFIED (branch) | 1 | — |
| 6 | 2 | benchmark + optimize | O1 (C1 +51 %), G2 (`MIXED`) | VERIFIED (branch) | 2 | — |
| 7 | 2 | global ModelConfig via agentgateway | `modelconfig.yaml` (source) | BLOCKED (kagent) | 0 | kagent installed, ModelConfig applied, trace to vLLM |
| 8 | 2 | Agent registry + UI screenshot | `infra/helm/agentregistry` pins (0.4.0) | BLOCKED (agentregistry) | 0 | registry deployed, UI with agents |
| 9 | 2 | RAG pipeline text→chunk→embedding→Feast, Airflow run + DataHub lineage | DAGs, RAG Feast contract (source) | OPEN | 0 | Airflow + DataHub runtime, RAG Feast publication (needs MLOps access §3-G) |
| 10 | 2 | RAG data governance | DataHub assertions (source/tests) | OPEN | 0 | DataHub runtime readback |
| 11 | 2 | incremental offline→online materialization (Airflow) | Feast repo, MLOps campaign | PARTIAL (not re-audited) | 0 | Airflow run screenshot |
| 12 | 2 | streaming feature jobs to offline & online | Flink/Kafka local stack | PARTIAL (not re-audited) | 0 | job run + data readback |
| 13 | 2 | KFP 4-step training pipeline | KFP on GKE, F9 pipeline | PARTIAL (not re-audited) | 0 | KFP run screenshot bound to SHA |
| 14 | 2 | MLflow versioning + auto tag | MLflow on GKE | PARTIAL (not re-audited) | 0 | model/data version + tag evidence |
| 15 | 2 | KServe model-in-image + successful call | KServe `fraud-detector` Ready on GKE | PARTIAL (runtime exists, call not re-captured) | 0 | KServe call evidence |
| 16 | 1* | Web API FastAPI + pydantic + k8s health | Fraud API (FastAPI) on GKE; MCP services (source) | PARTIAL | 0 | Web API pulling user and chunk by ID from the online store, deployed |
| 17 | 1 | async concurrency | async FastAPI | PARTIAL | 0 | runtime |
| 18 | 2 | MCP tool via Helm + rolling update + auto fallback | `mcp-services.yaml` (source) | OPEN | 0 | deploy, rolling update and rollback evidence |
| 19 | 2 | Agent with MCP tool, multi-replica, KEDA autoscale | agents chart (source) | BLOCKED (kagent, KEDA) | 0 | scale-out/in evidence |
| 20 | 1 | Agent in Sandbox | SandboxAgent (source) | BLOCKED (Substrate + gVisor) | 0 | sandbox runtime evidence |
| 21 | 1 | publish Agent to registry | — | BLOCKED (agentregistry) | 0 | registry entry |
| 22 | 8 | 2nd Agent: online features + ML inference tool + RAG tool | feature-rag specialist + `feature-rag-mcp` (source) | BLOCKED (kagent, MLOps access, RAG data) | 0 | real Feast, KServe and RAG calls via the agent |
| 23 | 2 | agentgateway as MCP Gateway + policies | `mcp-gateway` (source, agentgateway standalone) | OPEN | 0 | deployed gateway, allow/deny evidence |
| 24 | 2 | agentgateway as A2A Gateway + policies | `a2a-gateway` (source) | OPEN | 0 | Coordinator→gateway→specialist trace |
| 25 | 2 | Coordinator multi-replica + KEDA + kagent UI chat | coordinator (source) | BLOCKED (kagent, KEDA) | 0 | scale test screenshot, UI chat |
| 26 | 2 | publish Coordinator to registry | — | BLOCKED (agentregistry) | 0 | registry entry |
| 27 | 2 | Substrate warm-up, many agents on few workers | WorkerPool (source) | BLOCKED (Substrate + gVisor) | 0 | WorkerPool warm-up evidence |
| 28 | 1 | unit coverage > 90 % (Web API, fixtures, mocks) | many tests; no coverage report | OPEN | 0 | `coverage` report on a defined scope |
| 29 | 2 | equivalence partitioning + BVA, parametrized | partial in tests | PARTIAL | 0 | mapped test file |
| 30 | 2 | mutmut on changed code, score > 80 % | — | OPEN | 0 | mutmut run |
| 31 | 2 | property-based idempotency (Hypothesis) | — | OPEN | 0 | Hypothesis tests |
| 32 | 2 | Locust load test, HTML | — | OPEN | 0 | Locust HTML |
| 33 | 2 | CI/CD RAG pipeline (Jenkins run) | `Jenkinsfile.rag` (source) | OPEN | 0 | Jenkins runtime + run |
| 34 | 2 | CI/CD MCP + data agent, prompts from Langfuse | `Jenkinsfile.llm-agent`; prompts in repo | OPEN | 0 | Jenkins run; Langfuse deployed and used |
| 35 | 2 | CI/CD coordinator | as 34 | OPEN | 0 | as 34 |
| 36 | 2 | CI/CD stream feature jobs | — | OPEN | 0 | Jenkins run |
| 37 | 2 | metrics UI behind NGINX | — | BLOCKED (NGINX) | 0 | NGINX + Grafana route |
| 38 | 2 | logs UI behind NGINX | — | BLOCKED (NGINX) | 0 | NGINX + logs UI |
| 39 | 2 | traces UI behind NGINX | — | BLOCKED (NGINX) | 0 | NGINX + traces UI |
| 40 | 2 | agent test UI behind NGINX | — | BLOCKED (NGINX, kagent) | 0 | kagent UI route |
| 41 | 2 | registry UI behind NGINX | — | BLOCKED (NGINX, agentregistry) | 0 | registry UI route |
| 42 | 2 | basic auth + rate limit at gateway | — | BLOCKED (NGINX) | 0 | 401/429 evidence |
| 43 | 1 | domain + HTTPS for Web API | cert-manager exists on GKE | BLOCKED (domain, public IP) | 0 | DNS + certificate |
| 44 | 1 | Terraform | `infra/terraform/gcp` + plan evidence | VERIFIED (I7) | 1 | screenshot of applied run |
| 45 | 1 | Ansible roles | — | OPEN | 0 | Ansible roles + run |
| 46 | 1 | Web API metrics | instrumentation (source) | OPEN | 0 | Prometheus/Grafana readback |
| 47 | 1 | CPU/RAM/disk/network metrics | — (GKE Managed Prometheus exists) | OPEN | 0 | dashboard |
| 48 | 1 | logs | otel-collector (source) | OPEN | 0 | Loki readback |
| 49 | 1 | traces | otel-collector (source) | OPEN | 0 | Tempo readback |
| 50 | 2 | LLM tokens, RTT, TTFT, PII trigger freq. | dashboards JSON (source) | OPEN | 0 | live metrics |
| 51 | 2 | per-agent calls, per-tool calls, failures | `observability.py` (source) | OPEN | 0 | live metrics |
| 52 | 1 | A/B test agents with different model configs | — | OPEN | 0 | A/B design + dashboard |
| 53 | 1 | A/B test LLM versions | — | OPEN | 0 | A/B on inference platform |
| 54 | 1 | Vault (or similar) | Workload Identity, K8s Secrets | OPEN | 0 | Vault deployed and used |
| 55 | 2 | README + docs rule (A2) | README, docs | PARTIAL | 0 | README rewrite per A2 |
| 56 | 2 | novel idea 1 + proof | candidates: O1 CPU OMP binding, G2 cache-aware evidence | PARTIAL | 0 | documented idea + proof not already in EDAI |
| 57 | 2 | novel idea 2 + proof | candidates: frozen feature contract, registry publication | PARTIAL | 0 | as 56 |

\* E16 is part of the Web API block (B16/C16).

**Conservative estimate now: 8 / 100**. That is rows 3–6 (7, branch evidence)
plus row 44 (1). Rows 11–15 likely carry runtime evidence on the MLOps
campaign branch; they were not re-audited here, so 0 is claimed.

## 3. Approvals required (cluster-wide, cloud or public exposure)

The current approval is kagent without Substrate, at most 2 temporary
e2-standard-4 nodes, and scoped MLOps access. The rubric needs more than that.

| Id | Change | Rows | Cluster-scoped objects | Requests (approx.) | Notes |
|---|---|---|---|---|---|
| A | kagent 0.10.3 (CRDs, controller, UI, Postgres) | 7, 19–27, 40 | 9 CRDs, 2 ClusterRoles, 1 binding | 0.3 CPU / 0.5 GiB | already approved in principle |
| B | KEDA 2.21.0 | 19, 25 | CRDs, operator, metrics APIService (cluster-wide) | ~0.3 CPU / 0.5 GiB | APIService affects the whole cluster's external metrics |
| C | agentregistry 0.4.0 | 8, 21, 26, 41 | to verify (chart pinned) | ~0.3 CPU / 0.5 GiB | registry UI |
| D | Substrate 0.0.9 + a GKE Sandbox (gVisor) node pool | 20, 27 | 3 CRDs, 5 ClusterRoles + 5 bindings, ValidatingAdmissionPolicy, `atelet` DaemonSet on **every node incl. MLOps**, Valkey ×6, RustFS (default creds must be replaced) | ~1.5 CPU / 2.5 GiB + one e2-standard-4 sandbox node | a **new node pool**; the 2-node ceiling would become 3 |
| E | NGINX Ingress Controller + external LoadBalancer IP + domain + TLS | 37–43 | IngressClass, ClusterRoles, admission webhook | ~0.2 CPU / 0.3 GiB + LB (~US$18/month) | **public exposure**; a domain is required (owner supplies or nip.io-style) |
| F | Vault (single instance, dev-grade storage) | 54 | none if namespaced; injector webhook is cluster-scoped | ~0.25 CPU / 0.25 GiB | — |
| G | MLOps scoped access | 9, 16, 22 | none | — | additive NetworkPolicies in `feature-store`, `fraud-serving` and `ml-platform`; Workload Identity binding for GCS registry read/write (exact binding to confirm); Redis Secret replicated without printing; RAG Feast project publication in the shared Redis |
| H | Observability stack (Prometheus, Loki, Tempo, Grafana; in-namespace, opt-in chart) | 37–39, 46–51 | node metrics: GKE Managed Prometheus already present | ~0.25 CPU / 0.9 GiB | — |
| I | Langfuse (prompt management) | 34, 35 | none | heavy (v3 needs ClickHouse, Redis, object store) | run locally (Docker) is cheaper |
| J | Airflow + DataHub + Jenkins runtimes | 9–11, 33–36 | none (local Docker on the laptop) | laptop: DataHub ~6 GiB, Airflow ~2 GiB, Jenkins ~1 GiB | memory note: local LLM workloads starved DP2 Airflow before |

**Resource arithmetic.** Each temporary node has ~2.8 CPU safe.

| Run together | CPU needed | Nodes |
|---|---|---|
| A + B + C + G + H + vLLM | ~1.5 + vLLM 2 + MCP/gateways 0.6 | 2 temporary nodes |
| D (Substrate + gVisor) | +1.5 CPU plus a dedicated sandbox node | 3 nodes |
| E | small, but adds a public IP | — |

So the full rubric is **not reachable** inside the 2-node ceiling **if
everything runs at once**. The proposal is sequential phases on at most
3 temporary nodes, cleaned up after each phase.

## 4. Proposed phases (each bounded and cleaned up)

1. **Data path** (G, J): MLOps scoped access; RAG publication through a local
   Airflow run to the GKE Redis/Feast; DataHub lineage; `feature_lookup`,
   `chunk_lookup` and `knowledge_search` live.
2. **Agents and gateways** (A, B, C, H): kagent, MCP Gateway, A2A Gateway,
   Coordinator plus two specialists, KEDA scale test, registry, observability.
3. **Sandbox/Substrate** (D): only with node-pool approval.
4. **External routing** (E, F): NGINX, TLS, Vault.
5. **CI/CD and tests** (I, J): Jenkins runs, Langfuse, coverage, mutmut,
   Hypothesis, Locust.

## 5. Audits for the blocked installations (read-only, 2026-11-01)

Owner decisions (2026-11-01):
- **Approved:** scoped MLOps access (namespace-scoped changes); observability,
  if it is namespace-scoped and fits.
- **Exact manifest review first:** kagent.
- **Audit first:** KEDA, agentregistry and Substrate.
- **Preserve Istio:** no NGINX, no public LoadBalancer.
- **Secrets:** audit GCP Secret Manager and Jenkins, not Vault.
- **Tooling:** hybrid.
- **No IAM broadening and no new nodes beyond 2.**

### 5.1 kagent 0.10.3 (rows 7, 19–26, 40)

| Item | Finding |
|---|---|
| Charts | `kagent-crds` OCI `sha256:76582530…8da0`; `kagent` OCI `sha256:2f355400…9063` (match `kagent-pilot/versions.env`) |
| Cluster-scoped | **only the 9 CRDs** (`agentharnesses`, `agents`, `mcpservers`, `memories`, `modelconfigs`, `modelproviderconfigs`, `remotemcpservers`, `sandboxagents`, `toolservers` `.kagent.dev`). The chart's 2 ClusterRoles (metrics-auth, metrics-reader) render only when `controller.metrics.enabled=true`, and `kagent-values-g3.yaml` turns that off |
| Namespaced | `kagent`: 3 Deployments (controller, UI, bundled Postgres), 3 Services, 1 PVC, 3 ConfigMaps, 1 Secret, 3 SAs, getter/writer Role + RoleBinding. `ewallet-agents`: getter/writer Role + RoleBinding |
| Images (digest) | controller `sha256:b2572a59…9b76`, UI `sha256:13f62914…1127`, Postgres 18.6-alpine3.23 `sha256:1a0c23d5…8c29`, Go ADK `sha256:5ee9592c…da1f` |
| Requests | 0.3 CPU / 0.5 GiB |
| Gap | the bundled Postgres template has no `nodeSelector`, so a Helm post-renderer adds the G3 node selector to every workload missing one (repo script) |
| Auth | controller `AUTH_MODE=unsecure` (cluster-internal); the UI is reached only through a localhost port-forward until rows 40/42 |
| Rollback | `helm uninstall kagent -n kagent`, then delete the 9 CRDs (removes all kagent CRs) |

### 5.2 KEDA 2.21.0 (rows 19, 25)

| Item | Finding |
|---|---|
| Chart | tgz `sha256:a7da56bf…6dee` (matches `infra/helm/keda/versions.env`) |
| Cluster-scoped | 6 CRDs, 4 ClusterRoles + 4 ClusterRoleBindings, **APIService `v1beta1.external.metrics.k8s.io`**, ValidatingWebhookConfiguration `keda-admission` |
| Conflict check | the cluster serves only `v1beta1.metrics.k8s.io` (metrics-server), so there is **no existing external-metrics APIService**; HPA works today (istiod, kube-state-metrics) |
| Requests | 0.125 CPU / 0.26 GiB |
| Alternative | plain HPA on CPU (no install) exists; the rubric links KEDA explicitly, so HPA would be an equivalence claim |

### 5.3 agentregistry 0.4.0 (rows 8, 21, 26, 41)

| Item | Finding |
|---|---|
| Chart | OCI `sha256:77724562…8685`, tgz `sha256:757da462…578d` (match pins) |
| Cluster-scoped | **none** |
| Namespaced | server Deployment + bundled Postgres Deployment, 2 Services, PVC, Secret, ConfigMap |
| Requests | 0.35 CPU / 0.4 GiB |
| Gap | bundled `postgres:18` by tag, so it must be pinned by digest before install |

### 5.4 Substrate 0.0.9 (rows 20, 27): correction

The earlier claim that Substrate needs a dedicated GKE Sandbox (gVisor) node
pool is **not supported by the chart**:
- `WorkerPool.sandboxClass` is `gvisor|microvm`;
- the `atelet` DaemonSet fetches the `runsc` binary itself (`SandboxConfig`
  assets) and runs **privileged with a hostPath**.

| Item | Finding |
|---|---|
| Cluster-scoped | 3 CRDs, 5 ClusterRoles + 5 bindings, ValidatingAdmissionPolicy + binding, cluster-wide `SandboxConfig` |
| Privileged | `atelet` DaemonSet: `privileged: true`, hostPath `/var/lib/ateom-gvisor`, **no nodeSelector in the chart**. Without a post-renderer it lands on both MLOps nodes |
| Defaults to replace | RustFS credentials `rustfsadmin` (static); Valkey `valkey:8.0` and the bundled agentgateway `v1.3.0-alpha.1` are tags, not digests; Valkey 6 replicas |
| Footprint | system ≈ 1–1.5 CPU / 1.5–2.5 GiB plus 3 WorkerPools × 0.5 CPU / 1 GiB. Together with vLLM (2 CPU) and kagent (0.3 CPU) this **does not fit 2 temporary nodes** (~5.6 CPU safe), so it needs a sequential phase or a third node |
| Cluster RuntimeClasses | `gvisor`, `microvm` and others already exist (GKE-managed) |
| Approval needed | the cluster-wide install with a post-renderer pinning `atelet` and the workers to G3 nodes, replaced credentials, digest-pinned images, and the capacity plan |

### 5.5 External routing, secrets, Jenkins (rows 37–43, 54, 33–36)

| Item | Finding |
|---|---|
| Istio | ambient 1.30.7; `gateways.networking.istio.io` CRD present; **no Gateway API CRDs, no ingress gateway**. Reaching it from outside needs a gateway Deployment and a LoadBalancer (public), so it needs approval |
| GCP Secret Manager | **API disabled** in the project. Enabling it is a project change (approval) |
| Jenkins VM | **no VM exists** (only 2 GKE node VMs). Terraform/Ansible Jenkins VM not provisioned (approval: VM cost) |
| Airflow / DataHub | none running; DataHub quickstart containers exist locally (stopped) |
