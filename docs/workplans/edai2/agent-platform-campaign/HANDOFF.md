# Agent platform completion: campaign handoff

## B′ shared WorkerPool autoscaling `bprime-20261116T165353Z` (2026-11-16/17)

Evidence: `~/ewallet-evidence/agents/bprime/bprime-20261116T165353Z/` (`SHA256SUMS.txt`), mirrored in
`docs/evidence/edai2/agents/bprime-20261116T165353Z/`. Results: `AGENT_PLATFORM_COMPLETION.md` §9.

| Item | Value |
|---|---|
| `BPRIME_STATUS` | **COMPLETE**: all 14 definition-of-done items pass on GKE (§9) |
| `GKE_E2E_STATUS` | **PASS**: 5/5 workflows + 3/3 negatives on the final runtime and again after the wake; real Feast, KServe, RAG and Drift backends; trace with `kagent-controller` spans |
| `SCALE_OUT_STATUS` | **PASS**: automatic 2→3 in 19 s under the 100-task load; the new worker ran 29 actors; back to 2 automatically |
| `ACTIVE_ACTOR_SCALE_IN_STATUS` | **PASS** after one fix. R3a: drain kept a restoring/running actor through checkpoint. R3b: a claimed worker's restore is retried on a live worker. R3 attempt 1 failed, root cause fixed (`eafe9b2e`) |
| `COORDINATOR_KEDA_STATUS` | **PASS**: 2→3→2 under the same load; unaffected by the WorkerPool metric outage |
| `SLEEP_WAKE_STATUS` | **PASS**: guarded sleep paused 4 ScaledObjects (WorkerPool at 2); wake-full unpaused them; exact `verify --expect awake`; post-wake E2E 5/5 |
| Load | 100/100; 0.185 task/s (+45 % vs 0.128); p50 34.0 s; p95 102.6 s (vs 158.8 s); 71 admission waits |
| Cluster | **ASLEEP**: 0 nodes, `verify --expect sleep` passed; ScaledObjects (incl. the WorkerPool's) paused by marker `bprime-20261116T165353Z`; /32 removed; authorized networks = the original 3 CIDRs |
| Next wake | `cloud_lab wake-full --nodes 5 --manifest ~/ewallet-evidence/agents/bprime/bprime-20261116T165353Z/final-sleep/recovery-manifest` (its unpause includes the WorkerPool ScaledObject) |
| Cost (estimate) | about 2.3 h awake × 5 nodes × ~$0.165 ≈ $1.9 (list price, not billing) |

**Live state:**

| Release / object | State |
|---|---|
| `keda` | rev 2 (watches `ewallet-sandbox`) |
| `substrate` | rev 9: ateapi `0aeab81c` (drain + retryable), ate-controller `7475c1f5` (worker grace 150 s) |
| `substrate-crds` CRD | patched: `scaleSelector`, `labelSelectorPath` |
| WorkerPool `business-agents` | `scaleSelector`, `ateomImage` `0dd6cbf6` (drain on SIGTERM) |
| ScaledObject | `business-agents-workerpool` |

**Commits (rollback boundaries):**

| Commit | Change |
|---|---|
| `00311b08` | drain patch and tooling |
| `4e2d524c` | ScaledObject, ownership, `cloud_lab` |
| `2ae35022` | pins, `ateom`-only patch, R3 harness |
| `eafe9b2e` | retryable restore |
| docs commit | documentation |

**Rollbacks:**
1. `bprime.sh so-rollback` (deletes the ScaledObject; the pool goes back to 2).
2. `bprime.sh pool-rollback`.
3. `runtime_fix.sh substrate rollback` (`PKG-T-SUBSTRATE-ROLLBACK`).
4. `crd_patch.sh revert` (only once no pool uses `scaleSelector`).
5. `sandbox_watch.sh keda rollback` (`PKG-W-ROLLBACK`).

Roll back in that order.

**Ready next:**
- the legacy Specialist Deployment cleanup (deletion matrix in the Phase 1 section);
- the owner Evidence Replay.

**Watch next:**
- turns longer than 150 s;
- a `substrate-crds` upgrade reverting PKG-P3;
- ateapi log volume when the registry is polled.

## P2-A final closure `p2a-closure-20261116T151631Z` (2026-11-16)

Evidence: `~/ewallet-evidence/agents/p2a-closure/p2a-closure-20261116T151631Z/` (`SHA256SUMS.txt`), mirrored in
`docs/evidence/edai2/agents/p2a-closure-20261116T151631Z/`. Results: `AGENT_PLATFORM_COMPLETION.md` §8.

| Item | Value |
|---|---|
| `P2_A_STATUS` | **COMPLETE**: R1 and R2 passed on GKE; acceptance criteria P2A-01..18 met (§8) |
| `P2_FOUNDATION_STATUS` | **COMPLETE**: P2-A, P2-B, P2-C and P2-D all complete |
| `BPRIME_FOUNDATION_READY` | **YES, to start the B′ campaign.** Worker lifecycle, atelet placement, wake hygiene, tracing and the demand metrics are verified. B′ itself is not built (see below) |
| Runtime | Unchanged from Phase 2: `substrate` rev 7 (ateapi `d836b5a3`), `kagent` rev 9, `llm-agent` rev 54 |
| Code | `df7c2cc4`: test-only Substrate patch (`images: []`) and the R1/R2 harness. No runtime change |
| Cluster | **ASLEEP**: 0 nodes, `verify --expect sleep` passed. ScaledObjects paused by marker `p2a-closure-20261116T151631Z`. The temporary /32 was removed; authorized networks = the original 3 CIDRs |
| Next wake | `cloud_lab wake-full --nodes 5 --manifest ~/ewallet-evidence/agents/p2a-closure/p2a-closure-20261116T151631Z/final-sleep/recovery-manifest` |
| Approvals | Gate B (wake + guarded sleep), Gate C (R1), Gate D (R2), each separate (`approvals.txt`) |
| Cost (estimate, not billing) | about 1 h awake × 5 nodes × ~$0.165 per node-hour ≈ $0.8 |

**B′ handoff (preparation only; nothing deployed).** Target: `WorkerPool/ewallet-sandbox/business-agents`; first
experiment 2 → 3 → 2.

1. **KEDA watch:** KEDA 2.21 watches `ewallet-agents,ml-platform` only. Adding `ewallet-sandbox` is a KEDA Helm
   change.
2. **PKG-P3:** the WorkerPool CRD has `/scale` but no `labelSelectorPath`. Phase 1 assessed it as required for the
   KEDA-generated HPA. It is a CRD change (`substrate-crds`) and needs its own approval.
3. **ScaledObject:** a shared one for the WorkerPool, `minReplicaCount 2`.
   - Trigger: `sum(ewallet_sandbox_delegations_inflight)`, with a short staleness window, because scaled-in
     Coordinator pods leave series for up to 5 min.
   - Saturation alarm: `rate(ewallet_sandbox_admission_waits_total[2m]) > 0`.
4. **Ownership:** KEDA becomes the single owner of `WorkerPool.spec.replicas`. Nothing else may write it,
   including `cloud_lab` restore and manual scaling.
5. **Readiness:**
   - workers register at pod IP, before Ready (Substrate 0.0.9 has no readiness gate);
   - atelet is bound first on every node (P2-B);
   - WorkerPool 0.0.9 has no podAffinity or topology spread.
6. **Scale-in safety is the main open risk** (§8.7): a removed worker with an executing actor. B′ needs an
   acceptance test with an active actor during scale-in, and a drain or graceful-suspend story first.
7. **KEDA fallback:** decide the behaviour when Prometheus is unreachable (fallback replicas = 2).
8. **`cloud_lab`:** it must keep pausing the WorkerPool ScaledObject at sleep, and must not restore WorkerPool
   replicas against KEDA.
9. **Acceptance:**
   - automatic worker creation and removal;
   - placement on the new workers;
   - active-actor scale-in;
   - latency and throughput versus the fixed pool;
   - concurrent reliability.

   R1/R2 do not cover these.

## Phase 2 foundation `p2-foundation-20261116T084657Z` (2026-11-16)

Evidence: `~/ewallet-evidence/agents/phase2/p2-foundation-20261116T084657Z/` (`SHA256SUMS.txt`), mirrored in
`docs/evidence/edai2/agents/p2-foundation-20261116T084657Z/`. Results: `docs/edai2/agents/AGENT_PLATFORM_COMPLETION.md` §7.

| Item | Value |
|---|---|
| `P2_FOUNDATION_STATUS` | **PARTIAL** at the end of that campaign. **COMPLETE** since the P2-A closure above |
| `P2_A_STATUS` | **PARTIAL**: lifecycle is correct across 3 all-node replacements (sleep/wake) and the first post-fix start (17 stale records removed). R1/R2 not run |
| `P2_B_STATUS` | **COMPLETE**: atelet bound at node registration on 2 consecutive wakes; wake 4 passed 5/5 E2E with no manual step; exact `verify --expect awake` passed |
| `P2_C_STATUS` | **COMPLETE**: kagent-controller spans in the business trace |
| `P2_D_STATUS` | **COMPLETE**: counter = logged capacity waits (16/16); gauge returns to 0; labels bounded |
| `BPRIME_FOUNDATION_READY` | **CONDITIONAL**: signals, worker lifecycle and atelet placement are ready. B′ still needs R1/R2, PKG-P3 (`labelSelectorPath`), the WorkerPool ScaledObject and the KEDA watch for `ewallet-sandbox` |
| Cluster | **ASLEEP**: 0 nodes, `verify --expect sleep` passed. Agent ScaledObjects are paused by marker `p2-foundation-20261116T084657Z`. The temporary /32 was removed; authorized networks = the original 3 CIDRs |
| Next wake | `cloud_lab wake-full --nodes 5 --manifest ~/ewallet-evidence/agents/phase2/p2-foundation-20261116T084657Z/final-sleep/recovery-manifest` (mirrored under `final-sleep/`) |

**A. Commits** (branch only, not merged). Each is its own rollback boundary.

| Commit | Package | Change |
|---|---|---|
| `9f20ee96` | P2-A | ateapi worker reconcile patch |
| `e752cc63` | P2-C | `tr-agents`, `kagent-otel` and the runtime_fix replace path |
| `9d206555` | P2-D | Coordinator metrics |
| `8b43582c` | P2-B | `record-manifest` |
| `87531452` | P2-D | `img-coordinator` package |
| `01855047` | P2-A | ateapi digest pin |
| `998c0d2a` | P2-B | atelet placement |
| `ba1a9407` | P2-B | G3 label placement |

**B. Live state after the campaign:**

| Release | Revision | Change |
|---|---|---|
| `substrate` | 7 | ateapi `d836b5a3` + atelet placement (PriorityClass `ewallet-substrate-node-agent`) |
| `kagent` | 9 | OTLP endpoint with scheme |
| `llm-agent` | 54 | `tr-agents` flow + Coordinator `94a13a8b` |
| `llm-gpu-router` | — | uninstalled; namespace `ewallet-llm-gpu` kept |

**C. Approvals:** all are in `approvals.txt`.
- G1/G2, G5/G6 and G7–G9 at the start.
- F2: one `ml-pipeline` eviction during wake 1, before the owner ruled out manual eviction.
- G3 after the acceptance evidence.
- The P2-B steps (1)–(5).
- Never approved: G4 (R1/R2).
- The agi tool ran under the alias `agi-20261116T084657Z`, because it requires the `agi-` form.

**D. Rollbacks:**
- Substrate: `runtime_fix.sh substrate|atelet-placement rollback`, with tokens `PKG-T-SUBSTRATE-ROLLBACK` and
  `P2B-ATELET-PLACEMENT-ROLLBACK`. `atelet-placement` rolls back without `--wait` while asleep.
- kagent: `P2C-KAGENT-OTEL-ROLLBACK`.
- Agents: `TR-AGENTS-ROLLBACK` and `IMG-COORDINATOR-ROLLBACK`.

**E. Wake procedure from now on:**
1. Before each sleep, run `record-manifest --out <dir>`.
2. Wake with that manifest.
3. Do not scale MCP by hand.

`cloud_lab` labels the least-loaded nodes for the G3 LLM.

**F. Tooling gotchas:**
- `agi_probe` evidence is create-only: move stale `stage0/*precheck*.json` aside before rerunning a preflight.
- `helm get manifest` can return an empty result transiently; rerun the preflight.
- Port-forwards go stale after rollouts; restart them.
- The `img-agents` gate expects all three Agents to change; use `img-coordinator` for Coordinator-only changes.

**G. Not done:**
- R1/R2: owner approval needed.
- Merge: the branch is pushed, nothing is merged and no PR is open.
- B′ work: out of scope by directive.

**H. Cost:** about 3.4 h awake × 5 nodes × ~$0.165 per node-hour ≈ $2.8, over four wakes (09:32–13:12Z).

**I. Specialist Deployments, legacy credentials, Sandbox-only registry and the owner Evidence Replay:** untouched, as
directed.

**J. Next:**
1. Approve R1/R2 to close P2-A.
2. Then PKG-P3 and the B′ plan, using `sum(ewallet_sandbox_delegations_inflight)` as the demand signal.

## Phase 1 `p1-20261116T075841Z` (2026-11-16): wake verification, controller tracing, B′ readiness

Evidence: `~/ewallet-evidence/agents/phase1/p1-20261116T075841Z/` (`SHA256SUMS.txt`), mirrored in
`docs/evidence/edai2/agents/p1-20261116T075841Z/`.

- **Approvals** (`approvals.txt`): W1 (temporary /32 + `wake-full` + MCP back to 2), W2 (read-only + smoke), F1 (scale 3
  GPU leftovers to 0), W3 (KEDA-aware sleep).
- **Verdict:** `PHASE_1_STATUS = PARTIAL`. The wake works and the KEDA unpause is verified on GKE. Two wake-readiness
  defects in the Substrate layer remain. They block a clean wake and are the main B′ risks; neither was fixed here.

### P1-A baseline

- **Git:** HEAD `af16927c` = origin, clean tree.
- **Helm:**
  - `llm-agent` rev 52: agents and gateways (Helm in namespace `default`).
  - `llm-agent-mcp` rev 17.
  - `kagent` rev 8 (chart 0.10.3, controller `0.10.3-ewallet.ea032b19`).
- **Ownership** (from the renders and live labels):
  - Agent CRs, ScaledObjects, PDBs, SAs and policies: Helm `llm-agent`.
  - The Agent Deployments and Services: kagent controller, from the Agent CR.
  - HPAs: KEDA.
  - SandboxAgents and WorkerPool `business-agents`: applied directly (campaign label; no Helm owner).
  - ActorTemplates, golden actors and worker Deployment: kagent and the Substrate controller.
  - WorkerPool CRD: Helm `substrate-crds`.
  - Authorized networks: Terraform. Every temporary change was reverted.

### P1-B wake (real GKE)

- **Wake:** `wake-full` exit 0, 4 nodes Ready.
- **KEDA unpause:** `keda_resumed` listed all 3 ScaledObjects, and the pause annotations and markers were removed.
  - The HPAs reappeared only about 80 s later: the keda-operator pod was still `ContainerCreating`, and while it was,
    the ScaledObjects still showed `Paused=True`.
  - After that: 3 HPAs, `Paused=False`, `Ready=True`.
- **`verify --expect awake`:** fails only on `ewallet-mcp/{feature-rag,drift}-mcp replicas != 1`. The 2026-11-06
  recovery manifest predates PKG-P1 (Helm declares 2); this is a **stale manifest**, not a platform fault.
- **Defect 1 — node agent starved.**
  - After the wake, node `mh90` sat at 99% CPU requested, and the `atelet` DaemonSet pod there stayed Pending
    (Helm `substrate`, no `priorityClassName`).
  - Both business-agents workers were on `mh90`, so every Sandbox restore failed ("Resume rolled back after a failed
    restore"). The typed smoke was 0/3.
  - Cause: 3 `ewallet-llm-gpu` Deployments left over from the P3 GPU campaign, outside the cloud_lab manifest, took
    500m. They were scaled to 0 (F1, recorded drift).
- **Defect 2 — stale worker registry.**
  - The Substrate worker registry listed 4 workers for a 2-replica pool. `nns95` and `v8rzm`, from before the sleep,
    remained beside the live `4zllc` and `qbvxc`.
  - Actors assigned to a dead worker fail with `CallAteletRestore: worker pod not found`.
  - The combined smoke was `partial` (Drift failed) 3 out of 3 times; Drift alone passed.
  - Not repaired: no supported API removes a worker record.
- **Smoke after F1:**
  - Typed fraud: completed, prob 0.0639895560144887, Sandbox route, `actor_id sandbox-feature-rag`, 0 fallback.
  - Typed drift: completed, PSI 0.0004795543627041439, `actor_id sandbox-drift`.
  - Combined: `partial` (defect 2).
  - Negatives: 3/3 PASS.
- **Sleep:** KEDA-aware guarded sleep exit 0, `verify --expect sleep` with no failures, 0 nodes, temporary /32 removed.

### P1-C kagent controller tracing: `CONFIGURATION_FIX_AVAILABLE`

Full analysis: `tracing/DIAGNOSIS.txt`.

- Git **is** applied: live values `otel.tracing.enabled=true`, and ConfigMap
  `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT='otel-collector.ewallet-agents.svc.cluster.local:4317'`.
- The controller builds its exporter with `autoexport` from the standard OTEL env.
- OTEL Go parses an env endpoint as a URL: `url.Parse("host:4317")` gives scheme = host and an empty host. That is
  exactly the live log line `invalid target address ""` (seen again 2026-11-16 08:22Z).
- **Fix 1:** set the endpoint to `http://otel-collector.ewallet-agents.svc.cluster.local:4317` (one ConfigMap key; the
  controller restarts once).
- **Fix 2, also required:** an otel-collector ingress flow for `cluster.local/ns/kagent/sa/kagent-controller` (none is
  allowed today).
- Both are owner-approved Phase 2 items. Not applied.

### P1-D Specialist Deployments and B′ readiness

- **Routing:** `GatewayPolicy.call_for` routes a Specialist named in `EWALLET_SANDBOX_SPECIALISTS` to
  `/api/a2a-sandboxes/ewallet-sandbox/<agent>/`, always. There is **no automatic fallback** to the Deployment. The
  Deployment Specialists are reachable only after a P4 rollback, so they are **manual rollback targets**, not HA.

**Deletion matrix** (Helm `llm-agent` renders 22 objects for Feature-RAG and 21 for Drift: `ownership/specialist-objects.txt`):

| Resource (per Specialist) | Owner | Phase 2 action |
|---|---|---|
| Agent CR `ewallet-agents/<agent>` (type BYO) → kagent Deployment + Service | Helm → kagent | REMOVE (values: drop the BYO specialist) |
| ScaledObject `<agent>` + KEDA HPA | Helm / KEDA | REMOVE (keeps KEDA from scaling an idle Deployment) |
| PDB `<agent>` | Helm | REMOVE (also removes a sleep blocker) |
| ServiceAccount `<agent>` | Helm | REMOVE |
| ConfigMap `<agent>-prompt-v*` | Helm | INVESTIGATE (the registry prompt stays; the Sandbox image embeds its own) |
| AuthorizationPolicies: a2a-gateway→, kagent-controller→, →agentgateway, →mcp-gateway, →otel-collector | Helm | REMOVE |
| NetworkPolicies (≈10 per agent) | Helm | REMOVE |
| NetworkPolicy `allow-byo-modelconfig-to-kube-apiserver` | Helm (shared with the Coordinator) | MODIFY (drop the Specialist selector) |
| A2A Gateway `x-kagent-host` routes `feature-rag-specialist` / `drift-specialist` | Helm (`a2a-gateway-config`) | MODIFY. The AGI-08 negative uses `drift-specialist.ewallet-agents` as its routed control and needs a new control |
| MCP credentials `mcp-token-<agent>-<server>` (+ grants in `llm-agent-mcp`) | `llm-agent-mcp` credential lifecycle | MODIFY (keep the `sandbox-*` subjects; retire the Deployment subjects in a new server set) |
| Registry Agent entries | registry | MODIFY (runtime `kagent-sandboxagent`; drop `byo-image-digest`) |
| Grafana / KEDA queries `agent="<agent>"` | chart | MODIFY (the in-flight series then come only from actors; see the metric strategy) |
| SandboxAgents, WorkerPool, Coordinator, gateways, MCP servers | — | KEEP |

**B′ verdicts:**

| Item | Finding |
|---|---|
| CRD compatibility | `workerpools.ate.dev` v1alpha1 has `/scale` (`spec.replicas`/`status.replicas`) but no `labelSelectorPath` and no `spec.scaleSelector`. The KEDA HPA needs PKG-P3 (`crd_patch.sh`), which is cluster-scoped, Helm `substrate-crds`, and reverted by any `substrate-crds` upgrade |
| KEDA watch | KEDA 2.21.0 `WATCH_NAMESPACE=ewallet-agents,ml-platform`, so PKG-W (`values-sandbox-watch.yaml`) is required |
| Target | `WorkerPool/ewallet-sandbox/business-agents`. The chart template `autoscaling.yaml` (sandbox mode) targets a per-agent WorkerPool in `ewallet-agents` and does **not** fit: a dedicated template or manifest for the shared pool is needed |
| Metric | Substrate 0.0.9 exports only `rpc_server_call_duration_seconds` (`ate-api :9090`) and controller-runtime metrics, with no pending, assigned or free-worker gauge, and Prometheus does not scrape `ate-system`. Actor-side `ewallet_agent_inflight_tasks` freezes at 1 on suspend. Recommended: Coordinator metrics `ewallet_sandbox_delegations_inflight` (gauge) and `ewallet_sandbox_admission_waits_total` (counter of `sandbox_capacity_exhausted`), from the always-scraped Coordinator pods |
| Ownership | The WorkerPool has no Helm owner. KEDA would own `spec.replicas` through `/scale`; nothing else writes it (direct `kubectl apply` of the fleet would reset replicas: exclude `replicas` from that manifest or let KEDA own it) |
| Scale-in safety | **Not proven, and defect 2 shows it is unsafe today.** A removed worker pod stays registered, and later actors assigned to it fail. Also unverified: drain of busy workers, suspension of active actors, snapshot consistency |
| cloud_lab gap | `keda_controllers()` only matches `scaleTargetRef.kind == Deployment`, and the WorkerPool has no PDB, so guarded sleep would neither find nor pause its ScaledObject |
| Capacity / cost | A worker requests 500m/1Gi (limit 1500m/2Gi). After a full wake the 4 e2-standard-4 nodes are 83–99% CPU-requested, so a 3rd worker needs about 500m free and probably a 5th node (about +$0.165/h; 4 nodes ≈ $0.66/h) |

**cloud_lab design for B′:**

- Discover ScaledObjects of any kind whose target has `/scale` (WorkerPool included).
- Record `spec.replicas`, pause at the baseline (`paused-replicas=<min>`), and refuse while actors are `Running`
  (status API).
- After the wake, unpause, then verify that the registry workers equal the live worker pods.

Tests: a fake cluster with a WorkerPool ScaledObject; pause, then failure recovery, then selective unpause; plus a
refusal while actors are active.

### Phase 2 plan (smallest safe sequence; nothing executed)

1. **Wake hygiene** (no B′ yet).
   - `atelet` priorityClass `system-node-critical` (Helm `substrate` values, owner approval).
   - Decide on the `ewallet-llm-gpu` leftovers (remove, or add them to the manifest).
   - Refresh the recovery manifest (MCP at 2).
   - Find a supported way to clear stale workers. Upstream Substrate fix, or a controlled resync: required before B′.
2. **kagent tracing:** endpoint scheme fix plus the kagent-controller→otel flow (two gated packages).
3. **Coordinator metrics:** the two Sandbox metrics. Image roll through `img-agents`.
4. **Remove the Deployment Specialists:**
   - Chart values change that drops the BYO Specialists.
   - A new AGI-08 control route.
   - Credential server-set rotation.
   - Registry republish.
   - Gate: exact diff (the matrix above), dry-run, P4 E2E.
5. **B′:**
   - PKG-W (KEDA watch), then PKG-P3 (CRD, cluster-scoped approval).
   - Add `scaleSelector` to `business-agents`.
   - A ScaledObject on the shared pool (min 2, max 3–4, Coordinator metrics).
   - The cloud_lab WorkerPool support above.
   - Validation on kind first, then GKE: 2→3→2 workers under the AGR-06 profile, actors placed on the new worker, no
     stale registrations after scale-in, success 100/100.
6. **Integrated acceptance**, then the owner replay.

Rollback boundaries: one Helm revision per package, the CRD backup (`crd_patch.sh backup`), and KEDA values from before
PKG-W.

## Final remediation `rem-20261116T050511Z` (2026-11-16): current state

Results: [AGENT_PLATFORM_COMPLETION.md §6](../../../edai2/agents/AGENT_PLATFORM_COMPLETION.md#6-final-remediation-rem-20261116t050511z).

- **Deployed:**
  - `llm-agent` rev 52, with 3 BYO images from `21994c03`.
  - Sandbox fleet image `29867794`.
  - Registry published; readback 8/8.
  - MCP `llm-agent-mcp` rev 17, unchanged.
- **GKE:** ASLEEP (`verify --expect sleep` passed). The agent ScaledObjects are **paused** with the cloud-lab marker,
  and `wake-full` unpauses them. No temporary authorized network remains.
- **Not done:**
  - B′.
  - Re-running MCP failover (A6 was not approved; that code is unchanged).
  - kagent controller tracing endpoint (kagent release config).
  - The runtime label for Sandbox in-flight metrics.

## Final integration `agi-20261115T145608Z` (2026-11-15/16): read this first

Results: [AGENT_PLATFORM_COMPLETION.md](../../../edai2/agents/AGENT_PLATFORM_COMPLETION.md). Owner replay:
[OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md](../../../edai2/agents/OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md), helper
`scripts/llm_agent/agent_replay.sh`. Raw evidence: `~/ewallet-evidence/agents/integration/agi-20261115T145608Z/` with
`SHA256SUMS.txt`, mirrored in `docs/evidence/edai2/agents/agi-20261115T145608Z/`.

- **Deployed:** P1, S1, P2 and P4, then `t1-agents`. Images were rolled from `fd2c49c9` with `img-mcp` / `img-agents`.
  Helm `llm-agent` rev 51 and `llm-agent-mcp` rev 17 at the final regression.
- **Committed, not deployed:**
  - `bb7a2a09`: Sandbox span flush.
  - `f6ea89b5`: the Coordinator waits for a free Sandbox worker.
  - Both need an `img-*` roll approval.
  - `b2cc5459`: registry readback. It is a tool; nothing to deploy.
- **Campaign env `SHA`:** it still names the deployed `891fd9c6` commit. Later commits touched
  `ops/llm_agent`, a gate image input, so the next gated package needs an image rebuild from HEAD first.

### Approvals the owner can give (none was used without approval)

1. **Agent Registry write.** The corrective publish passed a server-side dry-run. Command in the replay, section 2.
2. **Image roll for `bb7a2a09` and `f6ea89b5`.**
   - Rebuild the 4 images from HEAD (the build scripts are in `$EV/stage1/build-images-g.sh`).
   - Run `img-mcp`, then `img-agents`.
   - Re-capture a Tempo trace; Sandbox Specialist spans should then appear.
   - Rerun `agent_replay.sh keda`. It should complete all tasks, waiting for Sandbox workers instead of failing.
3. **B′ WorkerPool autoscaling.** Separate CRD approval (PKG-P3); out of scope.

### Sleep

The guarded sleep was approved for this campaign (`$EV/approvals.txt`):

```bash
M=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
CLOUD_LAB_APPROVED=yes CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:agi-20261115T145608Z \
  python3 ops/gcp/cloud_lab.py sleep --execute --confirm-cluster ewallet-dev \
  --scale-pdb-guarded --manifest "$M" --campaign agi-20261115T145608Z --also-approve ewallet-mcp/feature-rag-mcp
python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest "$M"
```

**Result 2026-11-16 ~22:10Z: `SLEEP_BLOCKED`.**

- KEDA restored the guarded agent Deployments to 2 after `cloud_lab` scaled them to 0, so the PDBs still protected
  pods and the tool did not resize.
- Nothing was bypassed. The MCP Deployments that the sleep had scaled to 0 were restored (2 in `ewallet-mcp`, 1 for
  `ewallet-agents/drift-mcp`), and a Sandbox drift E2E passed afterwards.
- The pool went back from the PKG-C 5 nodes to 4, and the temporary `/32` was removed.
- **The cluster is AWAKE at 4 nodes.** To sleep it, the owner approves pausing the 3 ScaledObjects
  (replay section 12), then runs the guarded sleep.

A later sleep needs the owner's own approval for that campaign id (replay section 12). The manifest records the MCP
Deployments at 1 replica, so after a wake run `kubectl -n ewallet-mcp scale deploy/feature-rag-mcp deploy/drift-mcp
--replicas=2`; that is the replica count Helm declares.

- Machine-readable state: [`checkpoints.json`](checkpoints.json).
- Images: [`images.json`](images.json).
- Commands and pass rules: the
  [master runbook](../../../runbooks/edai2/AGENT_PLATFORM_MASTER_EVIDENCE_RUNBOOK.md).

## Blocker resolution (2026-11-15, BR-00 to BR-10)

| ID | Blocker | Resolution | Tests | Runtime |
|---|---|---|---|---|
| B1 | `feast 0.66.0`, CVE-2026-18948 (unsafe dill UDF deserialization), blocked `mcp-tools` | `feast[redis]==0.67.0` in `infra/docker/llm-agent-tooling` only; the lock moves `feast` alone. `feast_cross_version_check.sh`: the 0.66.0 feature repo publishes, the 0.67.0 MCP code reads 4 views, 31 features and 8 RAG fields, all equal. | Cross-version check PASS; Feast, MCP and RAG suites; lock contract | Image published, gate PASS; not deployed |
| B2 | kagent does not watch `ewallet-sandbox` | PKG-K: `controller.watchNamespaces` + `rbac.namespaces` (the kagent 0.10.3 keys) on the existing release, with the pilot post-renderer. Chart + live values + post-renderer reproduce the live release (29/29 objects). The diff adds 6 RBAC objects in `ewallet-sandbox` and changes `WATCH_NAMESPACES`. | `sandbox_watch_kind.sh`: `PACKAGES_VALIDATED`; unit tests | `APPROVAL_PENDING` |
| B3 | MCP experiment unschedulable on 4 nodes | `mcp_capacity.py` preflight. `run` stops with `CAPACITY_INSUFFICIENT` before creating anything. The optimized arm needs 2 distinct non-LLM nodes; the PKG-C plan is a 5-node wake. | Capacity and simulator tests | `APPROVAL_PENDING` (PKG-C); not proven at runtime |
| B4 | KEDA does not watch `ewallet-sandbox` | PKG-W: `values-sandbox-watch.yaml` on the existing release. Identity matches live; the diff adds 1 RoleBinding and changes `WATCH_NAMESPACE` on 3 Deployments. | `PACKAGES_VALIDATED` on kind | `APPROVAL_PENDING` (with PKG-P3 only) |
| B5 | P1–P4 prerequisites | `agi_campaign.sh` `AGI_PACKAGE` = `p1-mcp`, `s1-mcp`, `s1-agents`, `p2-agents`, `p4-agents`: exact identities, separate tokens, P2 rollback order, `IMAGE_SHA`. The fleet's Secret names now match `g3_mcp_credentials`. | Gate tests (25) | `APPROVAL_PENDING` per package |
| B6 | Guarded sleep used without its approval | `--scale-pdb-guarded` now needs `--manifest`, `--campaign` and `CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:<campaign>`. It scales only PDBs covered by the manifest or `--also-approve`, and resizes only once they are released. Otherwise `SLEEP_BLOCKED`. | 44 cloud_lab tests | Not run on GKE (by design) |
| B7 | Runbook out of date | Rewritten: PKG-0…P4 and PKG-C, a fresh-terminal setup, A+S kept apart from B′. A consistency test checks paths, packages, tokens, flags and namespaces. | `test_master_runbook.py` | Owner execution |

Two real defects were found and fixed along the way:

- The fleet referenced Secret names and a key that the credential tool never writes.
- A static S1 flow overlay would have deleted the 16 live cross-namespace policies,
  because Helm replaces lists.

## Approvals the owner can give, one at a time

| Package | Approves | Unlocks |
|---|---|---|
| PKG-K | `WATCH_APPROVAL=PKG-K:<context>`: kagent also watches `ewallet-sandbox` | AGS-01, 02, 04, 05, 06 (A+S, S0) |
| PKG-C | A 5-node `wake-full` for the AGR-02 window (+$0.18/h) | AGR-02 |
| PKG-P1 | `AGI_APPROVAL=P1-MCP:<campaign>` | AGR-01, AGR-03; AGI MCP side |
| PKG-S1 | Sandbox credentials, the copy of 2 client Secrets, `S1-MCP` and `S1-AGENTS`, ambient label | AGS-03; tool results in AGS-04 |
| PKG-P2 | `AGI_APPROVAL=P2-AGENTS:<campaign>` | AGI-01–14, AGR-04–07 |
| PKG-P4 | `AGI_APPROVAL=P4-AGENTS:<campaign>` | AGI-09–11-sbx |
| PKG-W + PKG-P3 | KEDA watch, then the CRD selector patch | AGS-07 (B′) |
| Guarded sleep | `CLOUD_LAB_GUARDED_SLEEP_APPROVAL=ewallet-dev:<campaign>` (+ `--also-approve ewallet-mcp/feature-rag-mcp` after P1) | Sleep when PDBs block it |

## Future report outlines (no final docs yet)

- **`docs/edai2/agents/AGENTS_REGISTRY_GATEWAYS.md`:**
  - agents and runtimes;
  - Coordinator routing (fraud, drift, combined);
  - the Feature-RAG and drift Specialists;
  - the global ModelConfig;
  - MCP and A2A Gateway policies, including the sandbox routes and flow;
  - the Registry;
  - AGI-01–14.
- **`docs/edai2/agents/AGENT_RELIABILITY_AUTOSCALING.md`:**
  - MCP replicas, RollingUpdate and client failover (AGR-01–03), with the capacity
    analysis;
  - KEDA's in-flight trigger, Prometheus, scale-out and scale-in, drain (AGR-04–07);
  - limits.
- **`docs/edai2/agents/AGENT_SANDBOX_SUBSTRATE.md`:**
  - SandboxAgents and gVisor isolation;
  - the shared WorkerPool and actor assignment;
  - cold against warm starts;
  - the watch-scope packages;
  - B′ (the kind result, the GKE status);
  - the fixed pool against the KEDA-scaled pool (AGS-01–07).

## GKE runs

- **`agi-20261114t221240z`** (evidence in `gke-agi-20261114t221240z/`). Awake
  22:17–22:38Z on 4 nodes, about $0.24.
  - Recorded live facts: the kagent and KEDA watch scopes, the WorkerPool `/scale`
    without a selector, and a server-side dry-run of the CRD patch (accepted).
  - CP6 was blocked by capacity.
  - Sleep is recorded as `SLEEP_VERIFIED`, but it used `--scale-pdb-guarded` without
    that flag's separate approval (B6).
- **BR (2026-11-15).** The cluster was not woken. Only read-only Helm reads were made
  against the control plane (kagent and KEDA values, manifests and status) to build
  the exact diffs.

## Stage PKG-K and Sandbox S0 (`agi-20261115T054518Z`, evidence in `gke-agi-20261115T054518Z/`)

- **Wake:** `wake-experiment --profile agent-sandbox --nodes 3`. The cluster went 0 → 3 → 0
  nodes and was awake from 05:47 to 06:23Z, about $0.30.
  - The profile held 34 workloads outside it at 0 (listed in `wake/wake.txt`).
  - The five PDB-guarded Deployments stayed at 0.
  - Running outside the profile, because they are not in the 2026-11-06 manifest:
    `ewallet-llm-gpu` ×4 and `keda` ×3. `ml-platform/feature-api` also ran, held at 1 by
    its ScaledObject.
- **PKG-K: `GKE_TESTED`, kept.**
  - Revision 5 → 6.
  - Identity PASS; exact diff (+6 RBAC, `WATCH_NAMESPACES`); server-side dry-run PASS.
  - After the upgrade, the live manifest equals the reviewed candidate.
  - The controller logs `["ewallet-agents","kagent-pilot","ewallet-sandbox"]`.
  - The pilot SandboxAgent is still Ready.
- **S0: `BLOCKED`.**
  - Observed working:
    - 2 warm gVisor workers, Ready, on 2 nodes;
    - both SandboxAgents `Accepted`;
    - both ActorTemplates created, with `sandboxClass gvisor` and the shared worker
      selector;
    - Substrate assigned one golden actor to each worker.
  - The golden snapshots never formed. atelet pulls the actor image itself and
    anonymously (`--gcp-auth-for-image-pulls=false`), so Artifact Registry answered
    `DENIED: Unauthenticated request`. The pilot works only because its image is public.
- **AGS-01:** PARTIAL.
- **AGS-02:** observed read-only. Workers run `ateom-gvisor`; `ewallet-sandbox/default`
  mounts no token and has discovery-only RBAC; `allowedDomains` are set. The egress
  denial was not exercised.
- **AGS-04:** BLOCKED. Without a golden snapshot, no session can create an actor.
- **Cleanup:**
  - Fleet, placeholder Secrets, ActorTemplates and the WorkerPool were removed.
  - The namespace and the PKG-K RBAC were kept.
  - Two Suspended golden-actor records remain in ate-api, with no pod and no snapshot.
- **Sleep:** `SAFE_TO_SLEEP`, then a plain `sleep`: `SLEEP_VERIFIED`.

### New owner decision: PKG-R (Artifact Registry pull for Substrate)

1. `substrate` release (`ate-system`): `atelet.gcpAuthForImagePulls=true`
   (`infra/kubernetes/kagent-pilot/values/substrate-ar-pull-overlay.yaml`). The atelet
   DaemonSet rolls.
2. IAM: `roles/artifactregistry.reader` on repository `ewallet-platform` for
   `principal://iam.googleapis.com/projects/198818809719/locations/global/workloadIdentityPools/ewallet-data-ai-platform-hg26.svc.id.goog/subject/ns/ate-system/sa/atelet`.

Offline, the overlay changes only atelet's `args[0]`. A live identity gate for the
`substrate` release, in the style of `sandbox_watch.sh`, is still to be written before
PKG-R runs.

### Screenshot views for the owner's rerun (after PKG-R)

```bash
kubectl -n ewallet-sandbox get workerpool,sandboxagent,actortemplate -o wide            # AGS-01_shared_workerpool.png
kubectl -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o wide
kubectl -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents \
  -o custom-columns=POD:.metadata.name,SA:.spec.serviceAccountName,IMAGE:.spec.containers[0].image   # AGS-02_sandbox_isolation.png
kubectl auth can-i --list --as=system:serviceaccount:ewallet-sandbox:default -n ewallet-sandbox
python3 -m ops.experiments.substrate_fleet summarize "$EV/ags/AGS-04-status.jsonl"     # AGS-04_actor_worker_assignment.png
curl -s "http://127.0.0.1:18383/api/substrate/status?namespace=ewallet-sandbox" | python3 -m json.tool
```

## PKG-R readiness (2026-11-15, no cloud mutation; evidence in `pkg-r-readiness-20261115/`: substrate-gates.txt, substrate-diff.json, iam-plan.txt)

- **Authentication path, read from the live configuration and the pinned source:**
  - atelet runs as `ate-system/atelet`, without `hostNetwork` and without a GSA annotation.
  - The cluster pool is `ewallet-data-ai-platform-hg26.svc.id.goog` and `cpu-pool` uses
    `GKE_METADATA`.
  - The flag makes atelet call `googauth.DefaultTokenSource` once at start-up. That
    resolves to the federated principal of the ServiceAccount, and a failure is fatal.
- **PKG-R Helm** (`scripts/llm_agent/substrate_pull_auth.sh`), run read-only against release
  `substrate` revision 2:
  - The lookup-render identity check PASSES. CA, TLS and session pools are read back;
    nothing is suppressed.
  - The diff is DaemonSet `atelet` `args[0]` only.
  - The server-side dry-run PASSES.
- **PKG-R-IAM** (`scripts/llm_agent/substrate_pull_iam.sh`): grants
  `roles/artifactregistry.reader` on repository `ewallet-platform` to the principal above.
  Currently absent. It adds or removes exactly one pair and checks that every other
  binding is unchanged.
- **Residue** (`scripts/llm_agent/substrate_golden_residue.sh`, `golden-residue.json`): the
  cause is Substrate 0.0.9 not deleting golden actors. `kubectl-ate` builds from the
  pinned source (`scripts/llm_agent/build_kubectl_ate.sh`, sha256 `f65ab2f7…`). Listing
  needs a running ate-api.
- **Next campaign:** the sequence is in section 2.3a of the master runbook.

Approvals needed, one at a time:

1. `IAM_APPROVAL=PKG-R-IAM:ewallet-data-ai-platform-hg26`.
2. `PKG_R_APPROVAL=PKG-R:<context>`.
3. The `agent-sandbox` wake on 3 nodes.
4. Optionally `RESIDUE_APPROVAL=GOLDEN-RESIDUE:<context>`.

## Stage PKG-R and Sandbox S0 (`agi-20261115T064905Z`, evidence in `gke-agi-20261115T064905Z/`)

- **PKG-R-IAM: kept.** One `roles/artifactregistry.reader` binding on `ewallet-platform`
  for `ns/ate-system/sa/atelet`. The only other binding (Cloud Build writer) is unchanged.
- **PKG-R: `substrate` 2 → 3, kept.**
  - Identity PASS (lookup render); diff atelet `args[0]` only; server-side dry-run PASS.
  - After the upgrade, the live release equals the reviewed candidate.
  - 3/3 atelets rolled out with 0 restarts. A failed token request at start-up would
    have been fatal, so this shows Workload Identity works. The pilot is still Ready.
- **Private actor image pulls: PASS.** Both ActorTemplates reached Ready with golden
  snapshots at 07:08Z. There were 0 pull, DENIED or Unauthenticated errors. The 32
  transient `Unavailable` dial errors (07:00–07:04) came while atelets and workers
  restarted.
- **AGS-01:** both SandboxAgents `Accepted` and `Ready`, both templates Ready on
  `business-agents`, 2 warm gVisor workers.
- **AGS-02:** partial. gVisor; `default` SA with no token and discovery-only RBAC;
  `allowedDomains` set. There is no NetworkPolicy in `ewallet-sandbox` and no egress probe
  was run, so network isolation is not verified.
- **AGS-04: `SHARED_WORKERS_OBSERVED`, golden actors excluded.**
  - 14 session actors on 2 workers. Each worker hosted 3 distinct session actors of both
    agents, at most 1 at a time.
  - Suspension was observed.
  - **Resumption failed:** `session actor … not reachable via atenet-router: context deadline
    exceeded` after 120 s, on every resume.
  - Business tool calls failed with `mcp_transport_error`: the MCP Gateway is held at 0
    by the profile, and S1 is not deployed.
- **Defect found and fixed:** kagent serves SandboxAgents at
  `/api/a2a-sandboxes/<ns>/<name>`, and `/api/a2a/` returned 404. The Coordinator, the
  Gateway routes, the runbook and the tests are fixed.
  - `ai_platform` changed, so **the four images must be rebuilt (PKG-0) before P1, P2
    or P4**.
- **Residue (not deleted, `golden-residue.json`):** 3 golden actors in `ate-golden`
  (2 old, 1 new) and 4 session actors in `ewallet-sandbox`. All Suspended, none with a pod.
- **Cloud:** 0 → 3 → 0 nodes, about $0.50; plain sleep, `SLEEP_VERIFIED`.

### Owner screenshot views (rerun with the same sequence, runbook section 2.3a)

```bash
scripts/llm_agent/substrate_pull_iam.sh plan                                          # IAM binding
kubectl -n ate-system get ds atelet -o jsonpath='{.spec.template.spec.containers[0].args[0]}'; kubectl -n ate-system get pods -l app=atelet
kubectl -n ewallet-sandbox get workerpool,sandboxagent,actortemplate -o wide            # AGS-01_shared_workerpool.png
kubectl -n ewallet-sandbox get actortemplate -o jsonpath='{range .items[*]}{.metadata.name} {.status.phase} {.status.goldenSnapshot}{"\n"}{end}'
kubectl -n ewallet-sandbox get pods -l ate.dev/worker-pool=business-agents -o custom-columns=POD:.metadata.name,SA:.spec.serviceAccountName,IMAGE:.spec.containers[0].image   # AGS-02
python3 -m ops.experiments.substrate_fleet summarize "$EV/ags/AGS-04-status.jsonl"     # AGS-04_actor_worker_assignment.png
```

## Resume root cause (2026-11-15, no cloud action; `resume-rca/RCA.md`)

- **Root cause (`ROOT_CAUSE_CONFIRMED`):** kagent 0.10.3 suspends a session actor
  asynchronously after each turn. `EnsureSessionActor` skips `ResumeActor` for a SUSPENDING
  actor. A follow-up sent within that window therefore waits 120 s and fails with
  `not reachable via atenet-router`.
  - GKE timeline: turn 2 was sent at 07:15:03, the actor was Suspended at 07:15:04, and it
    was never Resuming.
  - Reproduced deterministically on unmodified v0.10.3.
- **Fix in this repository (`FIX_IMPLEMENTED`, `UNIT_TESTED`):** `sandbox_sessions.py` waits,
  bounded, until the session's actor is `Suspended` before each follow-up. `--no-settle`
  reproduces the failure.
  - The production Coordinator sends one message per member context, so it is not affected.
- **Upstream patch (proposed):** `resume-rca/kagent-0.10.3-resume-during-suspend.patch`. All
  56 package tests pass. Not deployed: that needs a kagent image and upgrade approval.
- **Images:** rebuilt from `f2583641` for the `/api/a2a-sandboxes` fix. Trivy PASS 4/4,
  pushed, not deployed (`images.json`).
- **GKE A/B:** done on 2026-11-15; see the next section.

## Resume A/B on GKE (`agi-20261115T084916Z`, evidence in `gke-agi-20261115T084916Z/`)

- **Run:** `agent-sandbox` profile, 3 nodes, awake 08:51–09:27 UTC.
- **Sleep:** normal sleep, `SLEEP_VERIFIED` (cpu-pool 0 VMs). Cost about $0.40 at list
  price.
- **A, `--no-settle`:** the race reproduced. The follow-up got `rpc_error` after 120.5 s,
  and the actor stayed Suspended.
- **B2, paced (drift, 1 × 4): `RESUME_GKE_TESTED_PASS`.**
  - Actor `asr-62efd060a1d79057ff6018c4` resumed 3 times on the same ID.
  - Every restore was clean and every message reached the agent; no `rpc_error`.
  - This is a lifecycle result only: under S0 each task ended `failed`
    (`mcp_transport_error`).
- **B3:** the same worker then served feature-rag, so one worker served both agents
  (sharing holds).
- **B as first specified (both agents at once): failed, not the race.**
  - Two concurrent first-turn restores on one node were cancelled at kagent's 30 s
    `--substrate-call-timeout`. The actors were left `RESUMING`, both workers were held,
    and every later request got `no free workers`.
  - Recovery: the campaign worker pods were restarted; nothing was deleted.
- **Not fixed upstream:** the unmodified kagent controller is still vulnerable to unpaced
  multi-turn requests. Only the client-side pacing is verified.
- **Open, each needing an owner decision:**
  1. Raise `--substrate-call-timeout`, a shared kagent controller change. A single restore
     takes about 24–25 s, leaving about 5 s of headroom.
  2. Add worker anti-affinity to the WorkerPool.
  3. Upgrade kagent with the proposed patch.
  4. Delete the residual actors (`RESIDUE_APPROVAL`); they are recorded read-only.
  - Details: `resume-rca/RCA.md` section 7.

## Runtime stability PKG-T (`rts-20261115T114235Z`, evidence in `gke-rts-20261115T114235Z/`)

- **Verdict: `RUNTIME_STABILITY_GKE_PASS`.** The fix is in our pinned builds of kagent
  v0.10.3 and Substrate v0.0.9; upstream is unchanged. See `resume-rca/RCA.md` section 8.
- **Live (kept):**
  - kagent revision 8: patched controller (session turns and capacity wait),
    `SUBSTRATE_CALL_TIMEOUT=90s`.
  - Substrate revision 5: patched ate-api (detached workflows and restore rollback), atelet
    memory 512Mi:1536Mi.
  - The campaign WorkerPool uses the patched ateom; `kagent-pilot` keeps the release image.
  - Rollback: `runtime_fix.sh <target> rollback` returns kagent to revision 6 and Substrate
    to revision 3.
- **Found and fixed on GKE:**
  - F6: atelet was OOM-killed by its in-memory pull cache.
  - F7: `no free workers` while another session's suspend was releasing its worker.
- **Remaining:**
  - Single kagent replica.
  - A controller crash mid-turn beyond the 30 s grace can leave a RUNNING actor until that
    session's next turn.
  - The atelet, atecontroller and atenet release images still fail the Trivy gate.
  - The gateway's 150 s budget against a 90 s resume plus a 120 s business turn.
- **Next session:** PKG-P1, S1, P2, P4 and B′, on this runtime.
