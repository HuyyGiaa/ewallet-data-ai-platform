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
| KEDA Coordinator scaling | **GKE_PASS**: 2→3 at 05:45:51Z, 3→2 at 06:03:08Z; the scale-out pod completed tasks (again under B′, §9) |
| Shared WorkerPool autoscaling (B′) | **COMPLETE** (§9): KEDA 2→3→2 on Sandbox demand; the new worker ran 29 actors; 100/100 at 0.185 task/s (+45 %), p95 102.6 s; drain-safe scale-in (R3a/R3b); metric outage holds the pool |
| MCP failover | **GKE_PASS** (2026-11-15 run-04; code unchanged since) |
| Agent Registry (AGI-13) | **GKE_PASS** (§11.1): the 3 Agent entries republished from the live runtime (Sandbox-only Specialists, Coordinator `94a13a8b`); strict readback 8/8; no record removed |
| Distributed tracing (AGI-12) | **GKE_PASS**: Sandbox spans in the Coordinator's trace; since Phase 2 (§7) the kagent controller hop too (21 spans in the combined trace) |
| Security | **GKE_PASS** (unchanged policies; negatives pass) |
| Agent Fleet (shared WorkerPool) | **GKE_PASS** (2026-11-15/16 runs; fleet image updated, sharing unchanged) |
| Sleep / wake readiness | **GKE_PASS** (Phase 2, §7): three guarded sleep/wake cycles; the last wake needed no manual step and passed 5/5 E2E |
| Substrate worker lifecycle (P2-A) | **COMPLETE** (§8): R1 and R2 passed on GKE; stale records removed at every ateapi start, before ateapi serves; no `worker pod not found` after the fix |
| Coordinator Sandbox capacity metrics (P2-D) | **GKE_PASS** (§7.4): counter equals the logged capacity waits; gauge returns to 0 |
| Natural-language combined prompts | Rejected by design (strict guard, owner decision) |
| Legacy Specialist Deployment cleanup (`sp-cleanup`) | **GKE_PASS** (§10): 42 objects removed, 6 modified, 0 added (`llm-agent` rev 55); 5/5 E2E, 3/3 negatives, both KEDA healthy |
| Legacy MCP credential retirement | **GKE_PASS** (§11.2): final server sets `fin-20261117`/`-b`; 9 retired credentials rejected on 5/5 replicas; 9 client Secrets deleted after zero references; old (unreferenced) server-set tables kept |

## 1. Architecture

```
kagent UI / A2A client
  └─ A2A Gateway (agentgateway, route kagent-a2a) ─ kagent controller ─ fraud-coordinator (BYO, KEDA 2..3)
       ├─ LLM Gateway (agentgateway :3000) ─ vLLM CPU "ewallet-fraud-llm"   (NL intake only; ModelConfig ewallet-global-llm)
       └─ A2A Gateway route sandbox-<agent> ─ kagent controller ─ Substrate actor (gVisor) on WorkerPool business-agents
            feature-rag-specialist / drift-specialist (SandboxAgent only since sp-cleanup, §10; WorkerPool KEDA 2..3)
              └─ ateom egress proxy (169.254.17.1:3128, cluster Services only) ─ MCP Gateway (identity sandbox default; server set fin-20261117, §11)
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
  - It labels both the BYO Deployment image and the Sandbox fleet image (until sp-cleanup, §10: a Sandbox-only
    Specialist has no BYO Deployment and no `byo-image-digest` label).
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
- **R1/R2:** the controlled single-worker replacement tests ran on 2026-11-16 in campaign
  `p2a-closure-20261116T151631Z` and passed (§8).

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

## 8. P2-A final closure (`p2a-closure-20261116T151631Z`)

Evidence: `~/ewallet-evidence/agents/p2a-closure/p2a-closure-20261116T151631Z/`, mirrored in
[`docs/evidence/edai2/agents/p2a-closure-20261116T151631Z/`](../../evidence/edai2/agents/p2a-closure-20261116T151631Z/)
(`SHA256SUMS.txt`). Raw ateapi and Coordinator logs are kept private. The runtime is the one deployed in §7, with no
change in this campaign:
- ateapi `substrate-ateapi:0.0.9-ewallet.0291af52.d836b5a3@sha256:f51f7946…`;
- Coordinator `94a13a8b`;
- releases `substrate` rev 7, `kagent` rev 9, `llm-agent` rev 54.

Each step was approved separately (`approvals.txt`): Gate B (wake), Gate C (R1) and Gate D (R2).

### 8.1 Root cause and the existing fix

The defect is described in §7.1: worker records outlived pods deleted while ateapi was down, and actors were sent to
dead pods.

Worker selection (`AssignWorkerStep`) reads the store-backed worker cache and does **not** consult the pod informer.
A stale record is therefore *eligible* until the syncer removes it. The fix (`9f20ee96`) bounds that window:
- an update event removes the record as soon as the pod is deleting;
- a startup reconcile and a 2-minute pass catch deletions that ateapi missed.

If a stale record is selected anyway, the resume fails at the atelet dial, before any atelet call. The PKG-T rollback
then returns the actor to SUSPENDED and releases the claim. The request fails fast and visibly, and nothing runs.

### 8.2 Local tests (`df7c2cc4`, test-only patch, no image change)

- **New tests:**
  - stale record selected → dial fails, no atelet call, actor SUSPENDED, claim released, not sticky over 3 retries;
  - after one reconcile → the actor resumes on the live worker;
  - pool change re-registers the worker;
  - a failed store delete is retried by the next pass.
- The full ateapi package passed with `-race` (×3) and in the official build.
- That build reproduces the deployed ateapi binary byte for byte (`binary_sha256 3c1f43d1…`).
- The R1/R2 harness is `ops/experiments/worker_lifecycle.py`. It records worker pods, ateapi and the registry (kagent
  `/api/substrate/status`) every second and computes the assertions from that record. It has 7 unit tests.

### 8.3 R1: replace one idle worker while ateapi is up — PASS

- **Target:** `business-agents-deployment-55b8b4858c-72k82` (UID `28f33640…`). It was idle: no assignment and no
  non-suspended actor.
- **Mutation:** a graceful `kubectl delete pod`.
- **Timeline:**

  | Event | Time | After the delete |
  |---|---|---|
  | Delete issued | 15:58:33.84 | — |
  | Record removed while the pod was still Terminating | 15:58:34.67 | +0.84 s |
  | Pod gone | 15:58:35.79 | +1.95 s |
  | Replacement `g4kjk` (UID `6480ca8e…`) registered | 15:58:38.73 | +4.9 s |
- **Registry:** the harness saw no stale record in any sample, and the registry matched the live pods at the end.
- **After R1:** fraud, drift and combined all completed (combined `complete`, `unavailable: []`).
  - 4 delegations gave 4 placements, 2 of them on the replacement, including fraud.
  - Each actor had 1 placement plus 2 no-op ResumeActor calls (~2 ms, RUNNING, same pod).
  - Coordinator: 3 tasks, 4 Sandbox sends, 0 waits, 0 failures.
  - 0 `worker pod not found`.

### 8.4 R2: lose one idle worker while ateapi is down — PASS

- **Method:** scale `ate-api-server-deployment` (Helm `substrate`, replicas 1) to 0, wait until no ateapi pod
  exists, delete one idle worker gracefully, wait until it is gone, then scale back to 1.
  - This is the sleep failure reduced to one pod.
  - Deleting the ateapi pod instead would let the Deployment recreate it at once.
  - Blocking API access would exercise the informer relist instead.
- **Target:** `business-agents-deployment-55b8b4858c-xswmd` (UID `0dced329…`).
- **Timeline:**

  | Event | Time |
  |---|---|
  | Scaled to 0 | 16:06:19.34 |
  | No ateapi pod; worker deleted | 16:06:23.29 |
  | Worker gone while ateapi was down; scaled to 1 | 16:06:27.39 |
  | New ateapi registered the replacement `9fgqh` (UID `e5433ac5…`, created during the outage) | 16:06:34.53 |
  | Initial sync | 16:06:34.567 |
  | **Stale `xswmd` removed** (`pod no longer exists`) | 16:06:34.577 |
  | Pod Ready | 16:06:39 |
  | First RPC | 16:06:46.78 |
- **Stale-eligibility window: none.**
  - The startup reconcile finished 12 s before the first request.
  - The readiness probe (initial delay 5 s) keeps the Service from routing to ateapi before then.
  - The same ordering held at the wake: reconcile 16 ms after the initial sync.
- **Outage:** about 20 s without a serving ateapi.
- **After R2:** fraud, drift and combined all completed.
  - 4 placements, one of them on the R2 replacement.
  - 3 tasks, 4 sends, 0 waits, 0 failures, and 0 `worker pod not found`.
- **No manual action:** no Valkey or registry action, and ateapi was restored to its Helm value.

### 8.5 Remediation

None was needed. No defect was found, and the runtime was not changed.

### 8.6 Final integrated acceptance (same runtime, after R1 and R2)

- **Typed:** fraud `ok`, drift `complete`, combined `complete` with `unavailable: []`.
- **NL:** fraud `ok`, drift `complete`.
- **Negatives:** 3/3.
- **Registry:** consistent (no stale record, no unregistered pod, no actor on a missing pod).
- **Trace:** the combined trace has 21 `kagent-controller` spans; the service set is the same as in §7.
- **KEDA:** HPAs at 2/2/2. The WorkerPool stayed at 2.
- **`verify --expect awake`:** no failures.
- **Sleep:** the final guarded sleep passed `verify --expect sleep`. The /32 was removed and the authorized networks
  equal the original 3 CIDRs.

### 8.7 What R1/R2 do not prove

- **Actively executing actors:** both tests replaced **idle** workers. Replacing a worker while its actor is executing
  is not covered.
  - On a deleting pod, the syncer marks the bound actor SUSPENDED while the pod drains.
  - A resume elsewhere during the grace period could overlap with the draining pod.
  - This must be tested in B′ scale-in safety acceptance.
- **Stale records while ateapi serves:** the only case left is a delete event that ateapi misses while it is running.
  The informer relist and the 2-minute pass cover it. If such a record is selected, the request fails fast with no
  execution (tested locally, not observed on GKE).
- **Duplicate execution:** the "no duplicate execution" result is scoped to these runs and their logs (tasks, sends,
  placements and ResumeActor calls). It is not a proof of exactly-once execution.

## 9. B′: shared WorkerPool autoscaling (`bprime-20261116T165353Z`)

Evidence: `~/ewallet-evidence/agents/bprime/bprime-20261116T165353Z/`, mirrored in
[`docs/evidence/edai2/agents/bprime-20261116T165353Z/`](../../evidence/edai2/agents/bprime-20261116T165353Z/)
(`SHA256SUMS.txt`; raw ateapi/Coordinator logs and private renders stay local).

- **Gates:** each one was approved separately (`approvals.txt`):
  1. wake;
  2. PKG-W;
  3. PKG-P3;
  4. WorkerPool and ScaledObject;
  5. Substrate drain runtime, with R3 approved on its own;
  plus the R3 fix.
- **Starting HEAD:** `26eddacd`.

### 9.1 Safe scale-in (B′-3): the defect and the fix

**Audit of Substrate 0.0.9:**
- ateom installs no signal handler, so the SIGTERM of a removed worker pod killed the actor mid-turn.
- The syncer released the actor as soon as the pod was terminating.
- Worker selection never looked at the pod.
- Worker pods had the default 30 s grace period and no preStop hook.
- kagent suspends the actor after every turn and serializes turns per session, so a worker is busy only for a turn and
  its checkpoint.

**`substrate-0.0.9-graceful-drain.patch` (`00311b08`):**
- **ateom:** on SIGTERM it refuses new Run/Restore and waits until the active sandbox is checkpointed, then exits.
- **ate-controller:** worker pods get `terminationGracePeriodSeconds: 150`.
- **ateapi:**
  - never assigns a worker whose pod is missing or terminating (this also closes the P2-A stale-record window);
  - keeps an actor bound to its draining worker until the pod is gone;
  - reconciles drained terminating workers away;
  - releases a killed drain's actor on the final delete.

**`substrate-0.0.9-drain-retryable.patch` (`eafe9b2e`), from R3 attempt 1:**
- **What failed:** a worker removed just after ateapi claimed it, before atelet reached ateom, has an idle ateom, which
  exits at once. The failed restore then surfaced as an ordinary error, and the combined result came back `partial`.
- **Fix:** ateapi now reports such a failure as `FailedPrecondition "no free workers"`, which kagent and the Coordinator
  retry on a live worker.

**Tests:** 11 new Go tests. The full ateapi, atecontroller and ateom suites pass with `-race` and in the official build.

| Component | Image | Release |
|---|---|---|
| ateapi | `0.0.9-ewallet.0291af52.d836b5a3.49125b15.7d22bbfc@sha256:0aeab81c…` | substrate rev 9 |
| ate-controller | `0.0.9-ewallet.0291af52.49125b15@sha256:7475c1f5…` | substrate rev 9 |
| ateom | `0.0.9-ewallet.0291af52.d8d1db43.49125b15@sha256:0dd6cbf6…` | WorkerPool `spec.ateomImage` |

Every pushed digest was verified to contain the tested binary.

**R3 on GKE (`R3-RESULT.txt`):**
- **R3a:** the worker was deleted the moment its ateom began restoring an actor. ateom waited for the restore, the turn
  and the checkpoint, then exited (`idle=true`). ateapi kept the actor bound and assigned nothing to the draining pod.
  The combined request completed, with one placement per actor, and the pod was gone within about 33 s.
- **R3b:** the worker was deleted right after it was claimed. The restore was reported as "no free workers", kagent
  retried it on the replacement worker, and the request completed in 16 s. Nothing ran twice.

### 9.2 KEDA and CRD integration, ownership (B′-1/2)

- **PKG-W:** KEDA rev 2 also watches `ewallet-sandbox`, with a RoleBinding for `keda-operator`. Every existing
  ScaledObject stayed Ready.
- **PKG-P3:** `crd_patch.sh` added `spec.scaleSelector` and `/scale labelSelectorPath`. The backup spec sha is
  `675b1a18…`. A `substrate-crds` upgrade reverts the patch; `crd_patch.sh` re-applies it.
- **WorkerPool:** gained `scaleSelector: ate.dev/worker-pool=business-agents`, and `/scale` now reports that selector,
  which matches the worker pods.
- **ScaledObject `business-agents-workerpool`** (`workerpool-scaledobject.yaml`):
  - min 2, max 3;
  - trigger: `sum(ewallet_sandbox_delegations_inflight) or vector(0)`, `AverageValue` threshold 1 (one worker per
    in-flight logical delegation);
  - scale-up +1/30 s;
  - scale-down after 300 s, then −1/60 s;
  - fallback `currentReplicasIfHigher`.
- **Ownership:** KEDA owns `spec.replicas`.
  - `namespace.yaml` keeps `replicas` only for creation, and the live pool is changed only through `bprime.sh`.
  - `cloud_lab` never records, restores or verifies WorkerPool-owned Deployments.
  - Guarded sleep pauses the WorkerPool ScaledObject at its floor, and wake-full unpauses it.

### 9.3 GKE acceptance

**Scale-out 2→3, automatic, under the 100-task load:**

| Event | Time |
|---|---|
| Demand above 2 | 18:33:40 |
| Pool spec 3, third worker `86rn7` Ready | 18:33:59 (≈19 s) |

- The new worker ran **29 actors**; the others ran 41 and 32.
- There were 0 rollbacks and 0 `not found` errors.
- The Coordinator KEDA scaled 2→3 at the same time (18:33:59), on its own, and back 3→2 at 18:47:16.

**Load test, against the fixed 2-worker baseline** (same harness and workload, 100 typed tasks at concurrency 8):

| Metric | 2 workers (baseline) | B′ (2→3) |
|---|---|---|
| Success | 100/100 | **100/100** |
| Throughput | 0.128 task/s | **0.185 task/s (+45 %)** |
| p50 / p95 | — / 158.8 s | **34.0 s / 102.6 s** |
| Admission waits (counter) | — | 71 |
| Peak workers / scale-out latency | 2 / — | 3 / 19 s (harness: 47 s to peak) |

**Metric failure (E):**
- **Method:** only the WorkerPool ScaledObject's `serverAddress` was pointed at a `.invalid` host, from 18:44:10 to
  18:51:38. The shared Prometheus was not touched.
- **KEDA:** health `Failing` (30 failures) and `Fallback=True`.
- **Result:** the pool **stayed at 3 for 7 min**, past the point where it would otherwise have scaled in. The
  Coordinator KEDA was unaffected; it scaled in normally at 18:47.

**Scale-in 3→2 (B):**
- **Timing:** after the restore, with continuous light traffic, the HPA chose 2 at 18:56:30.
- **Removed worker:** `2pfpz`, 9 s after its last actor's checkpoint. Its ateom logged `drain finished idle=true`.
- **Traffic:** all 8 requests sent across the window completed.
- **Registry:** consistent afterwards.
- **Busy workers:** a KEDA-chosen *busy* worker was not caught in this window; the busy case is R3a/R3b.

**E2E (C), on the final runtime:**
- Typed fraud `ok`, drift `complete`, combined `complete` with `unavailable: []`.
- NL fraud `ok`, drift `complete`.
- Negatives 3/3.
- The combined trace `5211445e…` has `kagent-controller` (21 spans), Coordinator, both Specialists, `feature-rag-mcp`,
  `drift-mcp` and agentgateway.
- Backends ran for real: Feast features at `2026-05-31T23:59:56Z`, KServe `fraud-detector` v1 probability 0.064, and
  the `knowledge_search` and `drift_detection` tools.

**Sleep/wake (G):**
- Guarded sleep paused 4 ScaledObjects, including the WorkerPool's at 2, and `verify --expect sleep` passed.
- wake-full `keda_resumed` included the WorkerPool ScaledObject, and `verify --expect awake` passed (exact).
- The ateapi startup reconcile removed 3 stale records 24 ms after its sync.
- After the wake: 5/5 E2E and a consistent registry.
- Final state: guarded sleep, 0 nodes, /32 removed, the original 3 CIDRs.

### 9.4 Limits and observations

- **Turns longer than the grace period:** a turn longer than 150 s is killed at SIGKILL. Its actor is then released
  and restored on the next turn; this is tested locally, not observed on GKE.
- **Drain log timing:** ateom's `beginDrain` takes the service lock, so the "draining" log appears only after an
  in-flight Restore returns. ateapi's terminating-pod filter keeps new activations away from a draining pod meanwhile.
- **Pod choice at scale-in:** the ReplicaSet does not pick the idle worker. Safety comes from the drain, not from pod
  selection (no pod-deletion-cost, because ateapi has no pod-patch RBAC).
- **Evidence gap:** ateapi logs every `ListActors` response in full, so frequent registry polling rotated its log once
  and the Gate 5 E2E placement lines were lost. The run was repeated with a filtered log stream.
- **Duplicate execution:** "no duplicate" holds for these runs (tasks, placements, ResumeActor calls). It is not a
  proof of exactly-once execution.


## 10. Legacy Specialist Deployment cleanup (`sp-cleanup-20261116T200345Z`)

Evidence: `~/ewallet-evidence/agents/sp-cleanup/sp-cleanup-20261116T200345Z/` (`SHA256SUMS.txt`), mirrored in
`docs/evidence/edai2/agents/sp-cleanup-20261116T200345Z/`. Gate alias `agi-20261116T200345Z`.

**Why:** after P4 the Coordinator delegates only through the A2A Gateway sandbox routes, so the BYO Specialist
Deployments in `ewallet-agents` served no traffic. They still cost two pods each, a KEDA ScaledObject, a PDB that
blocked sleep, and 32 mesh/network policies.

**Mechanism (`f0f28506`):**
- `agents.specialists.<name>.runtime` is `deployment` (default, renders the old release unchanged) or `sandbox`.
- The overlay `agi/specialists-sandbox-only.yaml` sets `sandbox` for both Specialists. The chart then stops rendering
  their Agent CR, ScaledObject, PDB, ServiceAccount, prompt ConfigMaps, policies, network flows and `x-kagent-host` route.
- `.Values.agents.specialists` stays: tools, prompts and the BYO image (the SandboxAgent fleet uses the same image).
- Gate package `sp-cleanup` in `agi_campaign.sh` expects exactly 42 removed and 6 changed identities.
- The AGI-08 A2A negative now uses the sandbox route as its routed control.

**Gate (live, before the upgrade):** preflight PASS on rev 54; live diff equal to the matrix (removed 42, changed 6,
added 0).
- Changed fields: Coordinator annotation `a2a-gateway-config-sha256`; a2a-gateway `checksum/config`.
- A2A config: only the 2 `x-kagent-host` routes are dropped; `kagent-a2a` and both `sandbox-*` routes are identical.
- Flows: 8 dropped, all to or from the retired Specialists.
- NetworkPolicy and RoleBinding: SA `feature-rag-specialist` dropped.

**Upgrade (rev 55, 20:39–20:44Z):** Helm removed the 42 objects. kagent garbage-collected each Agent's Deployment,
Service, ServiceAccount and config Secret (`agent-card.json`, `config.json`, not a credential). Nothing was deleted by
hand, and nothing reappeared 15 min later. KEDA logged one race at 20:43:51 (ScaledObject evaluated after its
Deployment was gone), with no error after it.

**Kept (35 fingerprinted objects):** WorkerPool, both SandboxAgents, both remaining ScaledObjects, Substrate, kagent,
KEDA, MCP servers and gateways are unchanged. Only a2a-gateway and fraud-coordinator rolled, and their ReplicaSet
templates differ only by the annotation above. The 21 `mcp-token-*` Secrets are not Helm-managed and remain.

**Acceptance on GKE (after the upgrade):**

| Check | Result |
|---|---|
| E2E | 5/5: typed fraud `ok` (prob 0.0639895560144887), drift `complete` (psi 0.00048), combined `complete`, NL fraud `ok`, NL drift `complete`. Every hop: Coordinator → A2A Gateway `sandbox-*` route → SandboxAgent actor on `business-agents` → MCP Gateway (`actor_id sandbox-*`) → backend |
| No fallback | Legacy pods 0, Deployments 0; the retired hosts `drift-specialist.ewallet-agents` and `feature-rag-specialist.ewallet-agents` get 404 `route not found`, while their sandbox controls get 200 |
| Negatives | 3/3: wrong-server credential 401, ungranted tool hidden, undeclared A2A host 404 (sandbox control 200) |
| Tracing | 5 traces in Tempo (120–312 spans each) with agentgateway, kagent-controller, Coordinator, Specialist and MCP spans; no span names a retired host |
| Placement | 27 `ResumeActor` calls on both shared workers during the E2E window |
| KEDA | Coordinator and WorkerPool ScaledObjects Ready, `ValidMetricFound`, not paused; under the concurrency run the WorkerPool scaled 2→3 |
| Concurrency | 6 concurrent requests (2 fraud, 2 drift, 2 combined): 6/6 completed (25–75 s) |
| Persistence | 674 kagent sessions survived the sleep/wake; 15 new sessions since the cleanup, all on the SandboxAgents |
| Registry (read-only) | PARTIAL, see the verdict table; no republish was approved |

**Rollback:** `AGI_APPROVAL=SP-CLEANUP-ROLLBACK:agi-20261116T200345Z AGI_PACKAGE=sp-cleanup agi_campaign.sh rollback
agents`. It runs `helm rollback llm-agent 54` and requires the manifest to equal the stage 0 manifest. kagent then
recreates the Deployments and KEDA the two ScaledObjects.

**Not done (separate approvals):**
- Republish the registry metadata. The catalog already describes the Specialists as Sandbox-only.
- Retire the legacy MCP credentials `mcp-token-{feature-rag,drift}-specialist-*` (9 Secrets plus their grants in
  `llm-agent-mcp`). No workload mounts them now. The AGI-08 negatives still use them as test credentials, so a new
  server set and new negative credentials are needed first.
- Leftover references that match nothing: cloud_lab profile `minimal.one-agent` (never run) and a Loki regex in
  `dashboards/logs-operations.json`.

## 11. Final closure: Registry sync, legacy MCP credential retirement (`closure-20261116T211833Z`)

Evidence: `~/ewallet-evidence/agents/closure/closure-20261116T211833Z/` (`SHA256SUMS.txt`), mirrored in
`docs/evidence/edai2/agents/closure-20261116T211833Z/`. Gate alias `agi-20261116T211833Z`. Run unattended under
the owner's bounded "final closure" directive.

### 11.1 Agent Registry (Stage A)

- **Inputs:** the catalog was rendered from the live `llm-agent` values (both Specialists `runtime: sandbox`), the
  live Coordinator image (`94a13a8b…`) and the live SandboxAgent fleet image (`29867794…`). The Registry was
  snapshotted (GET) before the write.
- **Dry run:** Prompts and MCPServers were already identical; only the three Agent entries differed.
- **Publish:** Prompts and MCPServers `unchanged`; `drift-specialist 1.1.0`, `feature-rag-specialist 1.0.0` and
  `fraud-coordinator 1.1.0` `configured`. The prompt integrity gates were MATCH before and after.
- **Strict readback:** PASS 8/8, again PASS after the E2E.
- **Before/after:** the Specialists lost `ewallet.io/byo-image-digest` and their description now says "served only
  as a kagent 0.10.3 SandboxAgent … no BYO Deployment". The Coordinator label moved `9775406101b8…` → `94a13a8b1688…`.
  Old tags (`1.0.0`), all 12 prompt versions and both MCPServers are byte-identical.

### 11.2 Legacy MCP credential retirement (Stage B)

**Inventory (before):**
- Server set `sbx-20261115` (ewallet-mcp) held three releases: `legacy`, `r1-release1` and `sbx-20261115`.
- Active clients: SandboxAgents use `sbx-20261115`; the kagent controller (list-only) uses `r1-release1`.
- No workload referenced the nine `mcp-token-{drift,feature-rag}-specialist-*` Secrets.
- Before the change, all nine were accepted on every `ewallet-mcp` replica (they could still call tools).
- The in-release `ewallet-agents/drift-mcp` (legacy table) also accepted the `legacy` drift token.

**Tooling fixes (`46e4c354`, `a8fe2bdf`, `2cae8340`):**
- **Issue 1:** `grants_from_values` drops subjects whose deployed runtime is `sandbox`, from both the Specialist
  tools and any leftover server `toolGrants`.
- **Issue 2:** a source table tolerates retired (or explicitly dropped) subjects by name only, and never carries
  them.
- **Final set:** a final set can keep existing releases (`--carry-release`), so no client moves. It fails if any
  active, non-retired identity would be dropped.
- **Overlays:** Helm keeps a `null` coming from a second values file, so the `cr-*` packages apply their overlay
  themselves. The first gate caught this before any mutation.
- **Rollback:** neither `cr-mcp` nor `cr-agents` has a rollback. A rollback would re-select a server set that
  still accepts the retired credentials.

**Migration:**

| Step | Result |
|---|---|
| Prepare | Immutable final tables `fin-20261117` (ewallet-mcp drift/feature-rag: `sandbox-*` + `kagent-controller`). Validated offline with the real authenticator before any rollout |
| `cr-mcp` | `llm-agent-mcp` rev 17 → 18. Exact diff: grants env without the two retired subjects, auth table → `fin-20261117`. 4/4 replicas rolled, 0 restarts |
| `cr-agents` attempt 1 | Rejected. The old image (`695279967`) of `ewallet-agents/drift-mcp` has no discovery subjects, so the list-only config failed at startup (`authentication_not_configured`, readiness 503). The old pod kept serving; `helm --atomic` rolled back (rev 56 failed, rev 57 = rollback to 55). Nothing had been revoked or deleted yet |
| `cr-agents` attempt 2 | Table `fin-20261117-b` holds only `sandbox-drift`, which already has the same privilege on `ewallet-mcp/drift-mcp`. Validated offline against that image's own `security.py` (which also reproduces the attempt 1 failure). Rev 58, Ready |

**Revocation acceptance (per replica, `mcp_credential_check.sh`, direct pod forwards):** every probe passed with a
working control on the same replica, on 5/5 replicas (`ewallet-mcp` drift×2 and feature-rag×2, `ewallet-agents`
drift-mcp×1).
- All nine retired credentials get HTTP 401 at initialize.
- `sandbox-*` calls its granted tool: `ok` on ewallet-mcp. On `ewallet-agents/drift-mcp` it authenticates, but its
  backend lacks the drift baseline, as before the change.
- The kagent controller sees the tool but cannot call it (`call-denied`).
- One harness defect was found and fixed: stale port-forwards from an earlier run had answered on reused ports, so
  that run was discarded.

**Zero references, then deletion:**
- 0 references in 667 cluster objects (workloads, pods, ConfigMaps, Agents, SandboxAgents, RemoteMCPServers,
  ScaledObjects, WorkerPools).
- 0 references in both releases' manifests.
- Server-side dry run first. Exactly the nine client Secrets were deleted: 33 → 24 credential Secrets.
- **Kept:** `kagent-controller` (r1-release1, sbx-20261115), `sandbox-*`, the `mcp-token-revoked-*` sentinels, and
  every auth table, old sets included.

**AGI-08 (post-cleanup principals):**
1. A `sandbox-feature-rag` credential is refused by drift-mcp (401), while it is valid on feature-rag-mcp.
2. The list-only kagent controller cannot call `drift_detection`; the control is `sandbox-drift`.
3. An undeclared A2A host gets 404 while the sandbox route works.

No privilege was added for testing.

### 11.3 Full GKE E2E after the migration (Stage C)

| Check | Result |
|---|---|
| Typed fraud | `ok`, prob 0.0639895560144887, `sandbox-feature-rag` ran `feature_lookup`, `fraud_scoring`, `knowledge_search`; trace `610af841b5bf95ff839095fd0faa7611` |
| Typed drift | `complete`, psi 0.0004795543627041439, `sandbox-drift` ran `drift_detection`; trace `bc71530b9595cb3203b59c1cdc779d5d` |
| Typed combined | `complete`, `unavailable: []`; trace `21511c7dfce11c6b3338b72604fd03be` (206 spans, incl. kagent-controller 21) |
| NL fraud / NL drift | `ok` / `complete`; traces `3ead80b7c5394c09c100635d82c2b746` / `953682e3901a455d005924f663716d4f` |
| Negatives | 3/3 PASS |
| Concurrency | 6/6 (15–71 s); both KEDA ScaledObjects went 2→3 and Active |
| Placement | 183 `ResumeActor` on 3 shared workers, 0 restore errors; worker registry consistent (no stale record) |
| HTTP | Only the two negatives' 404/401 plus streamable-HTTP 405 (GET) |
| Registry | Strict readback PASS 8/8 |

### 11.4 Limitations

- **Old server sets:** `sbx-20261115`, `r1-transition` (ewallet-mcp) and `legacy` (ewallet-agents) still contain
  the retired tokens. They are no longer referenced, their client Secrets are gone, and `cr-*` refuse rollback. A raw
  `helm rollback` of `llm-agent-mcp` to ≤ 17 or `llm-agent` to ≤ 57 would still re-select them. Deleting those
  tables needs a separate approval.
- **Sp-cleanup rollback:** `SP-CLEANUP-ROLLBACK` (rev 54) is no longer a working path. The Specialist Deployments
  would come back without valid credentials.
- **`ewallet-agents/drift-mcp`:** it keeps `sandbox-drift` because its image cannot run list-only. It has no route
  or ingress, and its drift backend artifacts are missing (pre-existing).
