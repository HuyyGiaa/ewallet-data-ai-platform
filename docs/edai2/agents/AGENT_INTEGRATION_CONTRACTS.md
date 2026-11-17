# Agent platform integration: contract map

> **Status: TARGET ARCHITECTURE — SOURCE_IMPLEMENTED, LOCAL_TESTED, CLOUD_PENDING.**
> Nothing here has run on GKE yet. Runtime proof is collected with
> [AGENT_INTEGRATION_EVIDENCE_RUNBOOK.md](../../runbooks/edai2/AGENT_INTEGRATION_EVIDENCE_RUNBOOK.md).
> The deployed system is still described by
> [AGENTS_REGISTRY_GATEWAYS.md](AGENTS_REGISTRY_GATEWAYS.md); that report is
> updated only after the evidence campaign.

This document maps the integration of the existing agents with the LLM
platform, the global ModelConfig, the Feature Store, the RAG publication, the
drift tool and the MCP/A2A gateways. Module documentation stays authoritative:
[LLM_PLATFORM.md](../llm/LLM_PLATFORM.md), [FEATURE_STORE.md](../mlops/FEATURE_STORE.md),
[RAG_PIPELINE.md](../rag/RAG_PIPELINE.md).

## 1. Starting point (audit of `06eb47a7`)

| Area | Found in source | Gap closed here |
|---|---|---|
| Coordinator | BYO, deterministic delegation of one typed `G3BusinessRequest` to `feature-rag-specialist`; NL intake with one LLM tool, `investigate_transaction` | No typed drift request, no combined workflow, no drift routing |
| Feature-RAG Specialist | BYO, `feature_lookup` + `fraud_scoring` + `knowledge_search` through the MCP Gateway | — (unchanged contract; MCP client pieces moved to `byo_mcp.py`) |
| Drift Specialist | Declarative kagent agent (LLM chooses tool arguments); its free-text answer cannot satisfy the Coordinator's strict evidence contract | Deterministic BYO Specialist |
| `drift-mcp` | PSI implemented, but `create_drift_app()` wired `UnavailableBaselineStore` / `UnavailableWindowSampler` | Artifact-backed baseline registry and window sampler |
| `feature-rag-mcp` | `_rag_feature_store()` reused the Fraud store's Redis connection | Independent RAG connection; `isolated` profile with pinned databases |
| ModelConfig | BYO resolver in source and chart (`modelConfig.byoResolver`), off in production | Enabled in the AGI profile only; drift BYO has no LLM |
| Gateways | A2A routes for both Specialists by `x-kagent-host`; MCP routes `/feature-rag/mcp`, `/drift/mcp` | Unchanged |

## 2. Target architecture

```text
User → kagent UI → A2A Gateway → fraud-coordinator (BYO)
   fraud-coordinator ── one tool-selection call ──► LLM Gateway (agentgateway) → vLLM   [ModelConfig ewallet-global-llm]
   fraud-coordinator ── x-kagent-host ──► A2A Gateway ──► feature-rag-specialist (BYO, no LLM on the typed path)
                                                     └──► drift-specialist (BYO, never an LLM)
   feature-rag-specialist ── /feature-rag/mcp ──► MCP Gateway ──► feature-rag-mcp
        feature_lookup → Feast Online (Fraud target)   fraud_scoring → Feast + KServe V2   knowledge_search → Feast Online (RAG target)
   drift-specialist ── /drift/mcp ──► MCP Gateway ──► drift-mcp
        drift_detection → baseline registry (JSON) + window extract (gzip CSV) → PSI
```

The three gateways are separate agentgateway workloads; no gateway was added.
The ModelConfig is configuration read at startup, not a network hop.

## 3. Contracts

### 3.1 Coordinator inputs (`business_request.py`)

| Contract | Wire | Target | Business arguments |
|---|---|---|---|
| `g3-business-request/v1` (unchanged) | `CALL_AGENT=feature-rag-specialist` + `REQUEST_JSON=` | Feature-RAG | transaction, feature projection, optional policy query |
| `g3-drift-request/v1` (new) | `CALL_AGENT=drift-specialist` + `REQUEST_JSON=` | Drift | `feature_name` (monitored input), `baseline_id` (immutable; `latest`/`current`/`active` refused), `current_window` (aware, ordered, ≤ 31 days) |
| `g3-workflow-request/v1` (new, Coordinator only) | `CALL_WORKFLOW=combined` + `REQUEST_JSON=` | both | exactly one fraud member (`<id>.fraud`) and one drift member (`<id>.drift`); 4 KiB budget |

Each request is canonical JSON; the A2A `id` and `messageId` must equal the
`request_id`. Specialists accept only their own contract
(`business_request_from_a2a`, `drift_request_from_a2a`).

### 3.2 Natural-language routing (`workflow_intake.py`, opt-in)

`EWALLET_COORDINATOR_WORKFLOWS=fraud,drift` offers the LLM two tools
(`investigate_transaction`, `analyze_feature_drift`) in **one** call, at most
one call per tool. A deterministic router then decides:

- drift arguments must be literally present and labelled in the question
  (exactly one monitored feature, `baseline <id>`, `window <ISO> to <ISO>`); the
  LLM's values must equal them;
- an explicit drift question whose arguments are complete is completed
  deterministically when the LLM omitted the drift call (`selection_source:
  deterministic`, audited);
- transaction identifiers without the fraud selection are rejected
  (`incomplete_tool_selection`), never silently dropped;
- fraud arguments use the unchanged N11 grounding on the question with the
  window removed, so a window timestamp cannot become the event timestamp.

The default (`fraud`) keeps the N11 intake byte-for-byte; the Feature-RAG
Specialist keeps its optional intake. Delegation never calls the LLM again.

### 3.3 Coordinator results (`programmatic_coordinator.py`)

- Fraud only: unchanged `g3-orchestration/v1`.
- Drift or combined: `g3-orchestration/v2` with `workflow`, `status`
  (`complete`, `partial`, `unavailable`), `specialists.<agent>` (each with its
  own `request_id`, `status`, evidence and `duration_ms`) and `unavailable`
  entries naming the agent and code. Combined members run concurrently, each in
  a copy of the trace context.
- A failed member is recorded, never reconstructed; if every member fails, the
  task fails. Idempotency covers the whole workflow (fingerprint of the canonical
  request; subject = transaction user, or `drift:<baseline>:<feature>`).
- Drift evidence is re-validated by `drift_evidence.DriftFinding`: identity
  (feature, baseline, window), finite PSI equal to the sum of bin contributions,
  classification consistent with the baseline thresholds.

### 3.4 Drift MCP (`tools/drift_sources.py`, `ops/llm_agent/drift_artifacts.py`)

| Artifact | Schema | Pinned by |
|---|---|---|
| Baseline registry | `ewallet-drift-baselines/v1`: per baseline `baseline_id`, `feature_name`, `method psi`, cut points, reference proportions, smoothing, `min_current_count`, reference window, optional thresholds, `reference_count`, `source_sha256`; document `dataset_revision`, `source` (snapshot URI, export manifest digest), `synthetic` | `EWALLET_DRIFT_BASELINES_SHA256` |
| Window extract | `ewallet-drift-window/v1`: `dataset_revision`, coverage `[start, end)`, sampling (all or systematic stride), files with SHA-256, rows, features | `EWALLET_DRIFT_WINDOW_MANIFEST_SHA256` (manifest pins each file) |

Fail-closed rules: unknown baseline or baseline of another feature → `rejected`;
window overlapping the reference window or outside coverage → `rejected`;
unsupported feature → `rejected`; fewer observations than the minimum →
`insufficient_data` with no PSI; no thresholds → `unclassified`; wrong digest,
dataset revision or a synthetic artifact without `EWALLET_DRIFT_ALLOW_SYNTHETIC`
→ `unavailable`. Without `EWALLET_DRIFT_SOURCE=artifact` the recorded
unavailable stubs stay wired.

### 3.5 Feature-RAG MCP targets (`tools/service.py`)

`EWALLET_FEATURE_TARGET_PROFILE=legacy` (default) is the recorded wiring.
`isolated` requires:

- two Secrets (`EWALLET_REDIS_CONNECTION_STRING`, `EWALLET_RAG_REDIS_CONNECTION_STRING`)
  with explicit databases equal to `EWALLET_FRAUD_ONLINE_DB` and `EWALLET_RAG_ONLINE_DB`;
- **different** databases for the two targets;
- explicit registries;
- a RAG verification project `ewallet_rag_<suffix>` in DB 2–15;
- for the production Fraud store (DB 0), the explicit opt-in
  `EWALLET_FRAUD_ALLOW_PRODUCTION_DB=true`.

`/v1/capabilities` reports the non-secret target identity: databases, `store`
(production or verification), projects, registries, model, corpus and
embedding revisions, and the **access guarantee**:

| Layer | Guarantee |
|---|---|
| Application | The MCP server issues only Feast online reads (`get_online_features`). |
| Redis credential | **Not restricted.** The Feature Store Redis (`infra/kubernetes/feast-online/redis.yaml`) authenticates one default user with `requirepass` and has no ACLs. The AGI Secrets derived by `ops/llm_agent/agi_inputs.py` select a database but keep the same privileges. |

A Redis-level read-only user (ACL `+@read` on the Feast key pattern) would need
a change to the production Redis and is out of scope. The campaign instead
verifies afterwards that nothing changed (runbook Stage 5):

- DB 0–15 key counts and ownership markers;
- a `DUMP` digest of DB 3, via `rag_runtime_run.sh digest 3`;
- a bounded readback of the reference transaction's DB 0 features.

DB 0 (about 173k keys) is not digested in full.

### 3.6 Gateways and identities

| Hop | Route | Authorization |
|---|---|---|
| Coordinator → Specialist | A2A Gateway, `x-kagent-host: <agent>.ewallet-agents` (exact routes only) | Istio Ambient caller principal `fraud-coordinator` |
| Specialist → MCP server | MCP Gateway `/feature-rag/mcp`, `/drift/mcp` | Ambient caller principal + per-tool grants of the service-token subject (`drift-specialist: [drift_detection]`) |
| Agent → LLM | agentgateway `:3000/v1` | gateway client key from the ModelConfig's Secret reference |

The BYO Drift Specialist uses the existing `drift-specialist` ServiceAccount and
`mcp-token-drift-specialist-drift-mcp` Secret; no grant, route or policy was widened.

Two server-side MCP mechanisms are tested negatively in AGI-08, each with a
control that proves the denial came from that mechanism:

- **Per-server credentials.** The token table is per MCP server, so another
  server's credential gets HTTP 401 at `initialize`.
- **Per-tool grants.** An authenticated principal calling a tool outside its
  grant gets JSON-RPC `-32602 "Unknown tool"`, the same error as for an unknown
  tool, so tools cannot be enumerated.

`scripts/llm_agent/agi_probe.py` classifies every other failure as FAIL
(invalid arguments, backend, protocol, HTTP 5xx, transport, timeout).

### 3.7 Deployment gates (`scripts/llm_agent/agi_campaign.sh`)

| Stage | Command | Mutates |
|---|---|---|
| 0 | `preflight` (15 checks, each in a fresh `set -euo pipefail` process) → `stage0/PREFLIGHT_PASS`; `data-state before` | no |
| 1 | `gate mcp\|agents`: render → `helm_object_diff.py --expect-identities` (exact changed/added/removed identities) → `helm upgrade --dry-run=server` | no |
| 1 (A1) | `upgrade mcp\|agents`: requires `PREFLIGHT_PASS` for the same campaign, commit and context, `AGI_APPROVAL=A1:<campaign>`, an unchanged live release and the MCP release first; re-runs the gate, then `helm upgrade --atomic` | yes |
| 2 | `negative` (AGI-08) | no |
| 5 | `data-state after`; `rollback` (A2: `AGI_APPROVAL=A2:<campaign>`, back to the Stage 0 revision, then a 0-object diff against the Stage 0 manifest) | rollback only |

### 3.8 Observability

| Span / record | Attributes |
|---|---|
| `g3.coordinator.execute` | `g3.request_id`, `ewallet.agent`, `ewallet.coordinator.workflows`, `ewallet.result.status` |
| `g3.coordinator.nl_intake.llm_tool_selection` | `gen_ai.*` (model, tools, tokens, response ID) |
| `g3.coordinator.delegate` (one per Specialist) | `g3.request_id`, `ewallet.specialist`, `ewallet.gateway=a2a-gateway`, `ewallet.capability`, `ewallet.requested_tools`, `ewallet.status` |
| `g3.drift_specialist.execute`, `g3.drift_specialist.mcp_call` | `g3.request_id`, `ewallet.drift.*`, `mcp.tool.name`, `ewallet.gateway=mcp-gateway`, `ewallet.status` |
| `mcp.tool <tool>` (MCP server) | `mcp.tool.name`, `mcp.outcome`, `ewallet.service`, `ewallet.backend.operation` |
| MCP audit `tool_call_completed` | `request_id`, `trace_id`, tool, status, error code, duration |

No prompt text, token or connection string is recorded.

## 4. Data compatibility

DB 0 (canonical serving), DB 2 (rehearsal-5gb materialization), DB 3 (RAG
Hybrid) and DB 9 (synthetic streaming keys) are separate campaigns. The AGI
profile uses DB 0 read-only for Fraud (the only target with a recorded,
working transaction), DB 3 for RAG, and drift artifacts from the immutable
snapshot `feast-r5gb-20261111-214544`; the drift result is a statement about that
dataset. Full reasoning and the alternative profile:
[runbook §1](../../runbooks/edai2/AGENT_INTEGRATION_EVIDENCE_RUNBOOK.md#1-integration-profile-and-data-decision).
Local tests use a deterministic **synthetic** fixture
(`agi-fixture-synthetic-v1`) that the server refuses by default.

## 5. Changed modules, expected evidence and rubric rows

| Module | Change | Evidence | Rubric rows |
|---|---|---|---|
| `ai_platform/llm_agent/business_request.py` | drift and combined typed contracts, generic parser | AGI-08, AGI-10, AGI-11 | 24, 25 |
| `ai_platform/llm_agent/programmatic_coordinator.py` | drift/combined routing, concurrency, partial failure, workflow idempotency, delegation spans | AGI-10, AGI-11, AGI-12 | 24, 25 |
| `ai_platform/llm_agent/workflow_intake.py`, `nl_intake.py` | two-tool selection, deterministic router; planner accepts a tool set | AGI-09–AGI-11 | 3–6, 25 |
| `ai_platform/llm_agent/byo_coordinator.py` | `EWALLET_COORDINATOR_WORKFLOWS`, drift skill on the Agent Card, rendering | AGI-01, AGI-10, AGI-11, AGI-13 | 25, 26 |
| `ai_platform/llm_agent/byo_drift_specialist.py`, `drift_evidence.py`, `byo_mcp.py` | deterministic BYO Drift Specialist, shared MCP client, evidence model | AGI-07, AGI-10, AGI-12 | 19–21, 23 |
| `ai_platform/llm_agent/tools/drift_sources.py`, `tools/drift.py`, `ops/llm_agent/drift_artifacts.py` | baseline registry, window sampler, artifact builder | AGI-02, AGI-07 | 19–21 |
| `ai_platform/llm_agent/tools/service.py` | `isolated` Feast targets, separate RAG connection, drift wiring, capabilities configuration | AGI-02, AGI-04–AGI-06 | 11, 12, 17, 18, 22 |
| `ai_platform/llm_agent/observability.py`, `tools/runtime.py`, `agent_metrics.py`, `byo_runtime.py` | backend operation attribute, `trace_id` in MCP audit, drift agent label, audit field | AGI-12, AGI-14 | 24–27 |
| `infra/helm/llm-agent-platform/templates/agents.yaml`, `mcp-services.yaml`, `values.yaml` | BYO drift allowed; workflows value; `ragRedisSecret`; defaults render byte-identical | AGI-01, AGI-02 | 7, 22, 25 |
| `infra/helm/llm-agent-platform/agi/agents.yaml`, `agi/mcp.yaml` | disabled-by-default integration overlays | Stage 1 diffs | 7, 22–25 |
| `scripts/llm_agent/agi_campaign.sh`, `helm_object_diff.py` (`--expect-identities`), `rag_runtime_run.sh digest` | fail-closed gates, exact object identities, data-state check | Stage 0, 1, 5 | — (deployment safety) |
| `ops/llm_agent/agi_inputs.py`, `scripts/llm_agent/agi_probe.py`, `g3_a2a_smoke.py`, `build_g3_byo_drift_specialist.sh` | Stage 1 inputs and evidence probes | AGI-03–AGI-08 | 16, 23, 24 |
| `ops/llm_agent/registry_catalog.py` | runtime-accurate descriptions (BYO, Declarative, SandboxAgent), MCP refs per BYO module, values overlays | AGI-13 | 8, 21, 26 |

## 6. Local verification

Run with the repository's uv projects (`uv run --frozen`); results of the
implementation commit are in the final report of this task.

| Test module | Environment | Real components | Simulated |
|---|---|---|---|
| `test_agi_contracts.py` | both | Pydantic contracts, wire parsing | — |
| `test_agi_drift_sources.py` | tooling | baseline store, window sampler, PSI, production `create_drift_app` (service-token auth, grants, MCP JSON-RPC) | synthetic fixture data |
| `test_agi_feature_targets.py` | tooling (`--extra feast`) | target validation, RAG `RepoConfig` | `feast.FeatureStore` recorder (no Redis) |
| `test_agi_coordinator_workflows.py` | both | Coordinator core, idempotency, OpenTelemetry context | A2A transport recorder; drift findings computed by the real tool |
| `test_agi_workflow_intake.py` | BYO | router, grounding, typed requests, rendering | LLM responses (`httpx.MockTransport`) |
| `test_agi_drift_specialist.py` | BYO | A2A server (a2a-sdk), request parsing, evidence conversion | MCP session returning real tool results |
| `test_agi_end_to_end.py` | BYO | Coordinator → real Drift/Feature-RAG A2A apps → official MCP client → real `drift-mcp` → artifacts | gateways replaced by routing on `x-kagent-host` and the `/drift/mcp` route; Feature-RAG backend session; synthetic data |
| `test_agi_tools.py` | BYO | `agi_probe` classification against real `drift-mcp` (authorized, hidden tool, 401, rejected, unavailable) and A2A route verdicts; `agi_inputs` derivation | protocol, HTTP 500, timeout and transport failures injected around the real server |
| `test_agi_chart.py` | tooling | Helm renders of the AGI overlays and guards; registry catalog per profile and schema | — |
| `test_agi_campaign_gates.py` | tooling | `agi_campaign.sh` and `helm_object_diff.py` end to end | `helm`, `kubectl`, `gcloud`, RAG runner and probe runtime are recording fakes |
| `test_agi_runbook.py` | tooling | runbook blocks parse; scripts, modules and flags exist; mutations only in approved blocks; rubric wording | — |

None of these is GKE evidence.

## 7. Known limitations

- CPU Qwen3-0.6B may not emit the drift tool call; drift is then completed
  deterministically and audited as such. It may also mis-extract fraud
  arguments, which grounding rejects.
- The drift extract is a systematic sample of at most `--max-rows` rows per
  window so that it fits a ConfigMap; the manifest records the stride.
- `drift-specialist` keeps its Declarative `tools` grant in values (used by the
  rollback profile and the registry catalog); the BYO runtime calls only
  `drift_detection`.
- The deployed KEDA profile still scales `drift-specialist` on CPU.
- Native Agent Registry discovery is not used; the registry remains a catalog.
- Redis access is read-only at the application level only (§3.5).
- Laptop port-forwards bypass the Istio Ambient L4 caller check, so AGI-08 tests
  gateway routes and server credentials and grants, not the SPIFFE caller policies.

## 8. Evidence-to-rubric matrix

Rows are those of the EDAI-2 rubric. The status column of
[RUBRIC_EVIDENCE_INDEX.md](../RUBRIC_EVIDENCE_INDEX.md) is unchanged until the
owner's evidence exists: rows already COMPLETE stay COMPLETE, and no row becomes
COMPLETE from this campaign alone.

| AGI | Primary | Secondary | Proves | Does not prove | Independent evidence still needed |
|---|---|---|---|---|---|
| 01 | 7 | 3–5 (route only) | One ModelConfig, resolved by the BYO LLM agents; drift BYO has no LLM | Benchmark (6), inference platform setup | No, for row 7 |
| 02 | 22 | 16 | Separate pinned Fraud/RAG targets; drift artifact identity; the real access guarantee | Redis-level read-only; pipelines (11–12) | No |
| 03 | 16 | 23 | FastAPI/Pydantic validation and health through the gateway | Rolling update and automatic fallback (18) | Row 18: yes |
| 04 | 22 | 16 | Online features read by ID | Multi-replica, Sandbox (16's other proofs) | Row 16: Sandbox (20) |
| 05 | 22 | — | KServe V2 prediction with model identity | — | — |
| 06 | 22 | 9–10 (consumption) | RAG tool reads the published Hybrid index | Retrieval quality | — |
| 07 | 18 (Helm-deployed MCP tool) | 19 | PSI from versioned artifacts via MCP | Automatic fallback (18), autoscaling (19), Sandbox (20) | Rows 18–20: yes |
| 08 | 23, 24 | — | Server credentials, tool grants and A2A routes deny as designed | Ambient SPIFFE caller policy (port-forward bypass) | Policy objects (AGENTS §7) |
| 09 | 22 | 25 (UI chat) | Fraud workflow through both gateways in kagent UI | Multi-replica/autoscaling (19, 25) | Rows 19, 25: yes |
| 10 | 25 | 19 | Coordinator delegates to the drift agent | KEDA autoscaling (19), Sandbox (20) | Rows 19, 20: yes |
| 11 | 25 | 24 | Coordinator orchestrates both agents in one request | Coordinator scale-out (25) | Row 25 scale test: yes |
| 12 | 49 | 50 (tokens on one span) | One correlated trace across agents, gateways and MCP | Metric dashboards (50–51, already COMPLETE) | No |
| 13 | 8, 21, 26 | — | Catalog matches the deployed runtime (after an approved publish) | Registry discovery | Publish approval |
| 14 | 51 | 47 | Invocation and tool-call counts, running digests | Substrate WorkerPool (27), autoscaling (19, 25) | Row 27: yes |

Requirements that stay separate from this campaign:

| Row | Requirement still needing separate evidence |
|---|---|
| 18 | MCP automatic fallback and rollout behavior |
| 19 | Specialist autoscaling (KEDA) |
| 20 | Business agent in Sandbox |
| 22 | Deliverables inherited from rows 16–21 (multi-replica, Sandbox, autoscaling) |
| 25 | Coordinator autoscaling and a demonstrated scale-out |
| 27 | Substrate WorkerPool and warm-worker optimization |
| 34–35 | CI/CD deployment evidence beyond manual build and deploy |
