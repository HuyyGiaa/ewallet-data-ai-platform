# Agent Platform: deployment and operations

This page covers how the Agent Platform is deployed on GKE and how it is run: autoscaling, actor lifecycle, MCP availability, gateway policies, observability, the commands the owner used and the guarded sleep/wake cycle. Every statement links to the Helm values, manifest or script that implements it.

- **Architecture:** [AGENT_PLATFORM_ARCHITECTURE.md](AGENT_PLATFORM_ARCHITECTURE.md).
- **Measured results:** [AGENT_PLATFORM_EVIDENCE.md](AGENT_PLATFORM_EVIDENCE.md).

## Contents

1. [Deployment layout on GKE](#1-deployment-layout-on-gke)
2. [Helm releases and Agent configuration](#2-helm-releases-and-agent-configuration)
3. [Namespaces and Kubernetes resources](#3-namespaces-and-kubernetes-resources)
4. [KEDA for the Fraud Coordinator](#4-keda-for-the-fraud-coordinator)
5. [KEDA for the shared WorkerPool](#5-keda-for-the-shared-workerpool)
6. [HPA, ScaledObject and Prometheus metrics](#6-hpa-scaledobject-and-prometheus-metrics)
7. [Actor lifecycle: Running, Suspended, Resume](#7-actor-lifecycle-running-suspended-resume)
8. [How a new worker takes Sandbox actors](#8-how-a-new-worker-takes-sandbox-actors)
9. [MCP multi-replica](#9-mcp-multi-replica)
10. [RollingUpdate and PodDisruptionBudget](#10-rollingupdate-and-poddisruptionbudget)
11. [Gateway security policies](#11-gateway-security-policies)
12. [Prometheus, Grafana, Tempo and Loki](#12-prometheus-grafana-tempo-and-loki)
13. [Operational check commands](#13-operational-check-commands)
14. [Guarded sleep and wake](#14-guarded-sleep-and-wake)
15. [Observed operational limitations](#15-observed-operational-limitations)

## 1. Deployment layout on GKE

| Item | Value |
|---|---|
| Project, cluster, zone | `ewallet-data-ai-platform-hg26`, `ewallet-dev`, `asia-southeast1-b` |
| Node pool | `cpu-pool`, 5 nodes while awake (GKE `v1.36.4-gke.2377000`), 0 nodes while asleep |
| Control-plane access | IAM DNS endpoint kubeconfig (`~/.cache/ewallet-cloud-lab/kubeconfig`); no extra authorized network is added |
| Mesh | Istio ambient (ztunnel L4 identity, AuthorizationPolicies) |
| Agent control plane | kagent 0.10.3 (namespace `kagent`) |
| Warm worker fleet | Agent Substrate `ate.dev` v0.0.9 with the project egress patch ([architecture §8](AGENT_PLATFORM_ARCHITECTURE.md#8-agent-substrate-and-the-shared-workerpool)) |
| Autoscaler | KEDA ScaledObjects that own HPAs |

The live workload list in the GKE console shows these Deployments, with a Status column of OK and the pod counts below ([evidence §2](AGENT_PLATFORM_EVIDENCE.md#2-gke-workloads-and-platform-status)):

- **Agents:** `fraud-coordinator` 2/2 and `business-agents-deployment` (the WorkerPool workers) 2/2.
- **MCP servers:** `drift-mcp` 2/2 and `feature-rag-mcp` 2/2.
- **Gateways:** `a2a-gateway`, `mcp-gateway` and `agentgateway`, 1/1 each.
- **Observability:** `prometheus`, `grafana`, `tempo`, `loki`, `otel-collector` and `observability-alloy`.
- **LLM runtime:** `vllm-primary` 1/1 and `vllm-fallback` 0/0.

## 2. Helm releases and Agent configuration

| Release (namespace) | Revision at the replay | Chart | Deploys |
|---|---|---|---|
| `llm-agent` (`default`) | **60** (2026-11-19 09:00:45Z) | [`infra/helm/llm-agent-platform`](../../../infra/helm/llm-agent-platform/) | Coordinator Agent, A2A/MCP/LLM gateways, ModelConfig, Coordinator KEDA, network policies, observability stack, vLLM, idempotency Redis, into `ewallet-agents` |
| `llm-agent-mcp` (`ewallet-mcp`) | **19** (2026-11-17 01:29:12Z) | the same chart, MCP profile | `feature-rag-mcp` and `drift-mcp` (2 replicas each, PDBs) |

The revisions come from `helm history`, printed by the replay status check ([evidence §2](AGENT_PLATFORM_EVIDENCE.md#2-gke-workloads-and-platform-status)).

The release is built from the chart [`values.yaml`](../../../infra/helm/llm-agent-platform/values.yaml), [`values-gke-cpu.yaml`](../../../infra/helm/llm-agent-platform/values-gke-cpu.yaml) and these profile overlays:

- [`g3/g3.yaml`](../../../infra/helm/llm-agent-platform/g3/g3.yaml): BYO Coordinator, ModelConfig, idempotency Redis.
- [`g3/keda.yaml`](../../../infra/helm/llm-agent-platform/g3/keda.yaml) and [`agi/keda.yaml`](../../../infra/helm/llm-agent-platform/agi/keda.yaml): KEDA as the autoscaling engine with in-flight triggers.
- [`agi/agents.yaml`](../../../infra/helm/llm-agent-platform/agi/agents.yaml): the AGI agent profile and the BYO ModelConfig resolver.
- [`agi/specialists-sandbox-only.yaml`](../../../infra/helm/llm-agent-platform/agi/specialists-sandbox-only.yaml): Specialists as `runtime: sandbox` only.
- [`agi/mcp.yaml`](../../../infra/helm/llm-agent-platform/agi/mcp.yaml) and [`agi/mcp-reliability.yaml`](../../../infra/helm/llm-agent-platform/agi/mcp-reliability.yaml): MCP release with 2 replicas, topology spread and PDB.

The Coordinator settings that matter at runtime were read back with `helm get values` during the replay:

```yaml
agents:
  coordinator:
    byo:
      enabled: true
      naturalLanguageIntake: {enabled: true}
      sandboxSpecialists: [drift-specialist, feature-rag-specialist]
      workflows: [fraud, drift]
  specialists:
    drift-specialist: {runtime: sandbox}
    feature-rag-specialist: {runtime: sandbox}
  autoscaling:
    enabled: true
    engine: keda
    keda:
      transferHpa: true
      triggers:
        fraud-coordinator: {type: inflight, inflightPerReplica: "2"}
a2aGateway:
  requestTimeout: 660s
  sandboxRoutes: {agents: [drift-specialist, feature-rag-specialist], namespace: ewallet-sandbox}
otelCollector:
  metricExpiration: 60s
```

The Sandbox fleet is **not** part of a Helm release. The SandboxAgents, the WorkerPool and the WorkerPool ScaledObject are plain manifests applied through approval-gated scripts, so that KEDA stays the only owner of `WorkerPool.spec.replicas`:

- [`infra/kubernetes/agent-sandbox/namespace.yaml`](../../../infra/kubernetes/agent-sandbox/namespace.yaml)
- [`fleet.yaml.tmpl`](../../../infra/kubernetes/agent-sandbox/fleet.yaml.tmpl), rendered with the image digest by `scripts/experiments/render_sandbox.py`
- [`workerpool-scaledobject.yaml`](../../../infra/kubernetes/agent-sandbox/workerpool-scaledobject.yaml)

See the manifest header in [`namespace.yaml` L36–L40](../../../infra/kubernetes/agent-sandbox/namespace.yaml#L36-L40).

## 3. Namespaces and Kubernetes resources

| Namespace | Resources |
|---|---|
| `ewallet-agents` | kagent `Agent` `fraud-coordinator` (type BYO) and its Deployment; ScaledObject and HPA `fraud-coordinator`; PDB `fraud-coordinator`; StatefulSet `coordinator-idempotency-redis`; Deployments `a2a-gateway`, `mcp-gateway`, `agentgateway`; `ModelConfig ewallet-global-llm`; observability Deployments; default-deny `AuthorizationPolicy` with per-flow ALLOW policies and NetworkPolicies |
| `ewallet-sandbox` | `SandboxAgent` `feature-rag-specialist` and `drift-specialist`; `WorkerPool business-agents` (worker Deployment `business-agents-deployment`); one gVisor `ActorTemplate` per SandboxAgent revision; ScaledObject and HPA `business-agents-workerpool`; ServiceAccounts `default`, `sandbox-feature-rag` and `sandbox-drift`, all with token automount disabled |
| `ewallet-mcp` | Deployments `feature-rag-mcp` and `drift-mcp` (2 each), PDBs, Services; `default-deny` plus `allow-mcp-gateway-to-*` AuthorizationPolicies; NetworkPolicies for ingress from the MCP Gateway and egress to Feast Redis, KServe and OTel |
| `kagent` | kagent controller (A2A and SandboxAgent routes, task store) and kagent UI |
| `ewallet-prompts` | Agent Registry (`prompt-registry-agentregistry`) holding the Agent, MCPServer and Prompt catalog |

## 4. KEDA for the Fraud Coordinator

The ScaledObject is rendered by [`templates/autoscaling.yaml` L80–L148](../../../infra/helm/llm-agent-platform/templates/autoscaling.yaml#L80-L148) with these settings:

| Setting | Value | Source |
|---|---|---|
| Target | `Deployment/fraud-coordinator` | — |
| Min / max replicas | 2 / 3 | [`values.yaml` L444–L445](../../../infra/helm/llm-agent-platform/values.yaml#L444-L445) |
| Trigger | Prometheus `sum(ewallet_agent_inflight_tasks{agent="fraud-coordinator"}) or vector(0)`, `AverageValue`, threshold **2** in-flight tasks per replica | [`values.yaml` L428–L429](../../../infra/helm/llm-agent-platform/values.yaml#L428-L429), [`agi/keda.yaml` L15](../../../infra/helm/llm-agent-platform/agi/keda.yaml#L15) |
| Polling / cooldown | 15 s / 120 s | [`values.yaml` L430–L431](../../../infra/helm/llm-agent-platform/values.yaml#L430-L431) |
| Fallback | after 3 failed reads, hold 2 replicas | [`values.yaml` L433](../../../infra/helm/llm-agent-platform/values.yaml#L433) |
| HPA behaviour | scale-up: no stabilization, +1 pod per 60 s; scale-down: 300 s window, −1 pod per 60 s | [`values.yaml` L446–L453](../../../infra/helm/llm-agent-platform/values.yaml#L446-L453) |
| HPA adoption | `transferHpa: true` (KEDA adopts the existing HPA in place, without an autoscaling gap) | [`autoscaling.yaml` L84–L88](../../../infra/helm/llm-agent-platform/templates/autoscaling.yaml#L84-L88) |

**Why in-flight tasks and not CPU:** the Coordinator spends its time waiting on A2A calls, so its CPU barely moves under load (values comment, [L424–L426](../../../infra/helm/llm-agent-platform/values.yaml#L424-L426)). G5 confirmed this: at peak load the three Coordinator pods used 20m, 223m and 21m CPU.

The gauge is the up-down counter `agent_inflight_tasks` ([`agent_metrics.py` L67](../../../ai_platform/llm_agent/agent_metrics.py#L67)). [`track_inflight`](../../../ai_platform/llm_agent/agent_metrics.py#L168) increments it for each A2A task in execution.

## 5. KEDA for the shared WorkerPool

The ScaledObject is defined in [`infra/kubernetes/agent-sandbox/workerpool-scaledobject.yaml`](../../../infra/kubernetes/agent-sandbox/workerpool-scaledobject.yaml):

```yaml
spec:
  scaleTargetRef: {apiVersion: ate.dev/v1alpha1, kind: WorkerPool, name: business-agents}
  minReplicaCount: 2
  maxReplicaCount: 3
  pollingInterval: 15
  cooldownPeriod: 300
  fallback: {failureThreshold: 3, replicas: 2, behavior: currentReplicasIfHigher}
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:   {stabilizationWindowSeconds: 0,   policies: [{type: Pods, value: 1, periodSeconds: 30}]}
        scaleDown: {stabilizationWindowSeconds: 300, policies: [{type: Pods, value: 1, periodSeconds: 60}]}
  triggers:
    - type: prometheus
      metricType: AverageValue
      metadata:
        query: sum(ewallet_sandbox_delegations_inflight) or vector(0)
        threshold: "1"
```

- **Signal.** `ewallet_sandbox_delegations_inflight` is emitted by the **Coordinator**, not by the workers. It counts one unit per logical Sandbox delegation, from the first send until the delegation ends, including any time spent waiting for a free worker ([`_send`](../../../ai_platform/llm_agent/programmatic_coordinator.py#L665), [`track_sandbox_delegation`](../../../ai_platform/llm_agent/agent_metrics.py#L180)). A worker runs one actor at a time, so the number of workers needed equals the delegations in flight, hence threshold 1 (manifest comment, [L7–L11](../../../infra/kubernetes/agent-sandbox/workerpool-scaledobject.yaml#L7-L11)).
- **Fallback.** With `currentReplicasIfHigher`, a metric outage never shrinks a busy pool ([L29–L34](../../../infra/kubernetes/agent-sandbox/workerpool-scaledobject.yaml#L29-L34)).
- **Scale target.** Scaling the custom resource works because the pool exposes `scaleSelector: ate.dev/worker-pool=business-agents` and the WorkerPool CRD has a `/scale` subresource ([`namespace.yaml` L41–L42](../../../infra/kubernetes/agent-sandbox/namespace.yaml#L41-L42)).

## 6. HPA, ScaledObject and Prometheus metrics

```text
Coordinator process ──OTLP──> otel-collector ──prometheus exporter (namespace "ewallet")──> Prometheus
   agent_inflight_tasks{agent}            → ewallet_agent_inflight_tasks           ← KEDA (Coordinator)
   sandbox_delegations_inflight{specialist} → ewallet_sandbox_delegations_inflight  ← KEDA (WorkerPool)
KEDA ScaledObject ──owns──> HPA ──/scale──> Deployment fraud-coordinator | WorkerPool business-agents
```

- **Metric names.** The collector's Prometheus exporter adds the `ewallet_` prefix ([`otel-collector.yaml` L129–L138](../../../infra/helm/llm-agent-platform/templates/otel-collector.yaml#L129-L138)).
- **Stale series.** The exporter drops a series 60 s after its last update (`metric_expiration`, value `otelCollector.metricExpiration: 60s` in [`agi/keda.yaml` L21–L22](../../../infra/helm/llm-agent-platform/agi/keda.yaml#L21-L22)). Without this, a terminated pod's last gauge value would keep counting toward KEDA load for the exporter's default 5 minutes.
- **HPA ownership.** Each ScaledObject owns an HPA with the same name:
  - `fraud-coordinator` (target `0/2 (avg)` at rest);
  - `business-agents-workerpool` (target `0/1 (avg)` at rest).

  This is visible in the G5 baseline screenshot ([evidence §9](AGENT_PLATFORM_EVIDENCE.md#9-keda-autoscaling-g5)).

## 7. Actor lifecycle: Running, Suspended, Resume

The lifecycle of one Specialist session is set by kagent 0.10.3 SandboxAgents on Substrate, as described in the fleet manifest header ([`fleet.yaml.tmpl` L1–L5](../../../infra/kubernetes/agent-sandbox/fleet.yaml.tmpl#L1-L5)):

1. The Coordinator sends a turn with `contextId = request_id` to `/api/a2a-sandboxes/ewallet-sandbox/<agent>/` ([`GatewayPolicy.call_for`](../../../ai_platform/llm_agent/programmatic_coordinator.py#L271)).
2. kagent selects the actor for that session:
   - **new session:** a new actor is activated from the agent's gVisor `ActorTemplate`;
   - **known session:** the suspended actor is **resumed** from its snapshot.
3. The actor runs on a free worker of `business-agents` (state **Running**). It executes the Specialist module and calls the MCP Gateway.
4. After the response, the actor is **Suspended**: its state is checkpointed to the snapshot location `gs://ate-snapshots/ewallet-sandbox/<campaign>/<agent>/` ([fleet L70–L71](../../../infra/kubernetes/agent-sandbox/fleet.yaml.tmpl#L70-L71)), which frees the worker for another actor.

The G6 replay observed this lifecycle on the live pool ([evidence §10](AGENT_PLATFORM_EVIDENCE.md#10-shared-workerpool-g6)):

- 5 actors were seen both Running and Suspended.
- Both new and resumed sessions were measured.
- The peak was 1 running session actor per 2 worker pods.

## 8. How a new worker takes Sandbox actors

When KEDA raises `WorkerPool.spec.replicas` from 2 to 3, Substrate creates one more worker pod in `business-agents-deployment`. As soon as it is Ready, the next actor activations can be placed on it. Nothing restarts and no agent is redeployed: the SandboxAgents point at the pool, not at specific pods.

In G5 ([evidence §9](AGENT_PLATFORM_EVIDENCE.md#9-keda-autoscaling-g5)):

- **Startup:** the added worker `business-agents-deployment-7c4b97b9f4-np6fz` went from created to Ready in 3 s. It held its first `feature-rag-specialist` actor at 19:24:11Z.
- **Placement:** by the end of the run it had been assigned 47 of the run's actors, and 3 were seen Running on it.
- **Scale-in:** the pool went back to 2 workers at 19:41:18Z, after the load had ended (19:36:19Z).

The WorkerPool manifest's documented intent is that a removed worker drains, so ateom finishes the active turn and its checkpoint first ([`workerpool-scaledobject.yaml` L45–L47](../../../infra/kubernetes/agent-sandbox/workerpool-scaledobject.yaml#L45-L47)). This campaign did not test scale-in while actors were running.

## 9. MCP multi-replica

[`agi/mcp-reliability.yaml`](../../../infra/helm/llm-agent-platform/agi/mcp-reliability.yaml) sets, for the `llm-agent-mcp` release:

```yaml
mcpServices:
  replicas: 2
  topologySpread: true
  services:
    feature-rag-mcp:
      pdb: {enabled: true}
```

- **Rendering.** [`templates/mcp-services.yaml`](../../../infra/helm/llm-agent-platform/templates/mcp-services.yaml) renders each server as a Deployment with that replica count and `topologySpreadConstraints` across nodes ([L85–L86](../../../infra/helm/llm-agent-platform/templates/mcp-services.yaml#L85-L86)).
- **Statelessness.** The MCP servers are stateless: JSON responses only, no server sessions ([`POST /mcp`](../../../ai_platform/llm_agent/tools/service.py#L246)). Any replica can serve any request.
- **Client failover.** The Specialists' failover policy opens a new session for each attempt, so a retry after a transport failure can reach the other replica ([architecture §5](AGENT_PLATFORM_ARCHITECTURE.md#5-feature-rag-specialist)).

## 10. RollingUpdate and PodDisruptionBudget

| Object | Setting | Source |
|---|---|---|
| MCP Deployments | `strategy: RollingUpdate`, `maxUnavailable: 0`, `maxSurge: 1` | [`mcp-services.yaml` L74](../../../infra/helm/llm-agent-platform/templates/mcp-services.yaml#L74) |
| MCP PDBs | `minAvailable: 1` for `feature-rag-mcp` and `drift-mcp` | [`mcp-services.yaml` L215–L222](../../../infra/helm/llm-agent-platform/templates/mcp-services.yaml#L215-L222) |
| Coordinator PDB | `minAvailable: 1` | [`autoscaling.yaml` L193–L201](../../../infra/helm/llm-agent-platform/templates/autoscaling.yaml#L193-L201) |
| Gateway Deployments | `RollingUpdate`, `maxUnavailable: 0`, `maxSurge: 1` | [`agentgateway.yaml` L198](../../../infra/helm/llm-agent-platform/templates/agentgateway.yaml#L198) |

What these settings do:

- **During an image or config roll:** with `maxUnavailable: 0` and `maxSurge: 1`, a new pod must be Ready before an old one is removed.
- **During a voluntary eviction** such as a node drain: the PDB keeps at least one replica of each MCP server running.

The live values were read back before the final sleep: both MCP Deployments and the Coordinator had `{'maxSurge': 1, 'maxUnavailable': 0}`, 2/2 ready, and PDBs `minAvailable 1` with `ALLOWED DISRUPTIONS 1`. See [`final-sleep-mcp-coordinator-strategy-pdb.txt`](../../evidence/edai2/agents/replay-20261118T161231Z/artifacts/final-sleep-mcp-coordinator-strategy-pdb.txt).

This replay verified the **configuration** only. It did not roll the MCP Deployments under load. The evidence page separates configuration from the one failover experiment recorded earlier ([evidence §14](AGENT_PLATFORM_EVIDENCE.md#14-mcp-availability-and-rollingupdate)).

## 11. Gateway security policies

All three gateways are agentgateway instances. Their routes are generated from [`templates/policy-gateways.yaml`](../../../infra/helm/llm-agent-platform/templates/policy-gateways.yaml).

| Gateway | Policy | Why |
|---|---|---|
| A2A Gateway (`a2a-gateway:8083`) | One exact route per SandboxAgent, `/api/a2a-sandboxes/ewallet-sandbox/<agent>/` ([L219–L256](../../../infra/helm/llm-agent-platform/templates/policy-gateways.yaml#L219-L256)); no wildcard forwarding | The Coordinator can reach only the declared Specialists; an undeclared path returns 404 (negative test) |
| | `x-user-id` set by the gateway; client `authorization`, `x-ewallet-*`, `x-kagent-host`, `x-user-id` removed | A client cannot choose the principal that kagent attributes the session to |
| | Ingress on 8083 admitted only from `cluster.local/ns/ewallet-agents/sa/fraud-coordinator`: the network flow `fraud-coordinator-to-a2a-gateway` ([`g3/g3.yaml` L156](../../../infra/helm/llm-agent-platform/g3/g3.yaml#L156)), rendered as an ALLOW AuthorizationPolicy plus NetworkPolicy by [`security.yaml`](../../../infra/helm/llm-agent-platform/templates/security.yaml#L26-L47) | Only the Coordinator may delegate |
| | `requestTimeout: 660s` (live value) and a local token-bucket rate limit | Bounds a slow Sandbox turn and sheds bursts (HTTP 429), which the Coordinator backs off on |
| MCP Gateway (`mcp-gateway:8080`) | One route per MCP server, `/feature-rag/mcp` and `/drift/mcp` ([L101–L141](../../../infra/helm/llm-agent-platform/templates/policy-gateways.yaml#L101-L141)) | Specialists never connect to MCP servers directly |
| | Mode `ambient-backend-token`: ztunnel admits only the Sandbox worker identity `ewallet-sandbox/sa/default` (and kagent-controller for discovery); the MCP server's service-token subject and tool grants decide which tools are visible and callable | Identity enforced at L4 by the mesh, tool authorization at the server, which sees the credential |
| | Client `authorization` and identity headers removed; per-route timeout and rate limit | No header spoofing; bounded load on the MCP servers |
| LLM route (`agentgateway`) | Only reachable address for the ModelConfig base URL ([`validate_base_url`](../../../ai_platform/llm_agent/model_config.py#L85)); `requestTimeout: 180s` on CPU ([`values-gke-cpu.yaml` L58–L62](../../../infra/helm/llm-agent-platform/values-gke-cpu.yaml#L58-L62)) | Agents never call a model server or an external host directly |

Underneath the gateways, the agents namespace is **deny by default** at both Istio L4 and Kubernetes L3/L4. Each declared flow opens one source identity to one port ([`templates/security.yaml`](../../../infra/helm/llm-agent-platform/templates/security.yaml)).

## 12. Prometheus, Grafana, Tempo and Loki

| Signal | Path | Source |
|---|---|---|
| Metrics | Agents, MCP servers and gateways send OTLP to `otel-collector` (gRPC 4317); its Prometheus exporter is scraped by Prometheus. A `spanmetrics` connector turns spans into call, error and latency counts per `gen_ai.agent.name` and `gen_ai.tool.name` | [`otel-collector.yaml`](../../../infra/helm/llm-agent-platform/templates/otel-collector.yaml) |
| Traces | OTLP to `otel-collector`, then to Tempo. Sandbox actors export to `otel-collector.ewallet-agents.svc.cluster.local:4317` (allowed domain) | [`otel-collector.yaml` L142–L146](../../../infra/helm/llm-agent-platform/templates/otel-collector.yaml#L142-L146), [fleet L56–L60](../../../infra/kubernetes/agent-sandbox/fleet.yaml.tmpl#L56-L60) |
| Logs | `observability-alloy` tails pod logs (`loki.source.kubernetes`) and pushes them to Loki. Gateways log JSON with `trace.id` | [`pod-logs.yaml`](../../../infra/helm/llm-agent-platform/templates/pod-logs.yaml) |
| Stack | Prometheus, Loki, Tempo, Grafana Deployments; Grafana datasources with Tempo → Loki trace-to-logs links | [`observability-stack.yaml`](../../../infra/helm/llm-agent-platform/templates/observability-stack.yaml#L166-L186) |
| Dashboard | "E-Wallet LLM + Agent Platform": API rate and failures by route, MCP tool calls, LLM latency, tokens and TTFT, **Agent calls by agent (spanmetrics)**, **Agent/tool failures (spanmetrics)**, CPU and memory by pod | [`dashboards/llm-agent.json`](../../../infra/helm/llm-agent-platform/dashboards/llm-agent.json) |

Agent metrics emitted by the BYO runtime ([`agent_metrics.py` L57–L91](../../../ai_platform/llm_agent/agent_metrics.py#L57-L91)):

- `agent_invocations_total`
- `agent_inflight_tasks`
- `mcp_client_retries_total`
- `sandbox_delegations_inflight`
- `sandbox_admission_waits_total`
- the LLM prompt screening counters

## 13. Operational check commands

All campaign checks go through [`scripts/llm_agent/agent_replay.sh`](../../../scripts/llm_agent/agent_replay.sh) ([dispatch L495–L506](../../../scripts/llm_agent/agent_replay.sh#L495-L506)):

- **Inputs:** `AGI_ENV` (local env file, never committed) and one `REPLAY_DIR` per campaign.
- **Output:** each command saves its raw output and a `verdict.json` judged by [`ops/llm_agent/replay_verdict.py`](../../../ops/llm_agent/replay_verdict.py).

| Command | What it does | Mutates? |
|---|---|---|
| `agent_replay.sh status` | Helm revisions, Coordinator/gateway readiness, Agent/SandboxAgent/WorkerPool state, MCP replicas/strategy/PDB, both ScaledObjects/HPAs, retired workloads absent ([L134](../../../scripts/llm_agent/agent_replay.sh#L134)) | no |
| `agent_replay.sh keda` | Both ScaledObjects Ready/not paused, HPA owned, replica counts, recent rescale events ([L318](../../../scripts/llm_agent/agent_replay.sh#L318)) | no |
| `agent_replay.sh pf` / `stop-pf` | Starts and stops the six local port-forwards (A2A 18083, MCP 18082, kagent controller 18383, kagent UI 18300, Grafana 13000, registry 18121). `stop-pf` kills only its own `kubectl port-forward` processes ([L89](../../../scripts/llm_agent/agent_replay.sh#L89), [L117](../../../scripts/llm_agent/agent_replay.sh#L117)) | local only |
| `agent_replay.sh e2e fraud\|drift\|combined` | One typed request through the Coordinator, plus hop checks: Coordinator log, A2A route, MCP `actor_id`, retired Deployments absent ([L175](../../../scripts/llm_agent/agent_replay.sh#L175)) | one request |
| `agent_replay.sh negative` | Security negatives, each with a passing control ([L255](../../../scripts/llm_agent/agent_replay.sh#L255)) | probes only |
| `agent_replay.sh registry` | Agent Registry strict readback (GET only) ([L294](../../../scripts/llm_agent/agent_replay.sh#L294)) | no |
| `REPLAY_AUTOSCALE_APPROVED=yes agent_replay.sh autoscale` | G5: 100 typed tasks, concurrency 8, at most 900 s, sampling replicas, actors and `kubectl top` ([L394](../../../scripts/llm_agent/agent_replay.sh#L394)). One run per `REPLAY_DIR` | load (owner-run) |
| `REPLAY_SESSIONS_APPROVED=yes agent_replay.sh workers` | G6: sequential new and resumed sessions of both SandboxAgents on the shared pool ([L446](../../../scripts/llm_agent/agent_replay.sh#L446)) | sessions (owner-run) |

Direct `kubectl` reads used for the screenshots:

```bash
kubectl -n ewallet-sandbox get sandboxagents.kagent.dev,workerpools.ate.dev,actortemplates.ate.dev -o wide
kubectl -n ewallet-mcp get deployments,poddisruptionbudgets -o wide
kubectl -n ewallet-mcp get networkpolicy,authorizationpolicy
kubectl auth can-i get secrets --as=system:serviceaccount:ewallet-sandbox:default -n ewallet-sandbox   # no
kubectl -n ewallet-sandbox get sa default -o jsonpath='{.automountServiceAccountToken}'                # false
```

## 14. Guarded sleep and wake

The cluster is put to sleep (0 nodes) between work sessions with [`ops/gcp/cloud_lab.py`](../../../ops/gcp/cloud_lab.py).

**Read-only commands:**

- `status` ([L453](../../../ops/gcp/cloud_lab.py#L453))
- `plan-sleep` ([L461](../../../ops/gcp/cloud_lab.py#L461))
- `plan-wake` ([L715](../../../ops/gcp/cloud_lab.py#L715))
- `verify --expect sleep|awake` ([L796](../../../ops/gcp/cloud_lab.py#L796))
- `record-manifest` ([L708](../../../ops/gcp/cloud_lab.py#L708))

**Mutating commands:**

- `sleep` ([L493](../../../ops/gcp/cloud_lab.py#L493))
- `wake-full` ([L727](../../../ops/gcp/cloud_lab.py#L727))

Each mutating command needs all of:

- `--execute`;
- `--confirm-cluster ewallet-dev`;
- `CLOUD_LAB_APPROVED=yes`.

A guarded sleep also needs `CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:<campaign>` and `CLOUD_LAB_KEDA_PAUSE_APPROVAL=ewallet-dev:<campaign>`.

**Why the sleep is "guarded".** The MCP and Coordinator PDBs block a plain node drain, so the sleep runs in this order:

1. `record-manifest` saves the declared replicas: the **recovery manifest**, used by the next wake and by `verify`.
2. `plan-sleep` lists the PDB blockers. Every blocker must be covered by the approved PDB record (`docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z`) or named explicitly with `--also-approve`. In this campaign the only uncovered PDB was `ewallet-mcp/feature-rag-mcp` (Helm-owned, `minAvailable 1`).
3. `sleep --scale-pdb-guarded --pause-keda`:
   - pauses both ScaledObjects with `autoscaling.keda.sh/paused-replicas` and the `ewallet.io/cloud-lab-paused-by=<campaign>` marker;
   - scales `fraud-coordinator`, `drift-mcp` and `feature-rag-mcp` to 0;
   - resizes `cpu-pool` to 0.
4. `verify --expect sleep` checks the result against the recovery manifest.

`wake-full --manifest <recovery-manifest> --nodes 5` reverses the sleep:

- it resizes the pool;
- it relabels three nodes `ewallet.io/g3-llm=true`;
- it restores the recorded replicas;
- it unpauses **only** ScaledObjects that carry the cloud-lab marker.

| Campaign (UTC) | Action | Result |
|---|---|---|
| `interim-sleep-20261119T111823Z` | Guarded sleep after the G2–G4 evidence | `verify --expect sleep` failures `[]`, pool 0 VMs |
| `g5-prep-20261119T170446Z` | `wake-full --nodes 5` from the interim manifest; exit 0 at 17:10:20Z | `verify --expect awake` failures `[]`; `status` and `keda` PASS; 6/6 port-forwards |
| `final-sleep-20261119T212026Z` | Fresh recovery manifest (80 workloads), `plan-sleep` = `SAFE_AFTER_REVERSIBLE_PREPARATION`, dry run, guarded sleep exit 0 at 21:25:03Z | `verify --expect sleep` failures `[]`; 0 VMs; master authorized networks unchanged (3 CIDRs) |

The plan and verification files of the final sleep are copied under [`artifacts/`](../../evidence/edai2/agents/replay-20261118T161231Z/artifacts/):

- `final-sleep-plan-sleep.json`
- `final-sleep-verify-sleep.json`

**After a wake:** expect about 25 minutes of cold start. Some pods restart a few times on DNS-race CrashLoops (kagent controller, MCP, Coordinator) and recover without intervention. The status check reports these restarts as a WARN.

## 15. Observed operational limitations

These limitations were observed during the campaign, or follow directly from the deployed configuration:

1. **Throughput is bounded by the WorkerPool size.**
   - A worker runs one actor at a time (G6: peak 1 running session actor per 2 workers), and the pool maximum is 3.
   - At concurrency 8, G5 completed 100/100 tasks at **0.131 task/s**, with an end-to-end p50 of 43.5 s and p95 of 148.4 s, mostly time spent waiting for a free worker.
   - Raising `maxReplicaCount` would need more node capacity.
2. **The gateways run one replica each.** `a2a-gateway`, `mcp-gateway` and `agentgateway` are 1/1 in the live release, and their failover was not tested.
3. **Observability storage is ephemeral.** Prometheus, Loki and Tempo write to an `emptyDir` scratch volume ([`observability-stack.yaml` L284–L286](../../../infra/helm/llm-agent-platform/templates/observability-stack.yaml#L284-L286)), so metrics, logs and traces do not survive a pod restart or a sleep/wake cycle. Screenshots and the saved replay files are the durable record.
4. **Cold start after wake.** About 25 minutes, with self-healing DNS-race restarts. `vllm-primary` warms slowly on CPU; only natural-language intake depends on it, typed requests do not.
5. **ModelConfig changes require a Coordinator restart.** The BYO Fraud Coordinator resolves `ewallet-global-llm` at start-up only ([`model_config.py` docstring](../../../ai_platform/llm_agent/model_config.py#L1-L20)), so a change to the ModelConfig takes effect after `kubectl -n ewallet-agents rollout restart deploy/fraud-coordinator`. The two Sandbox Specialists do not use the LLM and do not resolve this ModelConfig.
6. **HTTP 405 on the MCP route.**
   - Observed: Tempo shows one error span, `GET /feature-rag/mcp` → HTTP 405, inside traces whose tool calls succeed.
   - Code fact: the MCP server registers only `POST /mcp`.
   - Status: recorded as a finding ([evidence §12](AGENT_PLATFORM_EVIDENCE.md#12-tempo-distributed-tracing)); the cause was not investigated further.
7. **The failure panel is not zero.** During the G6 session run, the Grafana panel "Agent/tool failures (spanmetrics)" showed a non-zero rate even though every one of the 24 session turns succeeded. Which spans it counts was not analysed in this campaign.
