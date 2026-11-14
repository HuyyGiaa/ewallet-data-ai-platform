# Agent platform completion: campaign handoff

Machine-readable state: [`checkpoints.json`](checkpoints.json). Commands and pass
rules: [master runbook](../../../runbooks/edai2/AGENT_PLATFORM_MASTER_EVIDENCE_RUNBOOK.md).

## Where things stand

| Area | Status | Notes |
|---|---|---|
| S1 Coordinator → sandbox Specialist delegation | LOCAL_TESTED | `contextId` = member request ID; per-agent controller route |
| S2 A2A Gateway sandbox routes | LOCAL_TESTED | exact routes; Gateway sets `X-User-Id`; agentgateway v1.7.0 validates the config |
| S3 Shared WorkerPool fleet (2 Specialists, 1 pool, 2 workers) | LOCAL_TESTED | `render_sandbox.py fleet` |
| S4 WorkerPool ScaledObject guard | LOCAL_TESTED | refuses to render without the selector patch |
| S5 B′ CRD selector patch package | LOCAL_TESTED (kind) | CP2 `CONTROL_LOOP_VALIDATED`; GKE apply needs P3 |
| S6 KEDA on all three BYO agents, 25 s drain | LOCAL_TESTED | GKE needs P2 |
| S7 Collector series expiry 60 s | LOCAL_TESTED | |
| S8 Evidence tooling | LOCAL_TESTED | `substrate_fleet.py`, drift load fixture; reuses `sandbox_client`, `replica_sampler`, `agent_load` |
| S9 Master runbook | SOURCE_IMPLEMENTED | |

## Owner approvals still needed

| Gate | What | Unlocks |
|---|---|---|
| P1 | `llm-agent-mcp`: 2 replicas + PDB, sandbox MCP token entries | AGR-01, AGR-03, AGS-03 |
| P2 | `llm-agent`: `agi/agents.yaml` + `agi/keda.yaml` (AGI A1) | AGI-01–14, AGR-04–07 |
| P3 | WorkerPool CRD selector patch on GKE + KEDA `watchNamespace` += `ewallet-sandbox` | AGS-07 (KEDA-scaled shared pool) |
| P4 | Coordinator sandbox route | AGI-09–11 through sandbox Specialists |
| kagent | `watchNamespaces` += `ewallet-sandbox` (kagent upgrade; live value lacks it) | AGS-01, AGS-02, AGS-04–06 |
| Feast | `feast` 0.66.0 → 0.67.0 in the MCP image (CRITICAL CVE-2026-18948) | a gate-passing `mcp-tools` image (P1, CP6) |
| Capacity | a 5th node, or 2 non-LLM nodes, during the AGR runs | AGR-02 (CP6) |

## Future report outlines (no final docs yet)

- `docs/edai2/agents/AGENTS_REGISTRY_GATEWAYS.md`: agents and runtimes; Coordinator
  routing (fraud, drift, combined); Feature-RAG and drift Specialists; global
  ModelConfig; MCP and A2A Gateway policies (sandbox routes); Registry; AGI-01–14.
- `docs/edai2/agents/AGENT_RELIABILITY_AUTOSCALING.md`: MCP replicas, RollingUpdate,
  client failover (AGR-01–03); KEDA in-flight trigger, Prometheus, scale-out and
  scale-in, drain (AGR-04–07); limits.
- `docs/edai2/agents/AGENT_SANDBOX_SUBSTRATE.md`: SandboxAgents, gVisor isolation,
  shared WorkerPool and actor assignment, cold against warm, the B′ selector patch
  (kind result and GKE status), fixed pool against KEDA-scaled pool (AGS-01–07).

## GKE run `agi-20261114t221240z` (evidence in `gke-agi-20261114t221240z/`)

- **Images (CP3), source `7d695365`:** `g3-byo-coordinator@sha256:0cc25968…`,
  `g3-byo-feature-rag-specialist@sha256:b8079aab…` and
  `g3-byo-drift-specialist@sha256:75185dbd…` passed the Trivy gate and were pushed.
  `mcp-tools` failed the gate on `feast 0.66.0`: CVE-2026-18948, CRITICAL, fixed in 0.67.0.
  It was not pushed. The fix is a Feast upgrade, which is an owner decision.
- **Live facts:** kagent watches `ewallet-agents,kagent-pilot`; KEDA watches
  `ewallet-agents,ml-platform`. The WorkerPool `/scale` has no `labelSelectorPath`, so B′
  is needed on GKE as well. The server-side dry-run of the selector patch was accepted;
  nothing was applied.
- **CP4 is `APPROVAL_PENDING`:** `ewallet-sandbox` is not watched by kagent, so it does not
  reconcile SandboxAgents there. Adding the namespace is a kagent upgrade.
- **CP6 is `BLOCKED` (capacity):**
  - The experiment pods must avoid `ewallet.io/g3-llm` nodes.
  - 3 of the 4 woken nodes carry that label, and the 4th was at 97% CPU requested. The
    optimized arm also needs two such hosts.
  - Both arms timed out at rollout and removed their namespace. Production `ewallet-mcp`
    was identical before and after.
  - Rerunning needs a 5th node or a wake profile with 2 non-LLM nodes (owner).
- **Sleep: `SLEEP_VERIFIED`.** The cluster was awake from 22:17 to 22:38Z on 4 nodes,
  about $0.24. `sleep` needed `--scale-pdb-guarded`, which returned the five PDB-guarded
  Deployments to the 0 replicas recorded in the 2026-11-06 sleep manifest. No
  experimental objects, Secrets, snapshots or port-forwards remain.
