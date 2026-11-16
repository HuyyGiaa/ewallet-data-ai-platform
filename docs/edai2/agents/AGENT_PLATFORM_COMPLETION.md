# Agent Platform on GKE: final integration results

This page reports the final GKE integration of the EDAI-2 Agent Platform: campaign
`agi-20261115T145608Z`, cluster `ewallet-dev`, 2026-11-15, branch
`feature/edai2-agent-platform-integration`.

- Every result below was measured on the cluster unless it is marked otherwise.
- The owner reruns the demonstrations for screenshots with the
  [owner evidence replay](OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md).
- Component design is in [AGENTS_REGISTRY_GATEWAYS.md](AGENTS_REGISTRY_GATEWAYS.md) and
  the contracts are in [AGENT_INTEGRATION_CONTRACTS.md](AGENT_INTEGRATION_CONTRACTS.md).

**Verdict after the final remediation (`rem-20261116T050511Z`, 2026-11-16, [§6](#6-final-remediation-rem-20261116t050511z)):**

| Area | Verdict |
|---|---|
| Core integration (`AGENT_PLATFORM_CORE_GKE_PASS`) | **Holds** (5/5 E2E through the Sandbox routes on the remediated images, 3/3 negatives) |
| Post-P4 reliability / capacity | **GKE_PASS**: 100/100 at concurrency 8 (40/40 at 2 and 4). Throughput ceiling 0.125–0.128 tasks/s from the fixed 2-worker pool |
| KEDA Coordinator scaling | **GKE_PASS**: 2→3 at 05:45:51Z, 3→2 at 06:03:08Z; the scale-out pod completed tasks |
| MCP failover | **GKE_PASS** (2026-11-15 run-04; code unchanged since) |
| Agent Registry (AGI-13) | **GKE_PASS**: readback 8/8 after the approved publish; no record removed |
| Distributed tracing (AGI-12) | **GKE_PASS**: Sandbox spans in the Coordinator's trace; since Phase 2 (§7) the kagent controller hop too (21 spans in the combined trace) |
| Security | **GKE_PASS** (unchanged policies; negatives pass) |
| Agent Fleet (shared WorkerPool) | **GKE_PASS** (2026-11-15/16 runs; fleet image updated, sharing unchanged) |
| Sleep / wake readiness | **GKE_PASS** (Phase 2, §7): three guarded sleep/wake cycles; the last wake needed no manual step and passed 5/5 E2E |
| Substrate worker lifecycle (P2-A) | **GKE_PASS** (§7.1): stale worker records removed at every ateapi start; no `worker pod not found` after the fix |
| Coordinator Sandbox capacity metrics (P2-D) | **GKE_PASS** (§7.4): counter equals the logged capacity waits; gauge returns to 0 |
| Natural-language combined prompts | Rejected by design (strict guard, owner decision) |

## 1. Architecture

```
kagent UI / A2A client
  └─ A2A Gateway (agentgateway, route kagent-a2a) ─ kagent controller ─ fraud-coordinator (BYO, KEDA 2..3)
       ├─ LLM Gateway (agentgateway :3000) ─ vLLM CPU "ewallet-fraud-llm"   (NL intake only; ModelConfig ewallet-global-llm)
       └─ A2A Gateway route sandbox-<agent> ─ kagent controller ─ Substrate actor (gVisor) on WorkerPool business-agents
            feature-rag-specialist / drift-specialist (SandboxAgent; Deployment copies stay as fallback, KEDA 2..3)
              └─ ateom egress proxy (169.254.17.1:3128, cluster Services only) ─ MCP Gateway (identity sandbox default)
                   ├─ feature-rag-mcp (2 replicas, PDB) ─ Feast online (DB 0 / RAG DB 3), KServe fraud-detector, RAG index
                   └─ drift-mcp (2 replicas, PDB) ─ pinned versioned baseline/window artifacts
```

| Component | Implementation |
|---|---|
| Coordinator | [`byo_coordinator.py`](../../../ai_platform/llm_agent/byo_coordinator.py), [`programmatic_coordinator.py`](../../../ai_platform/llm_agent/programmatic_coordinator.py), NL intake [`workflow_intake.py`](../../../ai_platform/llm_agent/workflow_intake.py) / [`nl_intake.py`](../../../ai_platform/llm_agent/nl_intake.py) |
| Feature-RAG / Drift Specialists | [`byo_feature_rag_specialist.py`](../../../ai_platform/llm_agent/byo_feature_rag_specialist.py), [`byo_drift_specialist.py`](../../../ai_platform/llm_agent/byo_drift_specialist.py), MCP client [`byo_mcp.py`](../../../ai_platform/llm_agent/byo_mcp.py) |
| MCP servers | [`tools/service.py`](../../../ai_platform/llm_agent/tools/service.py), chart release `llm-agent-mcp` |
| Gateways, KEDA, policies | chart [`infra/helm/llm-agent-platform`](../../../infra/helm/llm-agent-platform) (`agi/`, `g3/keda.yaml`, `templates/policy-gateways.yaml`, `templates/cross-namespace.yaml`) |
| Sandbox fleet | [`infra/kubernetes/agent-sandbox`](../../../infra/kubernetes/agent-sandbox) (SandboxAgents, WorkerPool `business-agents`); egress proxy [`substrate-0.0.9-sandbox-egress-proxy.patch`](../../../infra/kubernetes/kagent-pilot/patches/substrate-0.0.9-sandbox-egress-proxy.patch) |
| Agent Registry | [`ops/llm_agent/registry_catalog.py`](../../../ops/llm_agent/registry_catalog.py) (render / publish / readback) |
| Campaign gates | [`scripts/llm_agent/agi_campaign.sh`](../../../scripts/llm_agent/agi_campaign.sh), [`helm_object_diff.py`](../../../scripts/llm_agent/helm_object_diff.py) |

## 2. Implementation changes made during the campaign

Each change was gated: an exact Helm object diff, a server-side dry-run, a digest-pinned
image rebuilt from the commit, and a recorded approval. Every image rebuild went through
Trivy before push.

| Commit | Change | Why (GKE observation) |
|---|---|---|
| `2f7d5a2b` | MCP image copies `redis_isolation.py`; an import-closure test for every image | feature-rag-mcp crashed at start-up (`ImportError`) |
| `f84c3202` | credential plan reads `toolGrants` | Sandbox subjects had no MCP credentials |
| `af390922` | ateom egress proxy (Substrate patch) | ambient mesh does not capture forwarded actor traffic: it black-holed or arrived without identity |
| `c672ba09` | gate accepts checksum-only pod-template rolls (`--expect-annotation-only`) | P2 config change rolled Prometheus and Grafana |
| `0b8f945d`, `a5406c5b`, `132b6bfc` | agents back off on gateway 429. The MCP failover reads the status through the real MCP SDK (httpx2, which hides the status). Only the shed request is repeated, in session. agentgateway's in-protocol rate limit (`-32003`, the `tools/call` isError text) is recognised | AGR-06: 95, then 91, then 92 of 100 tasks lost to the gateway rate limits |
| `40027507` | the LLM is offered only the tools the deterministic screen allows | CPU model picked drift for a fraud question 3/3 |
| `c7a6f294`, `fd2c49c9` | `img-*` image-roll packages, per-package gate diffs; `t1-agents` Sandbox OTLP flow | image fixes after P2; Sandbox OTLP denied |
| `74dfa134`, `891fd9c6`, `bc30736e` | MCP experiment: NodeLocal DNSCache egress, token without newline, opt-in force failure | AGR-02 harness never ran on GKE before |
| `b2cc5459` | `registry_catalog readback` (GET only) | AGI-13 |
| `bb7a2a09` | flush spans before a SandboxAgent actor is suspended | AGI-12. **Committed, not deployed** |
| `f6ea89b5` | Coordinator waits for a free Sandbox worker (`sandbox_capacity_exhausted`, 2–10 s backoff inside the delegation timeout) | final regression: Sandbox pool full under concurrency 8. **Committed, not deployed** |

## 3. Results on GKE

The raw evidence is in `~/ewallet-evidence/agents/integration/agi-20261115T145608Z/`,
written as `$EV`. Its SHA-256 manifest is `$EV/SHA256SUMS.txt`, mirrored in
[`docs/evidence/edai2/agents/agi-20261115T145608Z/`](../../evidence/edai2/agents/agi-20261115T145608Z/).

| Checkpoint | Verdict | Measured result (evidence) |
|---|---|---|
| P1 MCP multi-replica (AGR-01, AGR-03) | GKE_PASS | 2/2 replicas per server on distinct nodes; PDB allows 1; 170/170 typed calls during a rolling update (`agr/`) |
| S1 Sandbox → MCP | GKE_PASS | MCP servers log `actor_id sandbox-feature-rag` / `sandbox-drift`; cross-credential negatives denied (`stage2/S1-neg-*`) |
| P2 KEDA (AGR-04, AGR-05) | GKE_PASS | 3 ScaledObjects Ready; KEDA owns the HPAs; `ewallet_agent_inflight_tasks` one series per pod (`agr/agr04-*`, `agr05-*`) |
| AGR-06 autoscaling | GKE_PASS | run-05: 100/100, 2.24 tasks/s, p50 2.29 s, p95 10.39 s. KEDA 2→3 at 19:08:26Z, 3→2 at 19:15:12Z. Scale-out pods completed 32 and 53 tasks. Failed pre-fix runs 01–04 kept (`agr/AGR-06/AGR-06-RESULT.txt`) |
| AGR-07 workflows after scaling | GKE_PASS | fraud, drift and combined E2E on the scaled Coordinator |
| AGR-02 MCP failover | GKE_PASS | run-04 (force failure, 10 calls/s): optimized 1188/1188 with 1 client retry; baseline 1129/1189, 12 s outage; production unchanged. Harness defects in runs 01–02 kept (`agr/AGR-02/AGR-02-RESULT.txt`) |
| G4 deadlines | TIMEOUT_VALIDATED_NO_CHANGE | cold actor 24.1 s, turn 0.7 s, `ResumeActor` 37–44 ms; all inside kagent 180 s and Coordinator 480 s (`g4/`) |
| P4 E2E Feature-RAG | GKE_PASS | typed and NL: probability 0.0639895560144887, features and citations equal the direct backend probes (`stage3/`) |
| P4 E2E Drift | GKE_PASS | typed and NL: PSI 0.0004795543627041439 equal to the direct probe, with dataset revisions (`stage3/`) |
| P4 E2E combined | GKE_PASS (typed) | both Sandboxes in parallel, `status complete`. NL combined is rejected `ungrounded_argument` (§5) |
| Negative E2E | GKE_PASS | out-of-scope request rejected before any tool; undeclared Sandbox path 404; unknown entities → truthful `missing_feature`, no score invented (`stage3/neg/`) |
| AGI-01, 02, 03, 08, 14 | GKE_PASS | ModelConfig uid/generation resolved by both LLM agents; isolated Feast targets; invalid input rejected; 3 negatives with controls; images equal campaign digests, 0 restarts (`stage2/`, `stage4/`) |
| AGI-12 tracing | PARTIAL | one trace of 233–249 spans across the A2A Gateway, Coordinator, LLM Gateway, vLLM, Sandbox route, MCP Gateway, MCP, Feast and KServe; `gen_ai.*` tokens. No Sandbox Specialist spans (§5) |
| MCP tool metrics | GKE_PASS | `ewallet_ewallet_mcp_tool_calls_total` (collector `namespace: ewallet` prefix) in Prometheus and on dashboard "E-Wallet Agent & MCP" (`stage4/AGI-14-mcp-tool-metrics.txt`) |
| AGI-13 Registry | PARTIAL | prompts and MCP servers registered and equal to the deployed catalog; agent entries stale (§5) (`stage4/AGI-13-*`) |
| AGS-01, 02, 03 | GKE_PASS (AGS-02 egress negative only unit-tested) | gVisor ActorTemplates, SA without token, discovery-only RBAC, mesh principal `ewallet-sandbox/sa/default` |
| AGS-04 shared workers | GKE_PASS | sequential run 12/12 `SHARED_WORKERS_OBSERVED` with golden actors excluded; the parallel run 24/24 completed but `NOT_OBSERVED` is kept (`ags/`) |
| AGS-05 cold vs warm | measured | new 24.5 / 26.5 s, resume 23.3 / 25.6 s (n = 6, drift / feature-rag) |
| AGS-06 utilization | measured | `ags/AGS-04/AGS-06-top.txt` |
| Security / external routing | GKE_PASS (review) | default-deny; A2A Gateway port 8083 admits only `sa/fraud-coordinator`; no Gateway, Ingress or LoadBalancer exposes the platform (`stage4/security-review-authz.txt`) |
| Final regression (`final-regression/`) | GKE_PASS (bounded) | status clean; 5/5 E2E (typed fraud, drift, combined; NL fraud, drift) through the Sandbox routes with no fallback; 3/3 negatives; registry readback reproduced |
| KEDA after P4 under load (`final-regression/keda/`) | PARTIAL | the Coordinator still scales 2→3→2 (21:33:54Z / 21:48:40Z) and the scale-out pod served tasks. Only 42/100 and 40/100 tasks completed: delegations now go to the fixed 2-worker Sandbox pool, which refuses overflow with `substrate worker pool has no free workers` (reproduced: 2 of 6 concurrent; `final-regression/sbx-concurrency/`). The accepted AGR-06 100/100 ran before P4 against the Deployment Specialists |
| B′ WorkerPool autoscaling | NOT_TESTED | needs a separate CRD approval; out of scope |

## 4. Test method

- **Business E2E:** the Coordinator was called through the A2A Gateway (`kagent-a2a` route,
  the path the kagent UI uses), with typed requests and natural-language prompts. Each
  result was checked hop by hop:
  - the Coordinator log;
  - the A2A Gateway route;
  - the MCP server's `actor_id`, which is the Sandbox credential subject;
  - zero tasks on the Deployment Specialists;
  - values compared with direct MCP probes of the same backends.

  These are the `agent_replay.sh e2e` checks.
- **Autoscaling:** the AGR-06 load Job (`agent_load.py`, concurrency 8, 100 tasks) ran
  inside the cluster, with replica samplers and HPA events. Real work on the new pods was
  shown by task counts per pod.
- **Failover:** an isolated copy of drift-mcp ran in `ewallet-mcp-exp`. The probe used the
  campaign's real client failover, and one pod was killed mid-run. Production
  `ewallet-mcp` was compared before and after.
- **Local tests:** run with the locked environments, for example
  `uv run --frozen --project infra/docker/g3-byo-coordinator --group test`. Neither
  environment collects every test: the A2A SDK, Feast and numpy are missing from one or
  the other. The pre-existing failures (RAG ingestion; the sandbox-experiment timeouts,
  which reproduce on the pre-campaign commit) are unchanged.

## 5. Limitations and pending approvals (as of 2026-11-15)

Items 1 and 2, and the capacity part of item 5, are resolved by the final remediation ([§6](#6-final-remediation-rem-20261116t050511z)).

1. **Agent Registry is stale for agents (AGI-13).**
   - The registry holds the 3 agents only at `1.0.0`, runtime `kagent-sandboxagent`, from
     2026-11-03.
   - The deployed agents are BYO `fraud-coordinator 1.1.0`, `feature-rag-specialist 1.0.0`
     and `drift-specialist 1.1.0`.
   - The corrective `registry_catalog publish` passed a server-side dry-run for all 8
     documents. The live write needs owner approval.
2. **Sandbox Specialist spans are missing from traces (AGI-12).**
   - Context does propagate: the actors' MCP calls are in the Coordinator's trace. After
     `t1-agents`, ztunnel shows their OTLP export allowed.
   - The actors buffer spans in a 5 s `BatchSpanProcessor`, and kagent suspends them about
     0.6 s after the answer. kagent sets `KAGENT_PRE_RESPONSE_TRACE_FLUSH=true` for this
     case; the BYO runtime ignored it.
   - `bb7a2a09` fixes it and is unit-tested. Deploying it needs an approved image roll
     (`img-mcp` / `img-agents`), and the trace must then be re-checked.
   - The root cause is consistent with every observation, but it is unverified until the
     fix runs on GKE.
3. **Natural-language combined prompts are rejected** with `ungrounded_argument`. The 0.6B
   CPU model returns drift arguments that differ from the labelled text. The strict guard
   stays by owner decision. Typed combined passes.
4. **Not exercised:**
   - drain under load during scale-in (scale-in happened with 0 tasks in flight);
   - the Sandbox egress-deny clause on GKE (the image is distroless).
5. **Throughput** is capped by the MCP Gateway `localRateLimit` (20 req/s, about 2.2–2.5
   tasks/s). KEDA adds concurrency headroom, not throughput past that limit.
   - After P4 the stricter limit is the **fixed 2-worker Sandbox pool**. kagent refuses delegations beyond its free
     workers (HTTP 200 with JSON-RPC -32603), and the deployed Coordinator fails them as `invalid_a2a_response`.
   - At concurrency 8 that lost 58–60 % of the tasks.
   - `f6ea89b5`, not deployed, makes the Coordinator wait and repeat instead.
   - Adding capacity needs more workers, either a larger fixed pool or B′. Scaling the Coordinator does not help.
6. **Cloud-lab sleep with KEDA.** The guarded sleep cannot hold the KEDA-scaled agents at 0, so the campaign sleep
   ended `SLEEP_BLOCKED` and the cluster was left awake at 4 nodes. The procedure that needs approval is in the
   [replay, section 12](OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md#12-final-check-cleanup-and-sleep-replay-12).
7. **Scope boundaries:**
   - The WorkerPool is fixed at 2 workers. Sharing is proven; WorkerPool autoscaling (B′)
     is not.
   - Neither kagent nor Substrate is HA.


## 6. Final remediation (`rem-20261116T050511Z`)

- **Evidence:** `~/ewallet-evidence/agents/remediation/rem-20261116T050511Z/`. Its manifest is `SHA256SUMS.txt`; the
  mirror is [`docs/evidence/edai2/agents/rem-20261116T050511Z/`](../../evidence/edai2/agents/rem-20261116T050511Z/).
- **Approvals:** owner-approved per item in `approvals.txt`. A6 (5th node) was not approved.
- **Images:** the 3 BYO images, built from `21994c03` and passed by Trivy:

  | Image | Digest |
  |---|---|
  | coordinator | `sha256:9775406101b83b03cdbbfcb550daf7e7ced61b0bde8ca1869a02e16daf06b69b` |
  | feature-rag | `sha256:29867794f67f103ed434110a6ad220347384a1f3f5cc8c3b77f1a4d9c4988b77` |
  | drift | `sha256:75011f6ce496e192db997ddc931c82d8d91c4ce87f59414d75ca6eacc2e86931` |

  The MCP image stays `fd2c49c9`, because nothing it imports changed.
- **Rollout:**
  - `llm-agent` rev 51 → **52** (`img-agents`; the gate diff was 3 Agents, images only).
  - Sandbox fleet image `81b6bd17` → `29867794`. The fleet is not Helm-managed: it is 2 SandboxAgents, image only,
    approved separately as A2b.
  - Rollback targets: rev 51; fleet image `81b6bd17`.

### 6.1 Sandbox capacity under load (P0)

- **Problem:** after P4, 42/100 and 40/100 tasks completed at concurrency 8.
- **Root cause:** kagent refuses a Sandbox delegation when the fixed 2-worker pool has no free worker. It answers
  JSON-RPC `-32603` with `substrate worker pool has no free workers` inside HTTP 200, and the Coordinator failed it as
  `invalid_a2a_response`.
- **Change** (`f6ea89b5`, `21994c03`):
  - `_send` recognises exactly that refusal and waits with jittered backoff (2–10 s, at most 12 attempts) inside the
    delegation timeout. The wait can be cancelled.
  - Every attempt is identical (same `messageId` and `contextId`), so kagent routes it to the same actor.
  - Other `-32603` errors and malformed bodies are not repeated.
  - Each wait is logged as `coordinator.delegation_wait`.
- **Tests:** identical attempts; 8 concurrent delegations on a 2-slot pool; trace context on every attempt;
  cancellation; deadline; budget exhaustion; non-retryable errors.
- **GKE result** (`acceptance/load/`, AGR-06 methodology, all through the Sandbox routes):

| Concurrency | Tasks | Completed | p50 / p95 e2e | Admission waits | Throughput |
|---|---|---|---|---|---|
| 2 | 40 | 40 | 15.7 s / 16.9 s | 0 | 0.125 tasks/s |
| 4 | 40 | 40 | 30.2 s / 63.5 s | 11 (29.7 s total, max attempt 3) | 0.127 tasks/s |
| **8** | **100** | **100** | 46.7 s / 158.8 s | 97 (353.3 s total, max attempt 7) | 0.128 tasks/s |

- **No duplicate business execution:** all 100 request IDs of the concurrency-8 run show exactly one `feature_lookup`,
  `fraud_scoring` and `knowledge_search` each on feature-rag-mcp (`c8-mcp-tool-calls.log`).
- **KEDA:** Coordinator 2→3 at 05:45:51Z and 3→2 at 06:03:08Z. The scale-out pod `…8ls5c` completed 5 tasks during the
  load.
- **Limitations:**
  1. **Throughput is capped by the pool.** It is about 0.125–0.128 tasks/s whatever the concurrency, so higher load
     only adds waiting. More throughput needs more Sandbox workers, from a larger fixed pool or B′ (out of scope).
     KEDA on the Coordinator adds admission concurrency, not capacity.
  2. **Fallback Deployment scaled for Sandbox work.** Sandbox actors now export `ewallet_agent_inflight_tasks` with
     `agent=feature-rag-specialist` (over 100 actor series, value 1 when frozen by the suspend). KEDA therefore also
     scaled the idle fallback Deployment `feature-rag-specialist` 2→3 (05:46:21Z) and back (06:04:07Z); it ran 0
     tasks. Fixing it needs a runtime label on the gauge and a filtered ScaledObject query: a new image and Helm
     change, not done.

### 6.2 Sandbox distributed tracing (P1)

- **Problem:** the trace held no Sandbox Specialist spans.
- **Root cause:** the actors buffer spans in a 5 s batch, and kagent suspends the actor about 0.6 s after the answer.
- **Change:** `bb7a2a09` honours `KAGENT_PRE_RESPONSE_TRACE_FLUSH`. `execute()` force-flushes with a 2 s bound after
  its span ended, on success and on error. Executor-level tests cover both Specialists.
- **GKE result:** trace `a38538611be4ffd81b5ae2d2e4402742` (natural-language fraud, `acceptance/trace/`) has 245
  spans: agentgateway 23, fraud-coordinator 174, **feature-rag-specialist 19**, feature-rag-mcp 29.
  - The Sandbox spans include `g3.feature_rag_specialist.execute` (489 ms).
  - Its children are `MCP send initialize`, `tools/list` and `tools/call feature_lookup / fraud_scoring /
    knowledge_search`, all in one trace with the Coordinator and the MCP server spans.
- **Remaining:** the parent of the execute span, the kagent controller hop, is not in Tempo. The controller logs
  `traces export: … invalid target address ""`, which is an empty OTLP endpoint in the kagent release. Spans of the
  actor's own HTTP server that end after the response are not flushed.

### 6.3 Agent Registry (P1)

- **Change:**
  - The catalog marks P4 Specialists `kagent-sandboxagent-byo`, rendered from the live Helm values.
  - It labels both the BYO Deployment image and the Sandbox fleet image.
  - Readback treats the absent field agentregistry stores for an empty list as equal.
- **GKE result:**
  - Before: `drift-specialist 1.1.0` and `fraud-coordinator 1.1.0` missing; `feature-rag-specialist` stale.
  - Publish: 8 documents, PASS.
  - Readback after: **8/8 match**. Agents went from 3 to 5 records; prompts stayed 12 and MCP servers 2; nothing was
    removed (`registry/`).

### 6.4 KEDA-aware sleep (P1, operational cost)

- **Problem:** the guarded sleep scaled the agents to 0, KEDA (`minReplicaCount 2`) restored them, and the PDBs blocked
  the resize (`SLEEP_BLOCKED`, 2026-11-16 early).
- **Change** (`21994c03`): `cloud_lab sleep --scale-pdb-guarded`:
  - detects the ScaledObjects of guarded Deployments **before any mutation**;
  - refuses unless it gets `--pause-keda` with its own approval (`CLOUD_LAB_KEDA_PAUSE_APPROVAL`);
  - pauses them (`autoscaling.keda.sh/paused-replicas=0` plus the marker `ewallet.io/cloud-lab-paused-by`), scales,
    and waits for the PDBs;
  - restores the pauses and replicas when the preparation fails.

  `wake-full` unpauses only marked ScaledObjects. `verify --expect awake` fails while a marked pause remains, and
  `plan-sleep` lists what would be paused. Six unit tests cover refusal, approval, ordering, failure recovery,
  selective unpause and "no KEDA installed".
- **GKE result:**
  - `plan-sleep`: `SAFE_AFTER_REVERSIBLE_PREPARATION`, no active work, `keda_pause` = the 3 agent ScaledObjects.
  - The approved sleep paused the 3 ScaledObjects, scaled the guarded Deployments, and resized `cpu-pool` to 0.
  - `verify --expect sleep`: no failures (`sleep/`).
  - The temporary authorized network was removed afterwards.
- **Remaining:** the unpause path of `wake-full` is unit-tested only. It runs at the owner's next wake (replay §1),
  which must show `keda_resumed` with the 3 ScaledObjects.

## 7. Phase 2 foundation (`p2-foundation-20261116T084657Z`)

Evidence: `~/ewallet-evidence/agents/phase2/p2-foundation-20261116T084657Z/`, mirrored in
[`docs/evidence/edai2/agents/p2-foundation-20261116T084657Z/`](../../evidence/edai2/agents/p2-foundation-20261116T084657Z/)
(`SHA256SUMS.txt`; the exported GPU release values and raw Coordinator logs stay private). Every mutation had its own
owner approval (`approvals.txt`). B′ was not started: no ScaledObject for the WorkerPool, no PKG-P3, and the KEDA watch
namespaces are unchanged.

### 7.1 P2-A: Substrate worker registry (`9f20ee96`, image `0.0.9-ewallet.0291af52.d836b5a3`)

- **Defect:** Substrate 0.0.9 removed a worker record only on a pod delete event, and its startup sync only added
  records. After a sleep every pod was gone, so the records stayed in Valkey and actors were sent to dead pods.
  On GKE before the fix, the combined, NL fraud and NL drift requests failed at the same seconds as
  `CallAteletRestore: worker pod not found`.
- **Fix:** ateapi reconciles the store against the synced worker-pod informer, at startup and every 2 min:
  - it removes records whose pod is gone, or whose pod has another UID or pool;
  - it releases their actor the same way a delete event does;
  - it removes nothing before the informer cache has synced, and never touches a live worker.

  Go tests: 7 new ones. Four of them fail on unpatched code. The full Substrate suite passes with `-race`.
- **GKE:** rolled out with `runtime_fix.sh substrate` (identity, exact diff = the ateapi image, server dry-run,
  `--atomic`).
  - The first start removed 17 stale records (4 `business-agents`, 13 `kagent-pilot`) and kept the 2 live workers.
  - After each later full sleep/wake it removed 3 stale records (the pods from before the sleep) and registered the
    new pods.
  - Not one `worker pod not found` was seen after the fix.
- **Not run:** the controlled single-worker replacement tests R1/R2 (deleting a worker pod, or deleting one while
  ateapi is down). The owner did not approve them. All-node replacement by sleep/wake was exercised three times.

### 7.2 P2-B: wake hygiene

- **Recovery manifest** (`8b43582c`): `cloud_lab record-manifest` records the awake cluster before each sleep. It is
  read-only and refuses a sleeping cluster or a sleep in progress.
  - The next wake restored the MCP servers to the Helm-declared 2 without a manual step.
  - `verify --expect awake` passed with its exact comparison on wakes 3 and 4. Phase 1 had failed it: the stale
    2026-11-06 manifest said 1.
- **atelet scheduling** (`998c0d2a`):
  - **Defect, seen on two 5-node wakes:** one atelet stayed Pending on a full node while a Sandbox worker ran
    there, which gave `found 0 atelet pods on node`.
  - **Root cause:** atelet does not tolerate `node.kubernetes.io/not-ready:NoSchedule`. The DaemonSet controller
    therefore created its pod only at node Ready, together with the whole priority-0 backlog.
  - **Fix:** the PriorityClass `ewallet-substrate-node-agent` (value 100000, `preemptionPolicy: Never`, so it never
    evicts anything) plus the not-ready toleration. It was applied with `runtime_fix.sh atelet-placement` while the
    pool was empty, because a live rollout would have stalled on the Pending pod.
  - **GKE:** on the next two wakes every atelet was created 2–3 s after its node registered and 15–20 s before
    Ready: 5/5 Running, with no eviction.
  - WorkerPool 0.0.9 has no podAffinity or topology spread, so workers cannot be tied to atelet. With atelet bound
    first on every node, any worker placement is served, which also holds for B′ scale-out.
- **G3 LLM label** (`ba1a9407`):
  - **Defect:** the wake labelled the first three node names. They were already full (82–99% CPU), so
    `vllm-primary` stayed Pending.
  - **Fix:** the label now goes to the nodes with the most free CPU. On wake 4 `vllm-primary` was scheduled at once.
- **`ewallet-llm-gpu`:** release `llm-gpu-router` (P3, finished 2026-11-08) was uninstalled after its values and
  manifest were exported. The namespace and the non-Helm `agentgateway-staging` (0 replicas, recorded in the
  manifest) remain.

### 7.3 P2-C: kagent controller tracing (`e752cc63`)

- **Network** (`tr-agents`): one NetworkPolicy and one Istio ALLOW for `cluster.local/ns/kagent/sa/kagent-controller`
  to `otel-collector:4317`.
- **Controller** (`runtime_fix.sh kagent-otel`): the endpoint is now
  `http://otel-collector.ewallet-agents.svc.cluster.local:4317`. The exact diff is 1 ConfigMap key and the checksum
  annotation; the image is unchanged.
- **GKE:** `kagent-controller` appears in Tempo with 21 spans in each combined business trace (wakes 1 and 4), and
  the `invalid target address` errors stopped.

### 7.4 P2-D: Coordinator Sandbox capacity metrics (`9d206555`, image `g3-byo-coordinator@sha256:94a13a8b…`)

- `ewallet_sandbox_delegations_inflight{specialist}`: one unit per logical delegation, released however it ends.
- `ewallet_sandbox_admission_waits_total{specialist}`: kagent capacity refusals only.
- Rolled with the new `img-coordinator` package (`87531452`); only the Coordinator Agent changed.
- **Bounded load:** 24 typed tasks at concurrency 4.
  - All 24 completed, with no task failure and no duplicate task id.
  - The counter reached **16**, equal to the 16 logged `delegation_wait code=sandbox_capacity_exhausted` lines.
  - There were 40 sends = 24 first sends + 16 resends.
  - The gauge peaked at 4–5 and returned to 0. The peak of 5 above concurrency 4 is export lag across 3 Coordinator
    pods during the KEDA scale-out.
- **Cardinality:** 2 series per Coordinator pod.
- **KEDA suitability** (evaluation only, B′ not activated): the gauge is a direct demand signal. A B′ trigger
  should use `sum(ewallet_sandbox_delegations_inflight)` with a short staleness window, since scaled-in pods leave
  series for up to 5 min. `rate(ewallet_sandbox_admission_waits_total[2m]) > 0` is a good saturation alarm.

### 7.5 Integrated acceptance (wake 4, no manual intervention)

- Typed: fraud `ok`, drift `complete`, combined `complete` with `unavailable: []`.
- NL: fraud `ok` and drift `complete`.
- Negatives: 3/3.
- `verify --expect awake`: no failures.
- `kagent-controller` spans are in the trace.

