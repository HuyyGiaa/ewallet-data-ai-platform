# Agent platform completion: pre-implementation audit (AUD-01 to AUD-06)

> **Archived 2026-11-17 (historical pre-campaign audit and design).** It records the options and owner decisions before the GKE campaigns; the implemented fleet is described in [kagent_bounded_pilot.md](../../../../../docs/edai2/agents/kagent_bounded_pilot.md) (Agent Fleet Architecture) and the measured results in [AGENT_PLATFORM_COMPLETION.md](../../../../../docs/edai2/agents/AGENT_PLATFORM_COMPLETION.md).

> **Status: AUDIT ONLY.** Source and documentation review plus a reading of the
> pinned upstream sources. No cluster, registry, bucket, Secret or Helm release
> was read or changed. Nothing here is runtime evidence, and no rubric row
> changes status.

| Item | Value |
|---|---|
| Branch | `feature/edai2-agent-platform-integration` |
| Baseline | `241c19b2` (P0–P2 remediation, pushed; `origin/main` unchanged at `06eb47a7`) |
| Target rows | 18, 19, 20, 22, 25, 27 (KEDA **and** Substrate are both mandatory) |
| Preserved | Fraud, Drift and Combined workflows (`g3-orchestration/v1` and `v2`), AGI-01–14 |

## 1. Sources consulted

| Component | Version | How it was read |
|---|---|---|
| kagent | 0.10.3 | Tag source tarball (`go/core/...`); CRD schemas in [`crd-schemas-kagent-0.10.3.json`](../../../../../infra/kubernetes/kagent-platform/crd-schemas-kagent-0.10.3.json) |
| Substrate | 0.0.9 | `substrate-crds` chart, digest `sha256:8a9e7ad0…`, equal to [`versions.env`](../../../../../infra/kubernetes/kagent-pilot/versions.env); `substrate` chart `sha256:8063aa18…`; tag source (`docs/`, `cmd/atecontroller`) |
| Substrate, later | v0.1.0 to v1.0.1 | WorkerPool CRD per tag (raw GitHub) |
| KEDA | 2.21.0 | Public chart (`templates/manager/clusterrole.yaml`, `values.yaml`); installed values in [`infra/helm/keda/values.yaml`](../../../../../infra/helm/keda/values.yaml) |
| Kubernetes HPA | `release-1.34`, `pkg/controller/podautoscaler/horizontal.go` (GKE runs 1.36; the code path is unchanged in recent releases) | Raw source |
| Repository | `241c19b2` | Chart, agents, MCP, experiments, runbooks, G5-B KEDA evidence |

## 2. Verified facts

| # | Fact | Source |
|---|---|---|
| F1 | The WorkerPool `/scale` subresource defines `specReplicasPath` and `statusReplicasPath` but **no `labelSelectorPath`** | `substrate-crds-0.0.9` `templates/ate.dev_workerpools.yaml` lines 432–436 |
| F2 | The HPA controller requires a selector for **every** metric type: `computeReplicasForMetrics` calls `validateAndParseSelector`, which fails with `selector is required` | `horizontal.go` lines 306 and 387–389 |
| F3 | Consequence: a KEDA ScaledObject targeting a 0.0.9 WorkerPool creates an HPA that never scales (`ScalingActive=False`). The chart's sandbox branch renders exactly this object | [`autoscaling.yaml` lines 6–48](../../../../../infra/helm/llm-agent-platform/templates/autoscaling.yaml) |
| F4 | `atecontroller` owns the worker Deployment (`Owns(&appsv1.Deployment{})`) and server-side applies it on every reconcile with `ForceOwnership` and `replicas = WorkerPool.spec.replicas`. Scaling the worker Deployment directly would be reverted | `cmd/atecontroller/internal/controllers/workerpool_controller.go` (`applyDeployment`, `SetupWithManager`), `workerpool_apply.go` line 75 |
| F5 | Upstream added `labelSelectorPath: .status.selector` in Substrate v0.5.0-alpha3 (absent in v0.0.9 to v0.4.0). kagent 0.10.0–0.10.6 all pin `kagent-dev/substrate v0.0.9`; only kagent 1.0.x pairs with Substrate 1.0.0 | `go/go.mod` `replace` lines per kagent tag |
| F6 | **Several agents can share one warm WorkerPool.** kagent builds each SandboxAgent's ActorTemplate with `workerSelector: kagent.dev/worker-pool=<pool>`; a pool is resolved in the SandboxAgent's namespace. Substrate's multi-template demo runs two templates on one pool | kagent `pkg/sandboxbackend/substrate/lifecycle_shared.go` lines 79–86 and 106–130, `agent_lifecycle.go` (`buildSandboxAgentActorTemplate`); Substrate `demos/multi-template/README.md` |
| F7 | One actor per A2A **session** (`contextId`): created or resumed on a request, then suspended after the response, which frees the worker. The kagent transport rejects a request without `contextId` or `X-User-Id` | kagent `agent_actor.go` (`EnsureSessionActor`, `SuspendSessionActor`), `internal/a2a/substrate_sandbox_transport.go` lines 57–174 |
| F8 | A worker hosts at most one running actor at a time; density comes from suspend/resume over time. Worker autoscaling is a roadmap item, not a Substrate feature | Substrate `docs/architecture.md` (Worker record, "Problems we haven't addressed"), `docs/roadmap.md` line 25 |
| F9 | Substrate 0.0.9 exports no busy-worker or active-actor metric; only `rpc.server.call.duration`, `atenet.router.route.duration` and `atelet.snapshot.size` | Substrate `docs/observability.md` lines 106–111 |
| F10 | kagent exposes `GET /api/substrate/status`: WorkerPools, ActorTemplates (golden snapshot), actors (`RUNNING`/`SUSPENDED`) and workers with their assigned actor. This is the worker-assignment evidence source | kagent `internal/httpserver/handlers/substrate.go` |
| F11 | KEDA 2.21's default operator RBAC grants `*/scale` (get, list, patch, update) for every API group. The installed KEDA watches only `ewallet-agents,ml-platform` | KEDA chart `values.yaml` (`rbac.scaledRefKinds`); `infra/helm/keda/values.yaml` |
| F12 | SandboxAgent and Agent have no `/scale`. The kagent BYO `deployment` schema has no `terminationGracePeriodSeconds` or `lifecycle` | `crd-schemas-kagent-0.10.3.json` |
| F13 | Worker pods run as the namespace `default` ServiceAccount (`WorkerPool.template` has no `serviceAccountName`). An actor therefore has no Kubernetes token, so the ModelConfig resolver (needs `get modelconfigs`) and possibly the kagent task store cannot authenticate from a sandbox | [`agent-sandbox/README.md`](../../../../../infra/kubernetes/agent-sandbox/README.md); `model_config.py` |
| F14 | The A2A Gateway's Specialist routes forward to `<agent>.ewallet-agents.svc:8080` Services, which a SandboxAgent does not have. The only controller route is `/api/a2a/ewallet-agents/`. The Coordinator sends neither `contextId` nor `X-User-Id` | [`policy-gateways.yaml`](../../../../../infra/helm/llm-agent-platform/templates/policy-gateways.yaml); `programmatic_coordinator.py` `GatewayPolicy.call_for` |
| F15 | `ewallet_agent_inflight_tasks{agent}` is exported per pod (`service.instance.id`), but only by images after `2af8b596` (2026-11-06). The live r42 BYO images (`017431d`, 2026-11-04) predate it. A dead pod's series persists for the collector's default 5-minute expiry | `agent_metrics.py`, `byo_runtime.py` (`configure_agent_metrics`); C-B runbook P4 "Known limit" |
| F16 | MCP failover: client-side bounded retry (≤ 3 attempts, read-only tool allowlist, only transport loss or 502/503/504), shared by both Specialists. agentgateway 1.7 does not retry MCP backends. Production MCP servers and the MCP Gateway run **1 replica** (`g3.yaml`, `mcp-isolated.yaml`), so there is no second backend to fail over to | `mcp_failover.py`; `g3/mcp-failover.yaml`; `g3/mcp-gateway-failover.yaml` |

## 3. Audit results

| Audit | Result | Severity of the blocking findings |
|---|---|---|
| AUD-01 Runtime and topology | **FIX_REQUIRED** | P0 (Coordinator cannot reach a SandboxAgent), P1 (identity, token, resolver in an actor) |
| AUD-02 KEDA | **BLOCKED** for KEDA→WorkerPool on the pinned CRD; **FIX_REQUIRED** for KEDA→Deployment | P0 (F1–F4), P1 (gauge images, scale-in, watchNamespace) |
| AUD-03 MCP fallback | **FIX_REQUIRED** | P1 (single replica; no measured failure injection) |
| AUD-04 Security and isolation | **FIX_REQUIRED** | P1 (sandbox credentials, snapshot secrecy, mesh path), P2 (Redis access is application-level) |
| AUD-05 Deployment and rollback | **FIX_REQUIRED** | P1 (cluster-scoped changes need explicit approval and an order) |
| AUD-06 Evidence | **PASS** (plan complete, nothing executed) | — |

### AUD-01 — Runtime and topology compatibility

| Question | Finding |
|---|---|
| Which business agents can run in Sandbox? | **Feature-RAG and Drift Specialists**: deterministic BYO servers, no Kubernetes API, no LLM on the typed path. They run as BYO SandboxAgents with `cmd`, `PATH`, `PYTHONPATH` and `EWALLET_AGENT_PORT=80` (already supported by `listen_port`). **Coordinator: not recommended.** It needs a Kubernetes token for the ModelConfig resolver (F13) and Redis idempotency, and a session actor that suspends after each response conflicts with its multi-minute delegations. |
| Reusable BYO implementations | The same three images and modules; no new runtime. `infra/kubernetes/agent-sandbox/arm-optimized.yaml.tmpl` and `scripts/experiments/render_sandbox.py` already render a BYO SandboxAgent. |
| Do MCP and A2A connectivity survive? | MCP: same gateway route (`/feature-rag/mcp`, `/drift/mcp`) via `sandbox.network.allowedDomains`. Unproven: the ztunnel path for gVisor actors (open question S1), and admitting `cluster.local/ns/ewallet-sandbox/sa/default` ([`s1-access.yaml`](../../../../../infra/kubernetes/agent-sandbox/s1-access.yaml)). A2A: **broken without changes** (F14). |
| ServiceAccounts, identities, Secrets | One identity per sandbox namespace (`default`, no token). Each Specialist needs its own MCP credential subject, a new entry in the MCP servers' token tables. The token lives in actor memory and therefore in golden and session snapshots. |
| Can the Coordinator delegate to Sandbox-backed Specialists? | Only after source changes (section 6). A Coordinator→Specialist call must go to the A2A Gateway path `/api/a2a-sandboxes/ewallet-sandbox/<agent>/`, served by a new `kagent-a2a-sandbox` route. It must carry `contextId` (the member `request_id`) and `X-User-Id` (a fixed service user). The kagent controller then selects the session actor. |
| Does any change break AGI? | No, if the Sandbox route is a third topology switch (`agents.specialistRuntime: deployment` by default). AGI's BYO-Deployment profile is the fallback and the rollback. |

Unresolved, runtime-only questions (from the [Sandbox README](../../../../../infra/kubernetes/agent-sandbox/README.md), not answerable from source):

1. Can uid 10001 bind port 80 in the gVisor actor?
2. Do actor packets traverse ztunnel (mTLS identity)?
3. Can the BYO task store (`KAGENT_URL`) authenticate from an actor?
4. Does the OpenTelemetry gRPC channel survive suspend/resume?

### AUD-02 — KEDA integration

| Check | Deployment target (BYO agent) | WorkerPool target (Substrate 0.0.9) |
|---|---|---|
| `/scale` subresource | Yes | Yes (F1) |
| Selector for the HPA | Yes | **No**: HPA fails with `selector is required` (F1, F2) |
| Scaling the underlying Deployment instead | — | Reverted by `atecontroller` (F4) |
| KEDA RBAC | Yes | Yes, default `*/scale` (F11) |
| KEDA watches the namespace | `ewallet-agents`: yes | `ewallet-sandbox`: **no** (F11); needs a KEDA `watchNamespace` change |
| Metrics adapter | `v1beta1.external.metrics.k8s.io` Available (G5-B record) | same |
| Prometheus reachable from KEDA | Yes: AuthorizationPolicy and NetworkPolicy for `keda-operator` in `ewallet-agents` (G5-B) | same Prometheus |
| Trigger metric exists | `ewallet_agent_inflight_tasks`: **only with rebuilt images** (F15); `keda_preflight.py` checks it | No Substrate metric (F9). Options: the Specialists' own in-flight gauge exported from actors, or `rate(atenet_router_route_duration_count[1m])` after pointing Substrate's `otel.endpoint` at the collector |
| Arithmetic | `AverageValue`: `desired = ceil(Σ in-flight / threshold)`, clamped to min 2, max 3 (chart guard) | `desired workers = ceil(Σ running sessions / 1)` (one actor per worker, F8) |
| HPA ownership | `transferHpa: true` adopts the chart HPA by name (tested locally, C-B P4) | No previous HPA |
| Rollback | Delete the ScaledObjects first, then upgrade without `g3/keda.yaml` (C-B P4). A plain upgrade leaves no autoscaler | Delete the ScaledObject; the WorkerPool keeps its last `spec.replicas`; reset it in the same release |
| Scale-in safety | A pod removed mid-task drops its A2A task; Coordinator idempotency marks it started, so a retry is `idempotency_ambiguous`. No grace-period field in the BYO schema (F12) | Workers run actors one at a time. A busy worker removed by scale-in loses its actor's running request; the suspended state stays in snapshots |
| Stale metric | A dead pod's gauge lingers up to 5 min (F15) | Same, for actor-exported gauges, and a suspended actor cannot export its final decrement |

**KEDA→WorkerPool options on the pinned versions**, smallest first:

1. **B′ — CRD selector patch (recommended to try first, in a gate).**
   - Add `spec.scaleSelector: string` to the WorkerPool schema and
     `labelSelectorPath: .spec.scaleSelector` to its `/scale` subresource.
   - Set `scaleSelector: ate.dev/worker-pool=<pool>` on the WorkerPool, which
     matches the worker pods' label.
   - `atecontroller` decodes WorkerPools into a typed struct and writes only
     `status`, so the field persists. `.status.replicas` keeps coming from the
     worker Deployment. The HPA writes `spec.replicas`, which `atecontroller`
     applies (F4).
   - Costs:
     - a cluster-scoped edit of a Helm-managed third-party CRD, which a
       `substrate-crds` upgrade reverts (record and re-apply it);
     - it must be proven on a local cluster first (CP2), then on GKE.
2. **C — Upgrade to kagent 1.0.x with Substrate 1.0.0** (native selector). This is a
   platform upgrade (CRDs, controller, UI): out of scope here, listed for completeness.
3. **Fallback A+S — KEDA on Deployments plus a separate Substrate fleet** (section 5.2).

### AUD-03 — MCP fallback (row 18)

| Mechanism | State |
|---|---|
| Client-side retry | Implemented and tested, read-only tools only. `drift_detection` is in the allowlist; no tool has side effects |
| Backend replica failover | Absent in production: 1 replica per MCP server (F16) |
| RollingUpdate availability | `maxUnavailable: 0, maxSurge: 1` ([`mcp-services.yaml`](../../../../../infra/helm/llm-agent-platform/templates/mcp-services.yaml)). Not exercised under load |
| Service-level recovery | Kubernetes restarts the pod; with one replica, calls fail until it is Ready |
| Gateway | agentgateway does not retry MCP; one gateway replica is itself a single point of failure |

Minimum change for row 18 (no weaker authorization, no unsafe retries):

- `g3/mcp-failover.yaml`: 2 replicas, topology spread, PDB re-enabled for `feature-rag-mcp`.
- `g3/mcp-gateway-failover.yaml`: 2 gateway replicas.
- `EWALLET_MCP_MAX_ATTEMPTS=3` (the default).

Failure injection:

- run the existing isolated `scripts/experiments/mcp_experiment.sh`
  (namespace `ewallet-mcp-exp`, per-run token, kills one pod mid-run, checks that
  production pods are unchanged), baseline against candidate, twice each;
- then a production `kubectl rollout restart` of `drift-mcp` during a bounded
  typed-request loop.

Pass: 0 failed calls and an equal production revision before and after.

### AUD-04 — Security and data isolation

| Area | Finding | Must be verified before rollout |
|---|---|---|
| Sandbox identity | `ewallet-sandbox/default`, no token; one identity for every actor in the pool (F13) | `kubectl auth can-i --list --as=system:serviceaccount:ewallet-sandbox:default` is empty; the namespace has Pod Security `privileged` (worker requirement) and nothing else runs there |
| MCP credentials | Dedicated subjects `sandbox-feature-rag` and `sandbox-drift`, each with its own grants; a production token-table change on both MCP servers (restarts them) | Grants: `feature_lookup, fraud_scoring, knowledge_search` and `drift_detection` respectively. AGI-08-style negative probes with the sandbox tokens |
| Snapshot secrecy | Golden and session snapshots contain process memory, including tokens (`gs://ate-snapshots/ewallet-sandbox/...`) | Bucket IAM limited to Substrate; snapshots deleted and tokens revoked at cleanup |
| Gateways | MCP: Ambient principal list plus server grants. A2A: the new sandbox route is admitted only for the Coordinator principal | `s1-access.yaml` diff; AuthorizationPolicy readback |
| NetworkPolicy | The sandbox namespace needs egress to `mcp-gateway`, `otel-collector`, `kagent-controller`, DNS and the Substrate control plane; ingress from `atenet-router` | Rendered policy diff; a denied probe to an unlisted host |
| Redis | Unchanged: application-level read-only, `requirepass` without ACLs (P1-B). Sandbox Specialists reach Redis only through MCP servers | AGI data-state check before and after |
| ModelConfig | The resolver stays on the Coordinator Deployment. Specialists make no LLM call on the typed path (Feature-RAG's optional NL intake stays off in Sandbox) | AGI-01 |
| Observability egress | Actors export OTLP to `otel-collector`; the collector redaction processor is unchanged | Trace and metric readback of a sandbox session |
| KEDA | `keda-operator` → Prometheus only (existing rules). A `watchNamespace` change touches only KEDA | KEDA Helm diff |

### AUD-05 — Deployment and rollback readiness

| Item | Finding |
|---|---|
| Reusable images and scripts | The three BYO images from one commit (`build_g3_byo_*.sh`); the MCP image (`build_changed_images.sh`); `scan_and_push.sh`; `agi_campaign.sh` (gates); `keda_preflight.py`; `keda_install.sh`; `mcp_experiment.sh`; `sandbox_experiment.sh`; `render_sandbox.py`; `serving_autoscale_experiment.sh` (scale sampling pattern) |
| CRDs present | kagent 0.10.3 (`SandboxAgent`), Substrate 0.0.9 (`WorkerPool`, `ActorTemplate`), KEDA 2.21 (`ScaledObject`). Only the B′ patch alters a CRD |
| Release ownership | `llm-agent` (namespace `default` → objects in `ewallet-agents`); `llm-agent-mcp` (`ewallet-mcp`); `keda` (`keda`); Substrate releases (`ate-system`); `ewallet-sandbox` objects through a new release `llm-agent-sandbox`, so the rollback is `helm uninstall` |
| Isolated testing | Possible for: the sandbox fleet, the WorkerPool, its ScaledObject and the MCP failover experiment. **Unavoidable production dependencies**: the shared kagent controller; the MCP token tables; the A2A Gateway route and the Coordinator (`llm-agent`); KEDA `watchNamespace`; the cluster-scoped WorkerPool CRD patch (B′); Prometheus and the collector |
| Deployment order | See section 7 |
| Rollback order | Reverse: Coordinator back to Deployment routing → delete sandbox ScaledObject → uninstall `llm-agent-sandbox` → remove MCP token entries → restore KEDA `watchNamespace` → revert the CRD patch (only once no WorkerPool uses `scaleSelector`) → KEDA on agents: P4 rollback order |

### AUD-06 — Evidence and observability readiness

**PASS (plan only).** Section 9 maps every item. The G5-B workflow is the model:

- a sampler records replicas and HPA decisions every 5 s;
- owner screenshots are taken from real terminals and Grafana;
- a manifest with `SHA256SUMS` is kept;
- the claims are bounded ("proves the chain reacts; not a performance result").

## 4. Rubric coverage by topology

| Row | B′ (shared WorkerPool scaled by KEDA) | Fallback A+S | Still separate |
|---|---|---|---|
| 18 | MCP failover overlays plus experiment | same | — |
| 19 | KEDA scales the WorkerPool that serves both Specialists | KEDA scales the BYO Specialist Deployments | — |
| 20 | Specialists run as SandboxAgents (gVisor) | Specialist SandboxAgent copies on the fleet | — |
| 22 | Feature-RAG in Sandbox, scaled, in the registry, through UI chat | Deployment scaled by KEDA; the Sandbox copy is evidenced separately | — |
| 25 | KEDA on the Coordinator Deployment (in-flight trigger) | same | — |
| 27 | Shared warm pool: 2 Specialists × N sessions on ≤ 3 workers, assignment and reuse, cold against warm | Same evidence from the fixed warm pool, but the pool is not KEDA-scaled | — |
| 34–35 | — | — | CI/CD evidence |

With fallback A+S, row 27 is evidenced by real worker sharing, and the pool is
warm but fixed-size. Rows 19 and 25 are evidenced by KEDA on Deployments. The
Coordinator's production route would still reach the Deployment Specialists; the
Sandbox Specialists are reached through kagent UI chat and, optionally, through the
Coordinator's sandbox route. The final report must say exactly that.

## 5. Topology

### 5.1 Recommended (B′, gated)

```text
kagent UI → kagent controller → fraud-coordinator (BYO Deployment, ewallet-agents; KEDA in-flight, 2–3)
   fraud-coordinator → agentgateway → vLLM                                   (ModelConfig resolver)
   fraud-coordinator → A2A Gateway /api/a2a-sandboxes/ewallet-sandbox/<agent>/ (contextId, X-User-Id) → kagent controller
        → atenet-router → session actor of feature-rag-specialist | drift-specialist (SandboxAgents, BYO, gVisor)
             on WorkerPool business-agents (ewallet-sandbox; KEDA 1–3 workers via the patched /scale)
        actor → MCP Gateway → feature-rag-mcp | drift-mcp (2 replicas each, client failover)
```

### 5.2 Fallback A+S

The AGI topology unchanged: three BYO Deployments, KEDA on all three. In addition,
`ewallet-sandbox` hosts the two Specialists as SandboxAgents on one fixed warm
WorkerPool (2 workers) for rows 20 and 27. Switch to the fallback at
checkpoint CP2 or CP5 (section 7) without code changes: the sandbox route is a
values switch.

## 6. Required source changes

| # | Change | Files | Severity |
|---|---|---|---|
| S1 | Coordinator delegation to a configured runtime per Specialist. For `sandbox`: path `/api/a2a-sandboxes/<ns>/<agent>/`, `contextId` = member `request_id`, header `X-User-Id: <service user>`. `GatewayPolicy` keeps an exact allowlist of hosts, paths and namespaces | `programmatic_coordinator.py`, `byo_coordinator.py` (env), tests | P0 |
| S2 | A2A Gateway route `kagent-a2a-sandbox` (path prefix `/api/a2a-sandboxes/ewallet-sandbox/`), admitted for the Coordinator only | `templates/policy-gateways.yaml`, values, tests | P0 |
| S3 | Sandbox fleet chart: one shared WorkerPool and two BYO SandboxAgents rendered from the Specialists' existing values (`cmd`, port 80, env, dedicated MCP Secrets), plus NetworkPolicies; a separate release; replaces the one-pool-per-agent `agents.mode=sandbox` branch | new template or a dedicated `agi/sandbox.yaml`, `templates/agents.yaml` guard | P0 |
| S4 | ScaledObject for the shared WorkerPool (trigger: Specialists' in-flight sum, or the router rate) **only with** `scaleSelector` set; a chart guard refuses WorkerPool scaling without it | `templates/autoscaling.yaml`, tests | P0 |
| S5 | B′ CRD patch as a reviewed, idempotent JSON patch with a check-and-revert script (`kubectl get crd workerpools.ate.dev -o json` comparison) | `infra/kubernetes/substrate-scale/` (new), test | P0 (B′ only) |
| S6 | KEDA on agents: switch the Drift trigger to in-flight once BYO; an AGI+KEDA overlay; scale-in drain: on SIGTERM stop accepting work and finish in-flight tasks within a bounded time (uvicorn `timeout_graceful_shutdown`); HPA scale-down window ≥ the longest task | `g3/keda.yaml`, `byo_*.py` `main()`, tests | P1 |
| S7 | Collector `prometheus` exporter `metric_expiration` reduced (for example 60 s) so dead pods stop counting | `templates/otel-collector.yaml`, test | P2 |
| S8 | Evidence tooling: scale sampler for agents and WorkerPools (reuse `serving_autoscale_experiment.sh` sampling); a load driver of typed requests at bounded concurrency; `/api/substrate/status` snapshotter (pools, actors, workers); cold/warm session timer (reuse `sandbox_experiment.sh` measurements) | `scripts/experiments/` | P1 |
| S9 | Runbook sections AGR and AGS on `agi_campaign.sh`-style gates | docs | P1 |

## 7. Implementation and verification order (checkpoints)

| CP | Work | Where | Exit criterion |
|---|---|---|---|
| CP0 | S1, S2, S6, S7, S8 source plus tests; AGI regression; default renders byte-identical | local | all tests pass |
| CP1 | S3, S4, S5 source plus tests; render diffs against the current releases | local | gates in place |
| CP2 | **B′ feasibility on a local cluster** (kind): Substrate 0.0.9 CRDs patched, KEDA 2.21, a stub external metric; the HPA reaches `ScalingActive=True` and writes `WorkerPool.spec.replicas`; `atecontroller` (or a stub) keeps it | local kind | pass → B′; fail → fallback A+S |
| CP3 | Images from one commit, scanned and pushed (A1) | Artifact Registry | digests recorded |
| CP4 | Sandbox S0 (boot in gVisor, port 80, golden snapshot) and S1 (MCP via mesh with sandbox tokens) for both Specialists, isolated namespace | GKE `ewallet-sandbox` | Ready actors; one real tool result each; negative probes denied |
| CP5 | B′ CRD patch on GKE, KEDA `watchNamespace` += `ewallet-sandbox`, WorkerPool ScaledObject | GKE (cluster-scoped, A2) | HPA `ScalingActive=True`; otherwise fallback A+S |
| CP6 | MCP failover: isolated experiment, then production overlays | `ewallet-mcp-exp`, then `ewallet-mcp` | 0 failed calls |
| CP7 | Agents: AGI rollout (AGI runbook Stage 1), then KEDA on the Coordinator (and on Deployment Specialists in A+S) | `ewallet-agents` | ScaledObjects Ready; the gauge is READY in `keda_preflight.py` |
| CP8 | Coordinator sandbox route on; AGI-09–11 rerun through Sandbox Specialists; AGR and AGS experiments | GKE | section 9 pass rules |
| CP9 | Rollback drill or promotion decision (A3) | GKE | prior state restored or decision recorded |

## 8. Safe cloud execution boundaries

| CP | Namespace | Resources | Release | Max footprint | Timeout | Cleanup | Production traffic affected |
|---|---|---|---|---|---|---|---|
| CP3 | — | 4 images | — | — | 1 h | none (immutable) | no |
| CP4 | `ewallet-sandbox` | Namespace, WorkerPool (1–2 workers at 500m/1 GiB), 2 SandboxAgents, 2 Secrets; MCP token entries (production Secret edit, restarts MCP pods) | `llm-agent-sandbox`; `llm-agent-mcp` values | +1 CPU, +2 GiB | 2 h | uninstall, delete snapshots, revoke tokens | brief: MCP pod restart (rolling, after CP6 two replicas) |
| CP5 | cluster | WorkerPool CRD JSON patch; KEDA values | `keda` | ≤ 3 workers (1.5 CPU, 3 GiB) | 1 h | revert the patch, restore KEDA values | no |
| CP6 | `ewallet-mcp-exp`, then `ewallet-mcp`, `ewallet-agents` | isolated namespace; then 2 replicas per MCP server and 2 gateway replicas | `llm-agent-mcp`, `llm-agent` | +250m and 576Mi | 1 h | `mcp_experiment.sh down`; Helm rollback | no for the isolated run; a rolling update for production |
| CP7 | `ewallet-agents` | 3 Agents (images, resolver), ScaledObjects replacing HPAs (transfer) | `llm-agent` | agents 2–3 replicas each (+300m, +768Mi peak) | 2 h | `agi_campaign.sh rollback`; P4 KEDA rollback order | rolling update, no gap (PDB 1) |
| CP8 | all above | load: ≤ 40 typed tasks at concurrency ≤ 8; at most 10 LLM chats | — | as above | 2 h | — | shared vLLM, bounded |

Every mutating CP needs its own approval string, a preflight, an identity-gated
diff and a dry run, as with `agi_campaign.sh`.

## 9. Evidence plan

AGI-01–14 are defined in the
[AGI runbook](../../../../../docs/runbooks/edai2/AGENT_INTEGRATION_EVIDENCE_RUNBOOK.md) and
[contracts §8](AGENT_INTEGRATION_CONTRACTS.md#8-evidence-to-rubric-matrix). For
B′, AGI-09–11 are repeated with the sandbox route (suffix `-sbx`).

| ID | Proves | Source | Command or UI | PASS only if | Screenshot | Raw | Rows | Blocker now |
|---|---|---|---|---|---|---|---|---|
| AGR-01 | MCP servers and gateway have 2 Ready replicas on different nodes | `g3/mcp-failover.yaml`, `mcp-gateway-failover.yaml` | `kubectl get deploy,pods -o wide -n ewallet-mcp`; `-n ewallet-agents mcp-gateway` | 2/2 each, distinct nodes, PDB `minAvailable 1` | `AGR-01_mcp_replicas.png` | `kubectl` output | 18 | single replica (F16) |
| AGR-02 | Pod loss does not fail read-only calls | `mcp_failover.py`, `mcp_experiment.sh` | the experiment, twice per arm | candidate: 0 failed calls, retries > 0; baseline shows failures; production unchanged | `AGR-02_mcp_failover_run.png` | `summary.json`, `production-before/after.txt` | 18 | images (CP3) |
| AGR-03 | Rolling update without failed calls | Deployment strategy | `kubectl rollout restart deploy/drift-mcp` during a 60 s typed loop | 0 failed calls; new ReplicaSet Ready | `AGR-03_mcp_rolling_update.png` | loop log, rollout history | 18 | CP6 |
| AGR-04 | ScaledObjects Ready and owning HPAs | `autoscaling.yaml` (keda) | `kubectl get scaledobject,hpa -n ewallet-agents` | Ready True, no fallback active, HPA owner `ScaledObject/<agent>` | `AGR-04_keda_scaledobjects.png` | YAML of both | 19, 25 | gauge images (F15) |
| AGR-05 | The trigger metric is real | `agent_metrics.py` | Prometheus `sum by (agent) (ewallet_agent_inflight_tasks)`; `kubectl get --raw /apis/external.metrics.k8s.io/v1beta1/namespaces/ewallet-agents/s0-prometheus?labelSelector=...` | Series per pod; the external metric equals the PromQL value | `AGR-05_inflight_metric.png` | query JSON | 19, 25, 51 | same |
| AGR-06 | Scale-out with serving replicas, then scale-in | HPA behaviour | load driver (≤ 8 concurrent typed tasks); sampler every 5 s | desired and Ready reach 3; the new pod completes tasks; back to 2 after the window; 0 task failures except recorded drains | `AGR-06a_scale_out.png`, `AGR-06b_scale_in.png`, Grafana | `replicas.jsonl`, HPA events, driver results | 19, 25 | S6, S8 |
| AGR-07 | Workflows keep working through scaling | AGI | AGI-09–11 during or after AGR-06 | completed, `status complete` | `AGR-07_workflow_after_scaling.png` | audit logs | 22, 25 | as above |
| AGS-01 | Business Specialists run as SandboxAgents on one shared warm pool | S3 | `kubectl get workerpool,sandboxagent,actortemplate -n ewallet-sandbox`; `GET /api/substrate/status?namespace=ewallet-sandbox` | 2 SandboxAgents Ready, both templates with `workerSelector kagent.dev/worker-pool=business-agents`, golden snapshots | `AGS-01_shared_workerpool.png` | status JSON | 20, 27 | CP4 |
| AGS-02 | Isolation | gVisor, namespace identity | worker pod `runtimeClass`/ateom; `kubectl auth can-i --list` as the sandbox SA; denied egress to an unlisted host | gVisor sandbox; no RBAC; egress denied | `AGS-02_sandbox_isolation.png` | outputs | 20 | CP4 |
| AGS-03 | Authorized calls succeed, unauthorized are denied | sandbox tokens | sandbox Specialist task; `agi_probe.py mcp --expect denied` with a sandbox token on an ungranted tool | real tool evidence; probe PASS | `AGS-03_sandbox_mcp_policy.png` | probe JSON, task artifact | 20, 23 | dedicated tokens |
| AGS-04 | Many sessions and agents on few workers | F7, F8 | N = 12 sessions over both Specialists against 2 workers; snapshot `/api/substrate/status` every 2 s | ≥ 2 distinct actors served by the same worker; peak running actors ≤ workers; all sessions complete | `AGS-04_actor_worker_assignment.png` | status time series | 27 | CP4 |
| AGS-05 | Cold against warm start | `sandbox_experiment.sh` | new session (create from golden) against resumed session; Deployment arm as reference | medians with n ≥ 5 each; no claim beyond the measured numbers | `AGS-05_cold_warm_latency.png` | timing CSV | 27 | CP4 |
| AGS-06 | Utilization and cost | Grafana CPU/memory, worker requests | workers × requests against the Deployment arm at equal load | reported as measured | `AGS-06_utilization_cost.png` | metrics export | 27 | CP4 |
| AGS-07 | KEDA scales the shared pool (B′) | S4, S5 | load on sandbox Specialists; sampler on `workerpool/scale` and the HPA | HPA `ScalingActive=True`; workers 1→2(→3)→1; sessions keep completing | `AGS-07_workerpool_keda_scaling.png` | `replicas.jsonl`, HPA events | 19, 27 | F1–F4: B′ patch or fallback |

## 10. Owner decisions

1. Approve trying B′ (a cluster-scoped patch of the Helm-managed WorkerPool CRD), or
   go straight to fallback A+S.
2. Approve the KEDA `watchNamespace` change (adds `ewallet-sandbox`).
3. Approve the dedicated sandbox MCP credentials. They are a production token-table
   change on both MCP servers, so do CP6 (2 replicas) first to avoid a gap.
4. Approve the snapshot bucket and its cleanup (snapshots hold tokens).
5. Approve the Coordinator's sandbox route as the delegated path for the evidence
   window, and its rollback.
6. Approve the load budget for AGR-06, AGS-04 and AGS-07 on the shared vLLM.
7. Approve the wake profile and node count for the campaign.

## Erratum (2026-11-15, runtime)

This audit originally gave the SandboxAgent A2A path as `/api/a2a/<ns>/<agent>/`. kagent
0.10.3 registers SandboxAgents under `/api/a2a-sandboxes/<ns>/<name>`, with handler key
`sandboxes/<ns>/<name>` (`go/core/internal/a2a/a2a_handler_mux.go` and `httpserver/server.go`).
The old path returned `404 Agent ewallet-sandbox/<agent> not found` on GKE. The paths
above, the Coordinator and the A2A Gateway routes now use `/api/a2a-sandboxes/`.
