# OWNER DIRECTIVE — FINAL EDAI-2 AGENT PLATFORM INTEGRATION ON GKE

**Repository:** https://github.com/HuyyGiaa/ewallet-data-ai-platform  
**Working branch:** `feature/edai2-agent-platform-integration`  
**Last verified branch HEAD:** `2f8778f4d9d9f9f5d29e637fbb17e6126c690f86` (verify at execution; do not assume it remains current)  
**Official rubric:** https://docs.google.com/spreadsheets/d/1bJCzCZ0k-ASnIXkBK1Bd-C9EyWXjPhbIkgAdVdbqH7E/edit?gid=685698288#gid=685698288  
**Target:** Existing GKE `ewallet-dev`, project `ewallet-data-ai-platform-hg26`, zone `asia-southeast1-b`  
**Scope:** AGI-01–14, AGR-01–07, AGS-01–07; related ModelConfig, Registry, security, metrics, and Istio external-routing assessment.  
**Status at handoff:** PKG-T runtime stability was accepted as `RUNTIME_STABILITY_GKE_PASS`; remaining integration packages are not yet accepted on GKE.

---

## 0. Your mission and definition of done

You are taking over the *remaining integration*, not starting a new design project. Make the existing EDAI-2 Multi-Agent Platform genuinely functional **on the real GKE cluster**, then hand over a repeatable owner-driven evidence campaign. Local/kind tests, successful Helm revisions, Ready Pods, or mocked API responses are **not** sufficient to claim business E2E PASS.

The primary success path is:

```text
Analyst → kagent UI / authorized client → Fraud Coordinator
       ├── LLM Gateway (agentgateway) → actual configured vLLM
       └── A2A Gateway (agentgateway) → kagent Controller
              → SandboxAgent Feature-RAG / Drift → Substrate / shared gVisor WorkerPool
              → MCP Gateway (agentgateway) → MCP Servers
              → Feast / KServe / RAG / drift artifacts
              → validated Specialist results → grounded Coordinator response
```

Do not invent results, bypass any gateway, create a duplicate data store, or silently switch to mocked backends. Verify that a result from the business backend actually appears in the final response; distinguish fraud and drift datasets.

**Priorities:**
1. Core business E2E really works on GKE for Feature-RAG and Drift, and for LLM selection where supported.
2. Required service identities, credentials, tool grants, security boundaries and request propagation work.
3. Required reliability and deployment autoscaling tests work under bounded real traffic.
4. Optional B′ WorkerPool scaling is attempted safely, without breaking a working A+S baseline.
5. Genuine evidence, clear docs and verified cloud sleep.

The platform need not achieve comprehensive production-grade HA or flawless behavior under every exotic upstream failure. It **must not** be declared ready while an observed core defect prevents real use.

**Working style:** diagnose and fix within approved scope, test locally, deploy to GKE, test again, record actual evidence, proceed to the next gate. Avoid a cycle of ever-larger readiness reports without deployment. Do not skip small but essential integration details.

---

## 1. Read actual repository state, then establish a single campaign

First check `git branch --show-current`, `git rev-parse HEAD`, `git status`, current commits and any uncommitted files. Work only on `feature/edai2-agent-platform-integration`; do not create worktrees, reset, force-push or merge `main`. Preserve concurrent/unfinished changes.

Read the canonical sources **before** deciding what to change:

```text
docs/workplans/edai2/agent-platform-campaign/HANDOFF.md
docs/workplans/edai2/agent-platform-campaign/checkpoints.json
docs/workplans/edai2/agent-platform-campaign/images.json
docs/workplans/edai2/agent-platform-campaign/resume-rca/RCA.md
docs/runbooks/edai2/AGENT_PLATFORM_MASTER_EVIDENCE_RUNBOOK.md
docs/runbooks/edai2/AGENT_INTEGRATION_EVIDENCE_RUNBOOK.md
docs/edai2/agents/AGENT_PLATFORM_COMPLETION_AUDIT.md
docs/edai2/agents/AGENT_INTEGRATION_CONTRACTS.md
docs/edai2/agents/AGENTS_REGISTRY_GATEWAYS.md
```

Then inspect relevant code/config in `ai_platform/llm_agent/`, `infra/helm/llm-agent-platform/`, `infra/kubernetes/agent-sandbox/`, `infra/kubernetes/substrate-scale/`, `ops/llm_agent/`, `ops/experiments/`, `scripts/llm_agent/`, `scripts/experiments/`, and `ops/gcp/`.

**Important stale-document warning:** earlier runbook sections may describe PKG-K `kagent` revision 6, Substrate revision 3, an unpatched 30s timeout, or historical S0 failures. These represent old experiments. The PKG-T handoff records the more recent deployed baseline. Verify effective *live* Helm values and image IDs before mutating any release, then update stale guidance. Neither documentation nor a tag alone proves current runtime state.

Read the rubric itself if accessible. If it cannot be accessed, record the access limitation, use the repo's rubric mapping, and do not invent unseen rubric criteria.

Create one unique campaign ID and a durable campaign evidence root, e.g. `docs/evidence/edai2/agents/<campaign>/` for sanitized committed evidence plus a private `~/ewallet-evidence/agents/<campaign>/` location for potentially sensitive raw data. Ensure files containing secrets are never copied into Git. Use atomic checkpoints, recorded commit/image digests and named test artifacts so an interrupted laptop session can resume. Do not place the only copy of a critical log or deployment baseline in `/tmp`.

Produce a compact checkpoint ledger: phase, prerequisites, source status, GKE status, actual PASS rule, artifact path, blocker, rollback. Avoid proliferating overlapping reports.

---

## 2. Freeze and verify the accepted PKG-T runtime

Previous campaign: `rts-20261115T114235Z`; accepted verdict `RUNTIME_STABILITY_GKE_PASS`. Recorded kept configuration:

| Component | Recorded GKE baseline |
|---|---|
| `kagent` release | Revision **8**, project-patched controller v0.10.3 |
| `substrate` release | Revision **5**, project-patched ate-api v0.0.9 |
| kagent Substrate RPC | `SUBSTRATE_CALL_TIMEOUT=90s` |
| atelet | 512Mi request / **1536Mi** limit |
| Sandbox Worker | Project-patched v0.0.9 `ateom-gvisor` image, digest-pinned in campaign manifest |
| Accepted behaviors | Unpaced session turns, concurrent restores, bounded worker wait, cancellation recovery, shared-worker reuse |
| Cloud at handoff | `SLEEP_VERIFIED`, CPU node pool at 0 |

Verify current effective Helm values, controller image digests, installed patches and namespaces. The accepted fixes are **in our pinned builds, not upstream kagent/Substrate releases**. Check compatibility when packaging P1/S1/P2/P4: later Helm renders must not silently revert image, timeout, atelet memory or watch scope.

Preserve this baseline unless an actual integration regression is observed and traced. Do not rebuild or redeploy PKG-T as a routine prerequisite. Existing known limitations are not an instruction to undertake an unlimited upstream rewrite:

- Single kagent controller replica; per-session turn coordination is in memory.
- A hard controller crash may delay Actor reconciliation.
- An ate-api crash can require a subsequent session turn to reconcile.
- Certain unchanged upstream images have reported vulnerabilities, to be tracked honestly.
- Prior GKE runtime tests were lifecycle tests; they were not successful MCP business-tool tests.

Add narrow regression checks that ensure live Sandbox sessions still work **while** MCP tasks are executed. Reopen PKG-T only for a demonstrated regression that blocks the target workflows.

---

## 3. Target architecture and ownership boundaries

Keep **three independent internal agentgateway v1.7.0 workloads**:

| Gateway | Function | Do not confuse with |
|---|---|---|
| **LLM Gateway** | Authorized Agent-to-LLM routing, ModelConfig model and key | External Istio routing |
| **MCP Gateway** | MCP server routing, service-token grants, tool authorization | Direct MCP server access |
| **A2A Gateway** | Coordinator-to-Agent delegation, allowlisted Agent paths | HTTPRoute to a public UI |

Also distinguish **Istio Ambient Mesh** (internal identity/ztunnel/policies) from the **Istio External Gateway** (public HTTPS/UI entrypoint). Do not merge these four concerns or add NGINX merely because the rubric uses that name.

Core Agent roles:

- `fraud-coordinator`: BYO Deployment, validates user input and performs LLM-dependent tool/workflow selection through LLM Gateway.
- `feature-rag-specialist`: typed requests, `feature_lookup`, `fraud_scoring`, `knowledge_search` through MCP Gateway.
- `drift-specialist`: typed drift requests, `drift_detection` using approved baseline/window artifacts through MCP Gateway.
- Both Specialists additionally execute as **SandboxAgents** using one shared `business-agents` WorkerPool and gVisor; the Coordinator's P4 path reaches these via A2A Gateway → kagent Controller.

The actual kagent v0.10.3 SandboxAgent route is `/api/a2a-sandboxes/ewallet-sandbox/<agent>/`, **not** the Deployment Agent's `/api/a2a/<agent>` route. Verify paths, `contextId`, user identity and allowed A2A destinations from source and runtime. Preserve separate logic for ModelConfig-bound declarative Agents vs BYO ModelConfig resolver; do not falsely claim both consume the CR in the same way.

A+S baseline: KEDA scales the three BYO Deployments, while a **fixed two-worker** Sandbox WorkerPool serves both Specialists. B′ is a **separate optional** design in which KEDA scales the WorkerPool via `/scale` (requires PKG-W/P3). Do not present KEDA Deployment scaling as proof of WorkerPool scaling.

---

## 4. Non-negotiable GKE safety and permissions

**GKE:** `ewallet-dev`, `ewallet-data-ai-platform-hg26`, `asia-southeast1-b`, CPU node pool `cpu-pool`. Use `ops/gcp/cloud_lab.py`, with its existing plan, approval, execution, verification and cost controls. No GPU, no new cluster or node pool. Prefer three CPU nodes for isolated Sandbox work. **Five CPU nodes are conditional on the separate PKG-C capacity/owner approval**, not an automatic permission to expand. Never add a sixth node because an experiment cannot schedule.

A cluster wake **does not authorize** changes to unrelated releases. For the existing reviewed **P1 / S1 / P2 / P4** package scopes, the owner authorizes the bounded integration campaign subject to *each package's existing approval token and safety gates*. Before any shared release mutation, require:

1. Exact repo commit/source and immutable image digest.
2. Live Helm revision and values saved privately (avoid secrets in Git/logs).
3. Rendered baseline equals live release; identify exact added/changed/removed objects.
4. Server-side dry-run and package-specific preflight PASS.
5. No unexpected identity, policy, credential, storage or resource changes.
6. Deterministic rollback, including HPA/ScaledObject ownership where relevant.
7. Blast radius, scheduling capacity and GKE sleep consequences understood.
8. Read-back and meaningful runtime verification after rollout.

Use the existing package-specific tokens documented in the master runbook; **never** bypass or weaken approval checks. Changes outside the reviewed package scope require a fresh exact diff and owner approval. **Not automatically authorized:** cluster-scoped CRD mutation (PKG-P3), wide IAM/RBAC policy changes, production data writes, deletion of historical Actor/snapshot residue, public ingress exposure, blanket credential revocation, and guarded sleep.

Use read-only or appropriately isolated inputs. Do not write to production Redis DB 0 or mutate canonical GCS datasets. Preserve all active credentials while rotating via reviewed transition logic. Handle sensitive outputs only in private directories; never commit Kubernetes Secrets or user/transaction PII. Never claim network authorization is proven merely because a YAML object exists.

**Plan safe sleep before wake:** P1 can introduce PDB-protected replicas. Check the sleep manifest, original replica states and PDBs; do not leave a 5-node cluster running due to an unanticipated PDB. Normal `plan-sleep` → `sleep` → `verify --expect sleep` is preferred. `--scale-pdb-guarded` always requires its own explicit approval; never disable PDBs or wipe Deployments to force sleep.

If progress is blocked on an approval, continue read-only/local work safely and report the exact approval, resource diff, rollback, cost and dependency. Do not run a mutating command while a prerequisite is blocked.

---

## 5. Campaign order, dependencies and mandatory outcomes

The sequence is:

| Phase | Package / task | Mandatory GKE outcome |
|---|---|---|
| **G0** | Preflight, data and PKG-T check | Reproducible baseline, correct images and input data, safe wake/sleep plan |
| **G1** | **PKG-P1 MCP Reliability** | Real MCP backends, correct Gateway routing; replica/rolling-update/failover checks |
| **G2** | **PKG-S1 Sandbox MCP Access** | Both Sandbox Specialists call real allowed MCP tools; negative auth denies |
| **G3** | **PKG-P2 Agents + KEDA** | Correct Agent/ModelConfig deployments, autoscale from real observed metrics |
| **G4** | **Timeout/deadline validation** | Actual E2E budgets consistent; fix observed conflicts before P4 acceptance |
| **G5** | **PKG-P4 Coordinator→Sandbox E2E** | Fraud, drift and supported combined workflow reach actual business backends |
| **G6** | AGI/AGR/AGS regression | Accurate PASS/PARTIAL/BLOCKED and real evidence for each applicable requirement |
| **G7** | B′: **PKG-W/P3** if separately approved | KEDA genuinely scales shared WorkerPool; safe scale-in; fallback preserved |
| **G8** | Security, telemetry, External Gateway review | Actual denials/metrics/trace; external scope identified without unsafe exposure |
| **G9** | Documentation + owner evidence readiness | One coherent architecture story; reproducible screenshot commands |
| **G10** | Cleanup, final verification and sleep | Campaign-only resources removed; kept releases documented; sleep verified |

Do not interpret this as a reason to run P2 before S1's required functionality: maintain package dependencies. Independently prepare/test code while another package is waiting, but serialize shared-release upgrades. **As soon as S1 works, run an early typed Sandbox→MCP business smoke. As soon as P4 works, run the complete business E2E**, rather than postponing all functional proof until after optional autoscaling experiments.

For every checkpoint: inspect current source → targeted tests → correct `uv` environment → image build/scan only if changed → exact Helm diff/dry-run → approved deploy → runtime test with factual PASS criteria → evidence → continue. Avoid rerunning the whole architecture audit at each step. A blocked experiment must not be silently skipped or relabeled PASS.

---

## 6. G0 — Preflight and real data contracts

Run the canonical AGI preflight only after choosing a valid wake profile. Verify required Helm release identities, endpoints, ServiceAccounts, registry, ModelConfig and namespaces, real read-only data, available capacity, plus `kagent` and `substrate` patched state. Check whether chart guards in `agi_campaign.sh` assume an older runtime/image configuration and correct *only those source-of-truth inconsistencies*, not their protections.

The existing AGI runbook distinguishes the datasets:

- **Fraud:** Redis DB 0 canonical online features, read-only through approved tool path; valid recorded transaction required.
- **RAG:** Redis DB 3 / the recorded Hybrid RAG publication; verify project, registry and checksum.
- **Drift:** versioned rehearsal snapshot baseline/current window, which may be a different dataset from the fraud transaction.
- Other DBs/snapshots are not automatically interchangeable.

Verify these references remain available and consistent with the latest published data. Do not invent a transaction ID or use unrelated streaming synthetic keys to prove canonical fraud scoring. Do not represent drift computed from rehearsal-5gb as an explanation for a particular transaction from the canonical fraud data. Record schema, data version, model version, registry and store provenance; redact customer/credential material from evidence. If a backend is asleep or unavailable, record why and restore it only through existing approved operations.

Observe real model readiness, KServe endpoints, Redis/RAG reachability, MCP connectivity and permissions. A business test must verify actual backend output, not just a successful HTTP status.

---

## 7. G1 — MCP reliability, PKG-P1 and AGR

Package: existing `p1-mcp` on Helm release `llm-agent-mcp` in `ewallet-mcp`. Prerequisites: pinned validated MCP image (including Feast 0.67.0 compatibility), correct inputs, identity/diff gate and capacity. Do not rebuild unchanged images. Verify:

- Both `feature-rag-mcp` and `drift-mcp` configured for the reviewed replica count (intended two each) and actually Ready.
- Correct Deployment selectors, Service Endpoints/EndpointSlices, probe endpoints, PDBs, rolling update strategy and resources.
- Placement on eligible Nodes; distinguish `ScheduleAnyway` preference from a guaranteed two-node spread.
- `mcp-gateway` itself exists, handles actual streamable-HTTP MCP protocol, routes to correct server, and enforces credentials/tool grants.
- Real approved `feature_lookup`, `fraud_scoring`, `knowledge_search`, `drift_detection` results, with backend provenance. Check exact tool contracts in source.

**AGR-01:** prove live replicas, endpoints, readiness and placement.  
**AGR-02:** if separately approved capacity and experiment permits, use `mcp_experiment.sh` baseline/optimized under bounded read-only load. Terminate or fault one approved experiment MCP Pod/backend and observe actual request continuity, retries, failover, latency and final recovery. Do not confuse 2 Ready Pods with demonstrated failover, and do not disturb production data. If PKG-C five-node scheduling still fails, record actual `CAPACITY_INSUFFICIENT` and stop that experiment, not the entire Agent Platform.  
**AGR-03:** execute a genuine rolling-update test under a typed request loop and record request failures, latency, ReplicaSets and endpoint transition. No invented 0-error statistics.

Verify all changes through actual runtime logs/requests. Preserve least privilege; avoid retrying non-idempotent operations. Prepare rollback of P1 before moving forward.

---

## 8. G2 — PKG-S1, real Sandbox→MCP business access

Prior S0 used random placeholder tokens and returned `mcp_transport_error`; **that is not business success**. S1 must connect both BYO SandboxAgents to real business MCP tools through MCP Gateway.

Use the existing `g3_mcp_credentials` **transition** workflow and reviewed overlays (`s1-mcp`, `s1-agents`), with dedicated subjects `sandbox-feature-rag` and `sandbox-drift` and corresponding client Secrets in `ewallet-sandbox`. Preserve all existing production subjects during transition; do not print or commit token values. Review any ambient namespace labeling, NetworkPolicy and AuthorizationPolicy changes exactly. The S1 agents overlay must **append** its intended cross-namespace flow without deleting the existing set.

Check the complete real path:

```text
Sandbox Actor → gVisor Worker → pod network / Istio Ambient / ztunnel
  → MCP Gateway → MCP server → approved backend
```

Determine the actual worker Pod ServiceAccount (`ewallet-sandbox/default` according to previous baseline, but verify), mesh enrollment, source identity as seen by the Gateway, backend token identity and policy enforcement. The pinned Substrate WorkerPool API does not necessarily expose every expected identity field. Do not assume SPIFFE propagation or `allowedDomains` provides enforcement without a negative test. Fix the actual supported enforcement layer rather than disabling security.

**Positive GKE tests:** Sandbox Feature-RAG can execute feature lookup, fraud scoring and policy knowledge search using valid real data; Sandbox Drift can execute the actual PSI/drift tool on the approved artifact versions. Every tool must be called through MCP Gateway and return a validated typed result. Tool-level failures must be visible, not converted to invented outcomes.

**Negative GKE tests:** missing/wrong token, cross-Specialist credential, ungranted tool, and safe forbidden-destination probe. Verify the denial happens at the intended boundary. Redact credentials.

**Lifecycle regression:** same Actor identity on follow-up, unpaced turns still work, no stuck `RESUMING`, WorkerPool shares capacity between the two Specialists, no permanent worker leak. Capture logs and Actor/Worker snapshots.

**S1 PASS only if:** both Sandbox Specialists complete real authorized MCP tool executions and negative authorization is demonstrated. No further work should claim full business E2E while S1 remains blocked.

---

## 9. G3 — PKG-P2 Agent deployment, ModelConfig, KEDA

Deploy the reviewed `p2-agents` integration, retaining three logical roles. Verify correct BYO images, selectors, probes, ServiceAccounts, input/output contracts, A2A/MCP URLs, Registry entries, telemetry, and global `ewallet-global-llm` ModelConfig usage. The Coordinator uses LLM-dependent model selection through LLM Gateway; typed Drift need not call the model. Check the actual **BYO resolver** rather than claiming it directly references ModelConfig the way a Declarative Agent does. Do not conflate Deployment Specialists with SandboxAgent replicas.

**KEDA for BYO Deployments:** verify the three ScaledObjects adopt/remove the intended existing HPAs without double ownership, Prometheus access, metric names/labels, freshness and `ScalingActive`, target/current values, available replicas, scale-up/down stabilization, and graceful shutdown of in-flight requests. Use bounded **real typed traffic**. Do not use `kubectl scale` as proof of autoscaling. Verify new Pods actually serve valid requests and scale-in does not silently drop work. Check metrics against logs and collected sampler output. If rollout or KEDA breaks, use the documented P2 rollback **delete ScaledObjects first, restore Helm/HPA, verify metrics ownership**.

A+S is a valid baseline even without B′: Agent Deployments are KEDA-scaled; the shared sandbox WorkerPool is fixed-size. Keep this distinction explicit in docs/rubric claims.

---

## 10. G4 — Deadline and timeout compatibility, built into this campaign

**Do not run a separate speculative timeout project.** Integrate this checkpoint before **P4 acceptance**. The currently documented budgets include:

| Boundary | Recorded value (verify effective runtime) |
|---|---:|
| Coordinator member delegation | 480s |
| A2A Gateway Sandbox route | 150s |
| kagent `ResumeActor` RPC | 90s |
| Feature-RAG business turn | 120s |
| ate-api detached workflow | 300s |

These are *limits*, not normal measured latencies. Actual restore on GKE after PKG-T was typically much shorter than 90s. However, a slow restore + a long tool workflow + Gateway overhead could exceed the 150s route budget. Also distinguish other independent boundaries: kagent A2A client, MCP Gateway and MCP client/server, LLM Gateway/vLLM, and (if externally traversed) Istio HTTPRoute. Note: existing external `templates/gateway.yaml` has separate 30s/25s request/backend values by default; it is not the same route as the internal A2A Gateway.

1. Read effective **GKE** values, configs, Envoy/agentgateway route behavior and code-level deadlines.
2. Trace real time spent waiting for a Worker, restoring Actor, reaching Specialist, calling MCP tools, accessing Feast/KServe/RAG/drift artifacts, generating Coordinator output and returning through Gateway.
3. Measure warm, cold and concurrent execution. Capture spans/IDs and first failing timeout boundary, where applicable.
4. If budgets are inconsistent **or real requests time out**, select the narrowest justified change, prove it is compatible with the enclosing deadlines, run focused tests, exact-diff/dry-run, approved deploy and repeat the E2E test.
5. Do **not** increase every timeout blindly or hide a backend deadlock. Do **not** change already accepted PKG-T logic without a demonstrated regression.

Verdict: `TIMEOUT_VALIDATED_NO_CHANGE`, `TIMEOUT_CORRECTED_GKE_TESTED`, or `TIMEOUT_BLOCKING_E2E`. An unresolved timeout that breaks supported core workflows blocks P4 PASS.

---

## 11. G5 — PKG-P4, complete Coordinator→Sandbox business E2E

Deploy the reviewed `p4-agents` route and Coordinator configuration only after P2/S1 prerequisites and timeout validation. Verify actual A2A Gateway policy, route allowlist, Service/namespace names, `/api/a2a-sandboxes/ewallet-sandbox/<agent>/`, `contextId`, `X-User-Id`, request/task IDs, status propagation and correlation. Do not bypass the A2A Gateway or route Coordinator straight to Specialist Pods as a shortcut. Keep Deployment Specialists available as the documented rollback fallback, without silently relabeling a fallback run as Sandbox E2E.

**E2E-FRAUD (required):** use a valid recorded transaction and supported natural-language or typed request. Validate PII screening, grounding and input schema; if natural language, observe Coordinator → LLM Gateway → configured vLLM selection. A2A Gateway must delegate to Sandbox Feature-RAG, which calls MCP Gateway for `feature_lookup`, `fraud_scoring` (KServe) and `knowledge_search` (RAG) as required by the contract. Compare reported features, prediction/probability, model version and policy citations with the actual tool/backend outputs. The Coordinator must not invent a fraud score or policy text. A missing data item or failed required tool must produce a truthful error, not fabricated success.

**E2E-DRIFT (required):** Coordinator selects/delegates to Sandbox Drift; its MCP tool accesses the approved versioned baseline/window artifacts, computes drift and returns structured metrics and data provenance. Distinguish dataset-level drift from a particular customer's fraud inference.

**E2E-COMBINED:** run if implemented/supported; verify both Specialists are selected appropriately, their outputs are correctly separated/combined, and concurrency does not regress WorkerPool stability. Do not invent this capability if the current public contract lacks it.

**Negative E2E:** invalid/out-of-scope request, incorrect Agent path or caller, invalid MCP token, forbidden tool and a bounded backend-failure scenario. Record whether errors propagate correctly without leaking data or corrupting session state.

**UI proof:** where the rubric requires actual Agent chat, use the real kagent UI. Direct curl/API tests aid diagnosis but do **not** replace UI evidence. Prepare owner-safe access instructions; do not auto-generate screenshots. Verify the final answer reflects genuine backend evidence, correct Agent/task lifecycle and correlated trace IDs.

**Core GKE PASS** requires actual end-to-end successful Feature-RAG and Drift paths and the LLM Gateway path for model-dependent requests, without mock backend results. A 200 response, `Pod Ready`, or a single isolated MCP call is insufficient.

---

## 12. G6 — AGI / AGR / AGS exact rubric regression

Run the real runbook inventory, not invented replacements:

- **AGI-01–14:** Global ModelConfig, Agent identities/Registry, MCP server contracts and real tool responses, Gateway routing, valid/invalid credentials and tool grants, Coordinator→Specialist delegation, kagent UI interaction, traces and evidence linkage.
- **AGR-01–07:** MCP replicas, failure behavior, rolling update, KEDA Deployment scaling, metric accuracy and freshness, actual scale-out/in and request continuity.
- **AGS-01–07:** SandboxAgent/gVisor, isolation, authorized tool access, shared WorkerPool across sessions, stable Actor IDs and suspend/resume, cold/warm latency, utilization/cost, and B′ where permitted.

Reuse PKG-T evidence for already accepted low-level runtime facts but do not claim it proves S1 business tools or P4 routing. A real integration regression warrants a narrow retest. For every row record requirement, code, GKE command/request, raw result, screenshot view, PASS condition and status. `NOT_TESTED` or `BLOCKED` is never a PASS. Separate **technical proof** from **owner screenshot captured**.

Consider that a documented 0-failure acceptance target must be evaluated on the relevant *post-fix* run, not by pretending pre-fix errors never happened. Preserve failed experiment history; distinguish it from the accepted rerun.

---

## 13. G7 — B′ WorkerPool autoscaling: optional, isolated and approval-gated

Only after core E2E is stable, evaluate B′. Existing code in `infra/kubernetes/substrate-scale/` patches WorkerPool CRD `/scale` via `scaleSelector` / `labelSelectorPath` and uses PKG-W KEDA watch scope. Previous kind control-loop proof is **not** GKE evidence. Note the pinned WorkerPool `spec.template` fields do not include podAntiAffinity or topologySpread; do not add fields Kubernetes will prune.

**CRD mutation is cluster-scoped and is not implicitly authorized by this directive.** Before applying PKG-P3: back up the live CRD, inspect all existing WorkerPools (including `kagent-pilot`), exact diff, API compatibility with PKG-T, dry-run, KEDA watch scope, active Actor drain semantics, rollback and explicit `CRD_PATCH_APPROVED`/`CRD_PATCH_GKE_APPROVED` approval. Never bypass an approval token.

If approved, test with bounded real typed traffic: KEDA observes the correct fresh metric → HPA `ScalingActive=True` → WorkerPool desired replicas via `/scale` → Substrate creates healthy Workers → Agents actually use new capacity → safe scale-in. Target experiment 1→2 (→3)→1 within actual CPU constraints. Verify Actor ownership and no active session loss; mere replica-count change is insufficient. Pay special attention to Substrate v0.0.9 behavior when a busy Worker is scaled in.

If safety or API compatibility cannot be demonstrated, mark `B_PRIME_BLOCKED`/`PARTIAL`, leave original CRD untouched or roll back precisely, and preserve the functional **A+S** baseline. Never trade a successful business workflow for an optional scaling claim. State clearly which rubric rows remain unproven.

---

## 14. Security, observability and external routing

**Security must be observed at runtime, not inferred from manifests.** Verify dedicated/least-privilege ServiceAccounts, token subjects, MCP tool grants, A2A caller restrictions, Istio Ambient ztunnel identity in the actual gVisor path, NetworkPolicies, secret lifecycle, allowed egress and safe negative-denial cases. If identity propagation differs from the original assumptions, implement a secure supported enforcement layer and document its actual guarantees. Never temporarily weaken global AuthorizationPolicies or make MCP endpoints directly accessible just to pass business E2E.

**Observability:** ensure real useful metrics, logs and OpenTelemetry trace propagation across Coordinator → LLM/A2A Gateway → Sandbox Specialist → MCP Gateway → MCP Server → backend. Verify request and trace IDs, actual parent/child span relationships, service labels, Prometheus in-flight metric semantics, KEDA metric source, Grafana dashboards and trace views (Tempo if deployed). Record an actual continuous request trace; do not stitch together unrelated traces. If the Gateway loses context, fix the propagation in the narrowest affected layer.

**External Routing & Gateway rubric rows 37–43:** user-selected implementation is **Istio Gateway**, distinct from internal agentgateways and Istio Ambient. Inspect the existing Gateway/HTTPRoute, actual deployment, API/UI requirements, HTTPS/domain, authentication and rate limiting. If the rubric explicitly names NGINX, document functional equivalence and potential grading mismatch rather than claiming exact technology compliance. Do not create a parallel NGINX installation. Do not expose Grafana, traces, Agent UI, Registry or internal admin endpoints publicly without a reviewed identity/TLS/rate-limit policy and separate scope approval. External-facing work must not derail core E2E; report explicit missing DNS/certificate/approval dependencies rather than inventing a domain.

---

## 15. Python dependencies, Go, image provenance and resource limits

**Python:** determine the correct `pyproject.toml` and `uv.lock` per project. Agent tooling is `infra/docker/llm-agent-tooling`; use `uv sync --locked` and `uv run --locked --project infra/docker/llm-agent-tooling --group test ...` for relevant tests, adapting to each actual project. `httpx` and `pydantic` were already declared/locked in PKG-T; do not add duplicates. Use `uv add` in the correct dependency group for genuine new packages. Run `uv lock --check`; do not use ad-hoc `pip install`, stray `requirements.txt` or unrestricted package upgrades to hide import errors. Make Docker/CI installs consistent with their lockfiles. Go components retain `go.mod` / `go.sum` and pinned toolchains; `uv` does not manage Go packages.

**Code quality:** modular, readable, focused diffs; separate runtime logic, deployment orchestration, evidence and tests. Preserve typed schemas and business contracts. Prefer existing workflows and chart helpers over new one-off scripts. Avoid global environment side effects, hidden credentials and scripts that silently mutate the cloud.

**Images:** rebuild only changed components from reviewed source, scan (Trivy/existing gates), record source SHA and digest, deploy immutable digest references, read back actual Pod image IDs. Do not downgrade security checks to finish quickly. Verify patched PKG-T images remain in place after other chart upgrades.

**Laptop:** a previous local session stopped abruptly after significant thermal throttling while heavy Go `-race` builds shared resources with kind and DataHub. Use controlled build parallelism, stop unrelated heavy local containers where safe, prefer resumable logs outside `/tmp`, and avoid a thermal/resource overload. Do not disable tests merely because they are heavy; move or bound their execution appropriately.

---

## 16. Evidence, screenshots, documentation and owner handoff

**Do not fabricate screenshots or results.** The owner will personally reproduce and capture terminal/Grafana/Tempo/kagent UI screenshots, typically under `~/Pictures/Screenshots/`. No `SCREENSHOT_AUDIT` bureaucracy. Keep genuine raw logs, sampled actors/workers, Helm diffs, metrics, request traces, `uv` tests, security denials and cloud lifecycle evidence, sanitized for Git. Create SHA256 manifests for actual captured/committed evidence. Never mark `EVIDENCE_CAPTURED` until artifacts really exist or `RUBRIC_COMPLETE` until the required screenshots/proofs have been reviewed.

Prepare a concise **OWNER REPRODUCTION RUNBOOK**, organized by AGI/AGR/AGS and actual screens: exact prerequisites, command/API or UI page, expected PASS output, screenshots to take, request/trace IDs, cleanup. The owner should not have to reconstruct commands from a long conversation. Do not generate fake UI captures or alter data to create attractive screenshots.

**Documentation must tell one coherent story.** Update existing canonical docs (when present):

```text
docs/edai2/agents/AGENTS_REGISTRY_GATEWAYS.md
docs/edai2/agents/AGENT_INTEGRATION_CONTRACTS.md
docs/edai2/agents/AGENT_RELIABILITY_AUTOSCALING.md
docs/edai2/agents/AGENT_SANDBOX_SUBSTRATE.md
docs/runbooks/edai2/AGENT_PLATFORM_MASTER_EVIDENCE_RUNBOOK.md
docs/workplans/edai2/agent-platform-campaign/HANDOFF.md
docs/workplans/edai2/agent-platform-campaign/checkpoints.json
```

Confirm each file actually exists and locate any renamed canonical equivalent instead of creating duplicates. Explain *why* components exist, precise real route, credentials/identity, core data contracts, observed GKE results, security and recovery guarantees, known limitations, evidence links and rubric mapping. Do not paste raw logs without interpreting them; do not claim an untested path has been deployed.

Preserve historical failed experiments and PKG-T evidence. Distinguish **source implemented**, **local/kind tests**, **GKE deployed**, **actual business success**, **evidence captured**, **rubric complete**. Clarify the distinction between fixed A+S and B′.

---

## 17. Failure management and scope discipline

Every observed failure should have: symptom/time/request ID → first failing layer → supporting log/trace → hypothesis with falsifiable test → narrow correction → local regression → exact Helm diff/approval → GKE retest → evidence and status. Prioritize fixes that restore core workflows or required security. Stop speculative loops; after bounded attempts or a fundamental upstream limitation, classify the blocker accurately and preserve a tested fallback. Never claim an optional enhancement is a required blocker if core E2E is already working. Conversely, never dismiss a discovered failure that breaks required E2E, authorization or safe cleanup.

**Stop immediately on:** unexpected shared-release/CRD diff, unauthorized broad permission, possible production data mutation, secret exposure, actual app business output inconsistent with backend evidence, unexpected PDB/sleep impact without a safe return path, or a rollback that cannot be performed. Record a specific decision package instead of bypassing controls.

**Cloud safety:** when blocked from further deployment, stop experiment traffic, preserve sanitized evidence, remove campaign-only resources and return the cluster to a safe node state through authorized tooling. Never leave expensive idle nodes running waiting for the owner; do not claim `SLEEP_VERIFIED` until `verify --expect sleep` confirms no failures and CPU node pool is zero.

---

## 18. Final verdict gates and delivery format

Report **three independent levels**, not one vague `COMPLETE`:

### Level A — `AGENT_PLATFORM_CORE_GKE_PASS`

Required: real Coordinator → correct LLM path for model selection → A2A Gateway → **Sandbox** Feature-RAG and Drift → MCP Gateway → genuine backend outputs → grounded Coordinator response, with correct authentication and no blocking timeout. Typed/API and UI proof where specified. No mocks. If any required path fails, choose `CORE_GKE_PARTIAL` / `CORE_GKE_BLOCKED` and name the missing path.

### Level B — `AGENT_PLATFORM_RELIABILITY_GKE_PASS`

Required for claimed rubric scope: actual MCP multi-replica/failover/rolling update, KEDA scaling Agent Deployments with real metrics, request continuity and shared Workers. Record B′ separately (`PASS`, `PARTIAL`, `BLOCKED`, or `NOT_TESTED`) because cluster-CRD approval/safe scale-in may not be available. Do not imply full HA of kagent or Substrate.

### Level C — `AGENT_PLATFORM_RUBRIC_COMPLETE`

Only after every required row has validated runtime proof and required owner screenshots. Until then report `RUBRIC_PENDING` with precise missing items. Do not claim screenshot capture on the owner's behalf.

**Final handoff, concise but complete:**

1. **Git:** starting/final HEAD, branch, commits, push and clean tree; no merge to main.
2. **Runtime baseline:** retained PKG-T image digests, kagent/Substrate revisions and important effective values.
3. **Packages:** P1/S1/P2/P4/W/P3 statuses, exact Helm revisions, resource diff, approvals and any rollback.
4. **Business:** actual fraud, drift and supported combined input/output contract, backend/version, terminal task states, request IDs, traces and cold/warm/concurrent latency.
5. **Gateway/deadlines:** actual LLM/MCP/A2A routing, measured budgets, any timeout fixes and post-fix retest.
6. **Security:** real allowed and denied operations and policy identities.
7. **Reliability:** MCP failover/rollout, KEDA metrics and scale-out/in, WorkerPool sharing and B′ if approved.
8. **Rubric:** AGI-01–14, AGR-01–07, AGS-01–07 and relevant external Gateway rows with precise evidence state and blockers.
9. **Evidence:** safe directory, raw file/trace references, SHA256 manifest, owner's commands/UI screenshot list.
10. **Cloud:** wake/sleep timeline, max nodes, cost estimate, removed vs retained resources, final `SLEEP_VERIFIED` and zero CPU nodes—or a clearly stated safety blocker.
11. **Remaining limitations:** `BLOCKING_CORE_E2E`, `BLOCKING_RUBRIC`, `SECURITY_BLOCKER`, `RUNTIME_LIMITATION`, `OPTIONAL_IMPROVEMENT`, with next owner decision only where genuinely necessary.

**Your marching order:** preserve proven PKG-T; complete and verify P1→S1→P2→P4 real GKE integration; validate/fix timeout conflicts based on actual traces; attempt B′ safely only with its own gate; deliver genuine evidence and coherent documentation; clean up and verify cloud sleep. Favor a working, secure, demonstrable core platform over indefinite speculative perfection. **Do not stop after writing code or printing a readiness report. Proceed through the approved execution gates.**
