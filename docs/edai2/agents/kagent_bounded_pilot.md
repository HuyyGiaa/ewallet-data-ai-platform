# Agent Fleet Architecture — From Sandbox Pilot to Production

This report describes how the business Specialists run as a fleet: kagent SandboxAgents on one shared, gVisor-isolated
Substrate WorkerPool. The fleet is autoscaled with the Coordinator, and it was hardened on GKE across the 2026-11-15
to 2026-11-17 campaigns. It is the second of the three Agent Platform reports:

| Report | Role |
|---|---|
| [Agent Platform](AGENTS_REGISTRY_GATEWAYS.md) | architecture, deployment, workflows, governance |
| This report | Substrate, the shared WorkerPool, sessions, placement, autoscaling, capacity, startup, cost |
| [Owner evidence replay](OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md) | how the owner re-captures the evidence |

**Labels used here:**
- **Pilot:** the 2026-11-02 mock-model pilot.
- **Measured on GKE**, with the campaign named: a recorded result on real business SandboxAgents.
- **`OWNER_EVIDENCE_PENDING`**: a measurement or screenshot that the next replay produces.

Campaign evidence is mirrored under `docs/evidence/edai2/agents/<campaign>/`. Every number below comes from
[AGENT_PLATFORM_COMPLETION.md](AGENT_PLATFORM_COMPLETION.md).

## 1. Why Substrate

A Specialist answers one investigation turn, then waits for the next. Running each Specialist as a Deployment keeps
its pods busy-idle and gives every tenant session the same process. Agent Substrate 0.0.9 adds three things:
- **Isolation per session:** each conversation (A2A `contextId`) gets its own *session actor*, run in a gVisor sandbox.
- **Density:** an idle actor is checkpointed to a snapshot and suspended, so many sessions share a few warm workers.
- **Warm start:** new actors are created from a *golden snapshot* of the agent image, not from a cold container start.

kagent 0.10.3 exposes this as the `SandboxAgent` CRD, reached at `/api/a2a-sandboxes/<namespace>/<agent>/`.

## 2. The initial bounded pilot (2026-11-02)

The pilot proved only compatibility: `SandboxAgent → ModelConfig → mock OpenAI endpoint` in the namespace
`kagent-pilot`, on a one-worker WorkerPool `kagent-pilot` with the `gvisor-default` SandboxConfig.
- **Pins:** kagent and Substrate charts by digest (kagent 0.10.3, Substrate 0.0.9).
- **Access:** the controller's A2A endpoint only, through a port-forward.
- **Stack:** Substrate control plane in `ate-system`, with 6 Valkey pods and RustFS for snapshots.

What it showed: a Ready/Accepted SandboxAgent answered a fixed mock response under gVisor, and a second message on
the same `contextId` reused the session. Its procedure is archived as the
[pilot runbook](../../../archive/legacy-docs/docs/edai2/agents/kagent_bounded_pilot.md).

## 3. Pilot limitations

- A mock model; no MCP, A2A tools, gateways, Feast, KServe or RAG.
- One worker: golden-actor creation could consume it, and `no free workers available` was a stop condition.
- No business identity, credentials or network policy to real backends.
- Preview software: privileged atelet, host state for gVisor, and a fixed 6-pod Valkey cluster.

**Pilot evidence does not prove business execution in a Sandbox.** It is not used as production evidence anywhere in
these reports.

## 4. Migration to production SandboxAgents

| Step | Campaign / change | What it solved |
|---|---|---|
| Sandbox → MCP access | S1; dedicated subjects `sandbox-feature-rag`, `sandbox-drift` | actors authenticate to the MCP servers with their own credentials; MCP logs `actor_id sandbox-*` |
| Egress proxy | `af390922` (Substrate patch) | Ambient did not capture forwarded actor traffic. The patched ateom re-originates egress from the worker pod (`169.254.17.1:3128`), so the worker identity applies, cluster Services only |
| Coordinator Sandbox routes | P4; `a2aGateway.sandboxRoutes` | the Coordinator delegates through the A2A Gateway to `/api/a2a-sandboxes/ewallet-sandbox/<agent>/` |
| Session resume race | PKG-T ([RCA](../../workplans/edai2/agent-platform-campaign/resume-rca/RCA.md)) | kagent treated a SUSPENDING actor as needing no resume; the patched builds resume correctly |
| Worker registry | P2-A (`9f20ee96`) | stale worker records after a sleep sent actors to dead pods; ateapi now reconciles against the pod informer |
| Sandbox-only | `sp-cleanup` (`llm-agent` rev 55) | the BYO Specialist Deployments, their routes and policies were removed; the Specialists run only as SandboxAgents |

## 5. The shared gVisor WorkerPool

Source: [`infra/kubernetes/agent-sandbox/namespace.yaml`](../../../infra/kubernetes/agent-sandbox/namespace.yaml),
[`fleet.yaml.tmpl`](../../../infra/kubernetes/agent-sandbox/fleet.yaml.tmpl).

| Property | Value |
|---|---|
| WorkerPool | `ewallet-sandbox/business-agents`, `sandboxClass: gvisor`, patched `ateomImage` |
| Worker resources | requests `500m` CPU / `1Gi`; limits `1500m` / `2Gi` |
| Replicas | owned by KEDA through `/scale` (min 2, max 3); `namespace.yaml` sets them only at creation |
| Agents on the pool | SandboxAgents `feature-rag-specialist` and `drift-specialist`, each with its own ActorTemplate and golden snapshot |
| Identity | worker pods run as `ewallet-sandbox/sa/default`: token automount off, discovery-only RBAC, mesh principal admitted only to the MCP Gateway (8080) and the OTLP collector (4317) |
| Credentials | per-agent MCP client Secrets (`sandbox-*` subjects), granted only their own tools |
| Node agent | atelet DaemonSet with PriorityClass `ewallet-substrate-node-agent` (no preemption), bound before workers on every node (P2-B) |

## 6. Session lifecycle

1. **New session.** The first message of a `contextId` makes kagent create a session actor from the agent's golden
   snapshot (`ActorTemplate.status.goldenActorID`). Substrate's ateapi assigns it to a free worker, and atelet
   restores it there.
2. **Turn.** The actor handles one A2A turn: the typed request, MCP calls and the result. Turns are serialized per
   session.
3. **Suspend.** After every turn kagent checkpoints the actor to a snapshot (in-cluster RustFS) and suspends it, which
   frees the worker.
4. **Resume.** The next message on the same `contextId` resumes the actor from its snapshot on any free worker.
5. **No free worker.** kagent refuses with `substrate worker pool has no free workers`. The Coordinator waits and
   retries the identical request inside its delegation timeout.

**Implication:** a worker is busy only during a turn and its checkpoint. One worker runs one actor at a time, and the
number of sessions is bounded by snapshot storage, not by workers.

## 7. Actor placement and sharing

Placement is ateapi's choice among free workers. It is not fixed per agent.

**Measured on GKE:**
- **AGS-04:** a sequential run of 12 sessions of both agents ended `SHARED_WORKERS_OBSERVED`: each worker hosted
  actors of both Specialists, golden actors excluded. A parallel run of 24 sessions completed 24/24 but did not show
  sharing on every worker. Placement varies, and sharing is not an invariant.
- **Final regression (`lc-20261117T002220Z`):** 214 `ResumeActor`, 0 restore errors, a consistent worker registry and
  747 suspended actors.

**Owner replay:** `agent_replay.sh workers` (§10) prints the placement per worker and the session outcomes.
`OWNER_EVIDENCE_PENDING`.

## 8. Coordinator and WorkerPool autoscaling

| Autoscaler | Target | Signal | Bounds and behaviour |
|---|---|---|---|
| `ewallet-agents/fraud-coordinator` | Coordinator Deployment | `ewallet_agent_inflight_tasks`, 2 per replica | 2–3 |
| `ewallet-sandbox/business-agents-workerpool` | WorkerPool `/scale` | `sum(ewallet_sandbox_delegations_inflight)`, `AverageValue` 1 | 2–3; up +1/30 s; down after 300 s, then −1/60 s; fallback `currentReplicasIfHigher` |

The two are independent:
- Coordinator replicas add admission concurrency.
- Workers add execution capacity.

**Safe scale-in** (`00311b08`, `eafe9b2e`):
- A removed worker's ateom refuses new work on SIGTERM and finishes its turn and checkpoint (grace period 150 s).
- ateapi never assigns a terminating worker.
- A worker claimed just before its removal is reported as "no free workers", so the restore is retried on a live
  worker.
- Tested on GKE as R3a and R3b.

**Measured on GKE (B′, `bprime-20261116T165353Z`):**
- **Scale-out:** demand above 2 at 18:33:40; the third worker was Ready about 19 s later and ran 29 actors.
- **Coordinator:** it scaled 2→3 at the same time and back at 18:47:16.
- **Metric outage:** the pool held 3 workers for 7 minutes (fallback).
- **Scale-in:** 3→2, with the removed worker idle and drained.
- **Repeated** with the 6-request regression on 2026-11-17: both autoscalers went 2→3→2.

## 9. Capacity testing

**Historical, same harness and workload** (100 typed tasks at concurrency 8, AGR-06 Job):

| Configuration | Success | Throughput | p50 / p95 | Source |
|---|---|---|---|---|
| Fixed 2 workers | 100/100 | 0.128 task/s | — / 158.8 s | `rem-20261116T050511Z` |
| Fixed 2 workers, lower concurrency | 40/40 at 2 and 4 | 0.125 / 0.127 task/s | 15.7 / 16.9 s (c2) | `rem-20261116T050511Z` |
| B′: WorkerPool 2→3 | 100/100 | 0.185 task/s (+45 %) | 34.0 / 102.6 s | `bprime-20261116T165353Z` |

The fixed pool's throughput did not change with concurrency: extra load only adds waiting. The +45 % comes from the
third worker, not from Coordinator replicas.

**A new bounded owner replay is not comparable to these numbers unless it reruns the same harness.**
- The replay's `keda demo` (6 requests) proves both autoscalers react; it is not a throughput measurement.
- A comparable rerun is the AGR-06 Job, an optional experiment with its own approval ([replay
  guide](OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md#fleet-measurements)). `OWNER_EVIDENCE_PENDING` if performed.

## 10. Startup and resume performance

| Quantity | How it is measured | Measured |
|---|---|---|
| New session, first response | `ops.experiments.sandbox_sessions`: first message of a context (`phase new`), end to end through kagent | AGS-05: 24.5 s (drift) / 26.5 s (feature-rag), n = 6 |
| Resumed session, first response | same driver, next message after the actor is Suspended (`phase resume`) | AGS-05: 23.3 s / 25.6 s, n = 6 |
| Actor activation and turn, split | ateapi and kagent logs | G4: cold actor 24.1 s, turn 0.7 s, `ResumeActor` RPC 37–44 ms |
| New worker (pod created → Ready) | worker pod timestamps during scale-out | B′: about 19 s from demand to a Ready third worker |

**Reading the numbers:**
- New and resumed sessions cost about the same, because both restore a snapshot: golden for new, the session's own
  for resume. The turn itself is under a second.
- So the first useful response after resume is dominated by activation, not by the business tools.
- The end-to-end numbers include the turn, so they are not "startup alone".

**Gaps:**
- No Prometheus metric separates snapshot restore from turn execution. The split comes only from logs (G4).
- There is no Deployment-based comparison on the final runtime: the BYO Specialist Deployments were removed, and
  their latency is not re-measurable without restoring them.

**Owner replay:**
- `agent_replay.sh workers` sends 2 messages per context and reports the median and p95 for `new` and `resume` per
  agent.
- `agent_replay.sh keda demo` reports the created→Ready time of any worker added during the run.

Both are `OWNER_EVIDENCE_PENDING`.

## 11. Resource and cost analysis

**Inputs:**
- Prices: the list prices in `ops/gcp/cloud_lab.py` (Cloud Billing Catalog, asia-southeast1, read 2026-11-06):
  $0.02690931 per vCPU-hour and $0.00360594 per GiB-hour for E2.
- Node: e2-standard-4 (4 vCPU, 16 GiB) = **$0.1653/h**.
- Worker requests: the WorkerPool spec above.

All figures are on-demand list prices and request-share estimates, not billing.

| Item | Formula | Estimate |
|---|---|---|
| GKE nodes during a replay | 5 × $0.1653 (+ $0.10/h cluster fee) | $0.83/h (+ $0.10) |
| One worker's request share | 0.5 × 0.0269 + 1 × 0.0036 | $0.0171/h |
| Warm baseline (2 workers) | 2 × $0.0171 | $0.034/h |
| Incremental cost during scale-out (3rd worker, no new node) | 1 × $0.0171 for its lifetime | $0.017/h |
| Worker at its limit (1.5 vCPU, 2 GiB) | 1.5 × 0.0269 + 2 × 0.0036 | $0.048/h |
| **Hypothetical** dedicated pools (2 warm workers per Specialist) | 4 × $0.0171 | $0.068/h (estimate) |

**What these numbers mean:**
- A worker's request is 12.5 % of a node's CPU and 6.25 % of its memory. On the fixed 5-node pool, scale-out to a
  third worker uses spare capacity: it adds request share, not a node.
- Sharing one pool between both Specialists halves the warm-worker request share against the hypothetical dedicated
  pools. That comparison is an **estimate**: the dedicated configuration was never deployed or measured.
- Actual usage is not the request. `kubectl top` during a replay gives it; the owner records it in the replay
  (`OWNER_EVIDENCE_PENDING`).
- No measured savings are claimed: there are no comparable measured configurations.

## 12. Reliability and recovery

| Risk | Mechanism | Measured on GKE |
|---|---|---|
| Stale worker records after a sleep | P2-A reconcile at startup and every 2 min | 17, then 3 stale records removed; 0 `worker pod not found` after the fix |
| Worker replaced while ateapi runs (R1) | update event removes the record during termination | record removed after 0.84 s, replacement registered after 4.9 s, requests completed |
| Worker lost while ateapi is down (R2) | startup reconcile before the first request | stale record removed 10 ms after the initial sync, 12 s before the first RPC |
| Busy worker removed at scale-in (R3a/R3b) | graceful drain, terminating-worker filter, retryable claim | requests completed, nothing ran twice in these runs |
| Metric outage | ScaledObject fallback `currentReplicasIfHigher` | the pool stayed at 3 for 7 min |
| Sleep/wake | guarded sleep pauses both ScaledObjects at their floor; wake-full unpauses them | `verify --expect sleep` and `verify --expect awake` passed |

"Nothing ran twice" holds for those runs and their logs. It is not a proof of exactly-once execution.

## 13. Results, screenshots and limitations

**Owner screenshots** (filenames from the [replay checklist](OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md#screenshot-checklist)):

| Evidence | Screenshot | Status |
|---|---|---|
| WorkerPool, SandboxAgents, actors per worker (Substrate status) | `AGENT_E02_SUBSTRATE_STATUS.png` | `OWNER_EVIDENCE_PENDING` |
| gVisor class, worker identity and RBAC | `AGENT_C01_SANDBOX_GVISOR_RBAC.png` | `OWNER_EVIDENCE_PENDING` |
| Sessions: new and resume latency, placement, suspend/resume | `AGENT_E01_FLEET_SESSIONS.png` (conditional: `workers`) | `OWNER_EVIDENCE_PENDING` |
| Both autoscalers scaling out and in | `AGENT_D02`–`AGENT_D04` (conditional: `keda demo`) | `OWNER_EVIDENCE_PENDING` |

**Limitations:**
- A turn longer than the 150 s grace period is killed at scale-in and restored on the next turn (tested locally only).
- The ReplicaSet does not choose the idle worker at scale-in; safety comes from the drain.
- The PKG-P3 CRD patch (`/scale` selector) is reverted by a `substrate-crds` upgrade and must be re-applied with
  `crd_patch.sh`.
- Placement varies, so sharing on every worker is not guaranteed.
- Throughput is bounded by the number of workers (max 3) and the MCP Gateway rate limit.
- Startup is measured end to end; there is no metric for restore alone (§10).
- The egress proxy's refusal of non-cluster hosts is unit-tested, not observed on GKE.
