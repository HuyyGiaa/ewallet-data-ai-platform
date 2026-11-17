# Agent Platform — Architecture, Deployment, Business Workflows & Governance

This is the main technical report of the EDAI-2 Agent Platform. It describes the platform as it runs on GKE after
campaign `lc-20261117T002220Z` (2026-11-17), with Helm releases `llm-agent` rev 59 and `llm-agent-mcp` rev 19.

**The Agent Platform is documented in three reports:**

| Document | Content |
|---|---|
| This report | Business problem, architecture, technology choices, deployment, registry, tools, Coordinator, gateways, availability, observability, CI/CD, results and limitations |
| [Agent Fleet Architecture](kagent_bounded_pilot.md) | Substrate, SandboxAgents, the shared gVisor WorkerPool, sessions, placement, autoscaling, capacity, startup and cost |
| [Owner evidence replay](OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md) | The operational guide: wake, commands, PASS/FAIL conditions, screenshots, guarded sleep |

The dated campaign record, with every measurement and decision, is
[AGENT_PLATFORM_COMPLETION.md](AGENT_PLATFORM_COMPLETION.md).

**Status markers used here:**
- **Measured on GKE**, with the campaign named: a result from a recorded campaign.
- **`OWNER_EVIDENCE_PENDING`**: a place for a screenshot or measurement that the owner captures in the next replay.
  Nothing there is claimed yet.
- **Prepared, not deployed**: source and procedure exist, but nothing has run on the cluster.

## 1. Business problem

A fraud analyst asks a question in natural language. For example: *"Payment `dc58ff4e…` of 275,465.53 VND from account
… at 2026-06-01 00:00 UTC — how risky is it, and which policy applies?"* A second kind of question is about data
quality: *"Has `account_tx_count_1h` drifted from its April baseline in May?"*

A trustworthy answer needs four independent sources:

- the customer's recent behaviour, from the online Feature Store;
- the fraud model's score, from KServe;
- the relevant internal policy, from the RAG knowledge base;
- for drift questions, a PSI computed from versioned baseline and window artifacts.

The design rule is that **the LLM never produces a business value.**
- The LLM only interprets the question and selects a tool.
- Every number in the answer comes from a deterministic tool call.
- A request with a missing or ungrounded argument is refused, not guessed.

## 2. Architecture

```text
Analyst ── kagent UI ── A2A Gateway (agentgateway) ── kagent controller
                                                         │
                                       fraud-coordinator (BYO Agent, ewallet-agents, KEDA 2–3)
                         one tool-selection call │                       │ delegation
                 LLM Gateway (agentgateway) → vLLM            A2A Gateway /api/a2a-sandboxes/ewallet-sandbox/<agent>/
                 (ModelConfig ewallet-global-llm)                        │
                                                                  kagent controller
                                                  ┌──────────────────────┴──────────────────────┐
                                    feature-rag-specialist                            drift-specialist
                                        (SandboxAgent)                                  (SandboxAgent)
                                                  └──── shared WorkerPool ewallet-sandbox/business-agents ────┘
                                                        Substrate actors, gVisor, KEDA 2–3
                                                                        │ ateom egress proxy
                                                                   MCP Gateway (agentgateway)
                                                       ┌────────────────┴────────────────┐
                                     ewallet-mcp/feature-rag-mcp (2 replicas)   ewallet-mcp/drift-mcp (2 replicas)
                                       │ feature_lookup  │ fraud_scoring  │ knowledge_search    │ drift_detection
                                     Feast online   Feast + KServe V2   RAG index (Feast)   pinned baseline/window artifacts
```

| Layer | Runtime object | Namespace |
|---|---|---|
| Entry | kagent UI, kagent controller | `kagent` |
| Coordinator | BYO kagent Agent `fraud-coordinator` (Deployment, 2 replicas at rest) | `ewallet-agents` |
| Gateways | `a2a-gateway`, `mcp-gateway`, `agentgateway` (LLM) | `ewallet-agents` |
| Specialists | SandboxAgents `feature-rag-specialist`, `drift-specialist` | `ewallet-sandbox` |
| Agent fleet | WorkerPool `business-agents` (Substrate, gVisor) | `ewallet-sandbox` |
| Tools | MCP servers `feature-rag-mcp`, `drift-mcp` | `ewallet-mcp` |
| Registry | agentregistry `prompt-registry-agentregistry` | `ewallet-prompts` |

**Request path for a fraud question:**
1. The kagent UI sends an A2A message through the A2A Gateway to the Coordinator.
2. The Coordinator screens the prompt for PII and makes one LLM tool-selection call through the LLM Gateway.
3. It validates every argument against the analyst's text.
4. It delegates a typed request through the A2A Gateway's Sandbox route to the kagent controller.
5. The controller activates a Substrate session actor of `feature-rag-specialist` on a shared worker.
6. The actor calls the MCP Gateway three times: `feature_lookup`, `fraud_scoring` and `knowledge_search`.
7. `feature-rag-mcp` reads Feast, calls KServe and searches the RAG index.
8. The Coordinator assembles the answer from the structured result.

A drift question takes the same path to `drift-specialist` and `drift_detection`. A combined request runs both
Specialists concurrently.

Traffic between namespaces is default-deny. Each arrow above is opened by one NetworkPolicy and one Istio Ambient
AuthorizationPolicy (§9).

## 3. Technology choices

| Need | Choice | Why |
|---|---|---|
| Agent runtime with a UI and A2A | kagent 0.10.3 | A2A server and client, a chat UI, Agent and SandboxAgent CRDs. BYO agents keep the logic in tested Python |
| Coordinator logic | BYO agent ([`byo_coordinator.py`](../../../ai_platform/llm_agent/byo_coordinator.py)) | Deterministic routing, grounding and idempotency cannot be left to a prompt |
| Specialist isolation and density | SandboxAgents on Substrate 0.0.9 | gVisor isolation per session; many session actors share a few warm workers, suspended to snapshots between turns ([Agent Fleet](kagent_bounded_pilot.md)) |
| Policy enforcement | agentgateway (three instances) | MCP-aware authorization (`mcp.tool.name`), A2A routing and rate limits in one place; agents never call tools or each other directly |
| Tool protocol | MCP (Streamable HTTP), FastAPI + Pydantic | Standard client, typed validation, health probes |
| Autoscaling | KEDA 2.21 | Scales on business demand (in-flight tasks, Sandbox delegations), not CPU, for both a Deployment and the WorkerPool |
| Network identity | Istio Ambient (ztunnel) | SPIFFE identity per ServiceAccount without sidecars in the agent pods |
| Catalog | agentregistry v0.4.0 | Versioned agents, MCP servers and prompts with a REST readback |

## 4. Deployment on GKE

- **Cluster:** `ewallet-dev` (`asia-southeast1-b`), node pool `cpu-pool` of e2-standard-4.
  - The cluster is kept asleep between campaigns (0 nodes).
  - It is woken to 5 nodes from a recorded recovery manifest with `ops/gcp/cloud_lab.py`.
- **Releases:**
  - `llm-agent` (namespace `default`): gateways, Coordinator, policies, observability.
  - `llm-agent-mcp` (namespace `ewallet-mcp`): the two MCP servers.
  - The Sandbox fleet (SandboxAgents, WorkerPool, ScaledObject) is applied from
    [`infra/kubernetes/agent-sandbox/`](../../../infra/kubernetes/agent-sandbox).
- **Images:** built by Cloud Build, scanned by Trivy, pinned by digest.
  - Coordinator: `g3-byo-coordinator@sha256:94a13a8b…`.
  - SandboxAgent fleet: `g3-byo-feature-rag-specialist@sha256:29867794…`, `g3-byo-drift-specialist@sha256:75011f6c…`.

| Concern | Source |
|---|---|
| Coordinator Agent, PDB, prompts | [`templates/agents.yaml`](../../../infra/helm/llm-agent-platform/templates/agents.yaml) |
| KEDA ScaledObject of the Coordinator | [`templates/autoscaling.yaml`](../../../infra/helm/llm-agent-platform/templates/autoscaling.yaml) |
| MCP Deployments (RollingUpdate, probes, PDB) | [`templates/mcp-services.yaml`](../../../infra/helm/llm-agent-platform/templates/mcp-services.yaml) |
| MCP and A2A gateway policies | [`templates/policy-gateways.yaml`](../../../infra/helm/llm-agent-platform/templates/policy-gateways.yaml) |
| NetworkPolicies, AuthorizationPolicies | [`templates/security.yaml`](../../../infra/helm/llm-agent-platform/templates/security.yaml), [`templates/cross-namespace.yaml`](../../../infra/helm/llm-agent-platform/templates/cross-namespace.yaml) |
| SandboxAgents, WorkerPool, WorkerPool ScaledObject | [`infra/kubernetes/agent-sandbox/`](../../../infra/kubernetes/agent-sandbox) |

**Changes are applied as gated packages** ([`agi_campaign.sh`](../../../scripts/llm_agent/agi_campaign.sh)). Each
package goes through a preflight, a render diffed against the live manifest with the exact expected object identities,
a server-side dry run, a recorded owner approval, and `helm upgrade --atomic`. Every gate and rollback refuses a
manifest that names a retired credential ([`retired_credentials.py`](../../../ops/llm_agent/retired_credentials.py)).

**Prepared, not deployed: `lf-agents`.** It removes the 9 leftover policies and the ServiceAccount of the
long-disabled in-release `ewallet-agents/feature-rag-mcp`, and deletes 2 unused `ewallet-agents` auth tables. It
would take `llm-agent` to rev 60. It needs owner approval
([runbook §2.6g](../../runbooks/edai2/AGENT_PLATFORM_MASTER_EVIDENCE_RUNBOOK.md#26g-final-orphan-and-auth-table-cleanup-lf-agents-proposed-not-executed)).

## 5. Agent Registry and ModelConfig

**Agent Registry.** agentregistry v0.4.0 in `ewallet-prompts` is the catalog of what is deployed. It holds 3 Agents,
2 MCP servers and versioned system prompts.

| Entry | Version | Description |
|---|---|---|
| Agent `fraud-coordinator` | 1.1.0 | BYO kagent agent; label carries the deployed image digest |
| Agent `feature-rag-specialist` | 1.0.0 | SandboxAgent runtime on the shared WorkerPool |
| Agent `drift-specialist` | 1.1.0 | SandboxAgent runtime, deterministic (no LLM) |
| MCPServer `feature-rag-mcp`, `drift-mcp` | v1 | MCP Gateway routes `/feature-rag/mcp`, `/drift/mcp` |
| Prompts | v5 / v5 / v2 | `fraud-coordinator-system`, `feature-rag-specialist-system`, `drift-specialist-system` |

- **Catalog code:** [`registry_catalog.py`](../../../ops/llm_agent/registry_catalog.py), with `render`, `publish` and
  `readback`. The catalog is rendered from the live Helm values and the live SandboxAgent image.
- **Strict readback** (GET only) compares every entry with the runtime. Measured on GKE: 8/8 match, before and after
  the final cleanup (`lc-20261117T002220Z`). The replay helper's `registry` command exits non-zero on any mismatch.
- **Prompt resolver:** [`prompt_registry.py`](../../../ops/llm_agent/prompt_registry.py).

**ModelConfig.** The shared `ewallet-global-llm` ModelConfig names the model and the LLM Gateway.
- The Coordinator resolves it at startup through the BYO resolver (`modelConfig.byoResolver`). The ModelConfig uid
  and generation were verified on GKE (AGI-01).
- The Specialists make no LLM call on the typed path.
- Details: [LLM_PLATFORM §5](../llm/LLM_PLATFORM.md#5-global-modelconfig-and-byo-resolver).

## 6. MCP tools and business integrations

### 6.1 MCP tool servers (Web API)

The tools are served by a FastAPI application, [`tools/service.py`](../../../ai_platform/llm_agent/tools/service.py).

| Property | Implementation |
|---|---|
| Validation | Each tool's arguments are a Pydantic model; invalid input returns a typed error, never a stack trace |
| Concurrency | `async def` handlers on an ASGI server |
| Health | `/health/live`, `/health/ready`, used by the Kubernetes probes; readiness is 503 until a credential table is loaded |
| Authentication | A service token from the server's own immutable table (§9); per-tool grants per subject |
| Audit | One `tool_call_completed` record per call: `request_id`, `trace_id`, tool, `actor_id` (the credential subject), status, duration |

| Tool | Server | Backend | Returns |
|---|---|---|---|
| `feature_lookup` | feature-rag-mcp | Feast online store (Redis) | the customer, account, device and merchant features, each with its event timestamp |
| `fraud_scoring` | feature-rag-mcp | Feast + KServe `fraud-detector` (V2) | probability, prediction, model name and version, input schema digest |
| `knowledge_search` | feature-rag-mcp | RAG chunks in Feast (published by the RAG pipeline) | policy passages with document, chunk, checksum and corpus revision |
| `drift_detection` | drift-mcp | versioned baseline registry and window extract | PSI, bins, thresholds, classification, dataset revisions |

### 6.2 Feature-RAG targets

`isolated` profile, in [`tools/service.py`](../../../ai_platform/llm_agent/tools/service.py):
- Separate Secrets and Redis databases for the Fraud and RAG targets, with explicit registries.
- The production Fraud database (DB 0) needs an explicit opt-in.
- `/v1/capabilities` reports the non-secret target identity.

**Access guarantee:**

| Layer | Guarantee |
|---|---|
| Application | The MCP server issues only Feast online reads |
| Redis credential | Not restricted: the Feature Store Redis has one password user and no ACLs |

### 6.3 Drift artifacts

[`drift_sources.py`](../../../ai_platform/llm_agent/tools/drift_sources.py) and
[`drift_artifacts.py`](../../../ops/llm_agent/drift_artifacts.py).

| Artifact | Schema | Pinned by |
|---|---|---|
| Baseline registry | `ewallet-drift-baselines/v1` (cut points, reference proportions, thresholds, source digest) | `EWALLET_DRIFT_BASELINES_SHA256` |
| Window extract | `ewallet-drift-window/v1` (coverage, sampling, file digests) | `EWALLET_DRIFT_WINDOW_MANIFEST_SHA256` |

The server fails closed in three ways:
- an unknown baseline, a baseline of another feature, or an overlapping or out-of-coverage window is `rejected`;
- too few observations gives `insufficient_data`, with no PSI;
- a wrong digest or a synthetic artifact is `unavailable`.

The deployed drift artifacts come from the immutable snapshot `feast-r5gb-20261111-214544`, so a drift result is a
statement about that dataset.

## 7. Coordinator and A2A delegation

**Contracts:** [`business_request.py`](../../../ai_platform/llm_agent/business_request.py) and
[`programmatic_coordinator.py`](../../../ai_platform/llm_agent/programmatic_coordinator.py).

| Contract | Wire | Target |
|---|---|---|
| `g3-business-request/v1` | `CALL_AGENT=feature-rag-specialist` + `REQUEST_JSON=` | Feature-RAG: transaction, feature projection, policy query |
| `g3-drift-request/v1` | `CALL_AGENT=drift-specialist` + `REQUEST_JSON=` | Drift: monitored feature, immutable `baseline_id`, window of at most 31 days |
| `g3-workflow-request/v1` | `CALL_WORKFLOW=combined` | both: one fraud member `<id>.fraud` and one drift member `<id>.drift` |

**Results:**
- Fraud only returns `g3-orchestration/v1` (`status` `ok`, `partial` or `unavailable`).
- Drift and combined return `g3-orchestration/v2`. It carries `workflow`, `status` (`complete`, `partial` or
  `unavailable`), one section per Specialist with its own `request_id`, status, evidence and duration, and the
  `unavailable` entries.
- A failed member is recorded, never reconstructed.

**Natural-language intake** ([`workflow_intake.py`](../../../ai_platform/llm_agent/workflow_intake.py),
[`nl_intake.py`](../../../ai_platform/llm_agent/nl_intake.py)):
1. **PII screen** ([`prompt_safety.py`](../../../ai_platform/llm_agent/prompt_safety.py)).
2. **One LLM call** offering only the tools the deterministic screen allows.
3. **Grounding:** every argument must appear verbatim in the question.
4. **Deterministic routing.**

A combined natural-language question is rejected (`ungrounded_argument`): strict grounding is kept by owner decision.

**Delegation:**
- **Route:** the Coordinator sends each typed request to `/api/a2a-sandboxes/ewallet-sandbox/<agent>/` on the A2A
  Gateway.
- **Idempotency:** it covers the whole workflow. A request id that is still running is answered
  `idempotency_in_progress`.
- **Full worker pool:** when no Sandbox worker is free, kagent's refusal is retried with jittered backoff inside the
  delegation timeout (`coordinator.delegation_wait`, counter `ewallet_sandbox_admission_waits_total`).
- **Demand metric:** `ewallet_sandbox_delegations_inflight` counts logical delegations in flight. It is the WorkerPool
  KEDA trigger.

## 8. Feature Store, KServe, RAG and Drift in the workflows

Measured on GKE (`lc-20261117T002220Z`), through the Sandbox routes:

| Workflow | Result | Backends |
|---|---|---|
| Typed fraud | `ok`, `fraud_probability 0.0639895560144887`, 4 features at `2026-05-31T23:59:56Z`, 3 policy citations | Feast, KServe `fraud-detector` v1, RAG |
| Typed drift | `complete`, PSI `0.0004795543627041439`, `no_drift`, 19338 observations | drift artifacts |
| Typed combined | `complete`, `unavailable: []`, both Specialists `ok` | all four tools |
| NL fraud / NL drift | `ok` / `complete`, same values | as above, plus the LLM Gateway |

- The values equal the direct backend probes of the AGI campaign.
- Unknown entities return a truthful `missing_feature` and no invented score.
- Out-of-scope questions are refused before any tool call.

Evidence for the next replay: `AGENT_B01`–`AGENT_B04` (§13), `OWNER_EVIDENCE_PENDING`.

## 9. Gateway security

| Gateway | Policy | Reason |
|---|---|---|
| MCP Gateway | `authorizationMode: ambient-backend-token` | ztunnel enforces the caller's SPIFFE identity at L4; the server applies the token subject's per-tool grants |
| MCP Gateway | rate limit per MCP server | protects Feast, KServe and Redis from a runaway loop |
| A2A Gateway | port 8083 admits only `sa/fraud-coordinator`; exact Sandbox routes per Specialist; the kagent UI route | only the Coordinator can start a Specialist task; undeclared hosts get 404 |
| A2A Gateway | `requestTimeout > 2 × LLM timeout + MCP timeout` (chart guard) | a delegation never times out before its inner calls |
| Namespaces | default-deny NetworkPolicies + Istio ALLOW per flow | every hop is opened explicitly |

**Credentials.** Each MCP server reads an immutable credential table (`mcp-auth-<server>-fin-20261117`), and only two
kinds of subject hold credentials:
- `sandbox-feature-rag` and `sandbox-drift` (the SandboxAgent identities): granted their own tools;
- the kagent controller: list-only.

The nine legacy Specialist credentials are revoked, and their tables and client Secrets deleted
(`closure-20261116T211833Z`, `lc-20261117T002220Z`).

**Sandbox isolation:**
- Actors run under gVisor.
- The worker ServiceAccount mounts no token and has discovery-only RBAC.
- Actor egress leaves only through the ateom proxy to cluster Services.

Details: [Agent Fleet §5](kagent_bounded_pilot.md#5-the-shared-gvisor-workerpool).

**AGI-08 negatives, each with a working control** (measured on GKE, 3/3):

| Negative | Expected | Control |
|---|---|---|
| `sandbox-feature-rag` credential on drift-mcp | 401 at `initialize` | `sandbox-drift` lists the tool |
| kagent controller (list-only) calls `drift_detection` | JSON-RPC `-32602 Unknown tool` | the same principal lists the tool |
| undeclared A2A host `payments-agent.ewallet-agents` | 404, not routed | the Sandbox route returns 200 |

Laptop port-forwards bypass the Ambient caller check. The negatives therefore prove server credentials, grants and
gateway routes; the SPIFFE caller policies are the policy objects above.

## 10. MCP availability and rollback

- **Replicas:** each MCP server runs 2 replicas on different nodes, with `RollingUpdate maxUnavailable 0` and a PDB
  that allows 1 disruption.
- **Rolling update** (AGR-03, measured on GKE): 170/170 typed calls succeeded during a production rolling update.
- **Failover** (AGR-02, isolated namespace `ewallet-mcp-exp`, a pod force-deleted at 40 s):

  | Arm | Result |
  |---|---|
  | Optimized (2 replicas, client retry) | 1188/1188 calls, 1 client retry |
  | Baseline (1 replica) | 1129/1189 calls, 12 s outage |

  Production `ewallet-mcp` was unchanged.
- **Client failover:** the agents' MCP client retries only the shed request, in session, on gateway 429 or the
  in-protocol rate limit.
- **Rollback rules** (runbook §2.6f):
  - `llm-agent-mcp` ≤ 17 and `llm-agent` ≤ 57 are intentionally nonfunctional: they name deleted tables or Secrets,
    and every project script refuses them.
  - Fix forward from the live values.

## 11. Observability

| Signal | Where |
|---|---|
| Traces | Tempo. A combined business trace holds `agentgateway`, `kagent-controller`, `fraud-coordinator`, `feature-rag-specialist`, `drift-specialist`, `feature-rag-mcp` and `drift-mcp`: 228 spans, 21 of them from the kagent controller (`lc-20261117T002220Z`) |
| Agent metrics | `ewallet_agent_invocations_total`, `ewallet_agent_inflight_tasks`, `ewallet_sandbox_delegations_inflight`, `ewallet_sandbox_admission_waits_total` |
| MCP metrics | `ewallet_ewallet_mcp_tool_calls_total` and latency, on dashboard **E-Wallet Agent & MCP** (`ewallet-agent-mcp`) |
| LLM metrics | tokens, TTFT, latency on **E-Wallet LLM Runtime** ([Observability §6](../observability/OBSERVABILITY.md#6-llm-telemetry)) |
| Logs | Loki: Coordinator audit (`coordinator.task_completed`), MCP audit (`tool_call_completed`) |

| Span | Attributes |
|---|---|
| `g3.coordinator.execute` | `g3.request_id`, `ewallet.agent`, `ewallet.result.status` |
| `g3.coordinator.nl_intake.llm_tool_selection` | `gen_ai.*` (model, tools, tokens) |
| `g3.coordinator.delegate` | `ewallet.specialist`, `ewallet.gateway`, `ewallet.status` |
| `g3.feature_rag_specialist.execute`, `g3.drift_specialist.execute` | `g3.request_id`, MCP child spans |
| `mcp.tool <tool>` | `mcp.tool.name`, `mcp.outcome`, `ewallet.backend.operation` |

No prompt text, token or connection string is recorded.

## 12. CI/CD integration and actual deployment status

- **CI:** Jenkins job `edai2-agent-ci` runs the `mcp-agents`, `agents` and `prompt-management` suites, plus a negative
  gate ([CI/CD](../cicd/CICD.md)).
- **CD:** the Jenkins delivery jobs for the MCP servers, the agents and the Coordinator are defined but **have not been
  executed**. Rows 34–35 remain open for CD.
- **Actual deployment:** releases are applied by an operator through the gated packages of §4. Each run is recorded
  with an exact diff, a server-side dry run and an approval in a dated campaign. `scripts/llm_agent/rollback.sh`
  (Jenkins) and `deploy_atomic.sh` carry the same retired-credential guard.

## 13. Results, screenshots and rubric mapping

The verdicts below come from recorded GKE campaigns. New screenshots of the final configuration are captured by the
owner with the [replay guide](OWNER_AGENT_PLATFORM_EVIDENCE_REPLAY.md#screenshot-checklist). Until then every
screenshot cell is `OWNER_EVIDENCE_PENDING`.

| Row | Requirement | Measured on GKE (campaign) | Owner screenshots |
|---|---|---|---|
| 8, 21, 26 | Agent Registry; publish agents and Coordinator | strict readback 8/8 (`lc-20261117T002220Z`) | `AGENT_A04`, `AGENT_A05`, `AGENT_A06` — `OWNER_EVIDENCE_PENDING` |
| 16, 17 | FastAPI + Pydantic + health; async | health and validation through the gateway (AGI-03); concurrent tool calls | `AGENT_A02`, `AGENT_G01` — `OWNER_EVIDENCE_PENDING` |
| 18 | MCP on Kubernetes with Helm, RollingUpdate, fallback | 2 replicas + PDB; AGR-03 170/170; AGR-02 failover 1188/1188 | `AGENT_G01` — `OWNER_EVIDENCE_PENDING`; failover `AGENT_G02` is conditional |
| 19 | Agent with MCP, multi-replica, KEDA | WorkerPool KEDA 2→3→2 (B′, `lc`); Specialists only as SandboxAgents | `AGENT_D01`, conditional `AGENT_D02`–`D04` — `OWNER_EVIDENCE_PENDING` |
| 20 | Agent in Sandbox | business SandboxAgents on gVisor; MCP `actor_id sandbox-*` | `AGENT_A02`, `AGENT_B02`, `AGENT_C01` — `OWNER_EVIDENCE_PENDING` |
| 22 | Online features + KServe + RAG agent | 5/5 business E2E with real backends | `AGENT_B01`, `AGENT_B02`, `AGENT_F03` — `OWNER_EVIDENCE_PENDING` |
| 23 | MCP Gateway + security | per-tool grants, credentials, negatives 3/3 | `AGENT_C02`, `AGENT_C03` — `OWNER_EVIDENCE_PENDING` |
| 24 | A2A Gateway + security | Sandbox routes, caller policy, 404 negative | `AGENT_C02`, `AGENT_C03` — `OWNER_EVIDENCE_PENDING` |
| 25 | Coordinator multi-replica + autoscaling | 2 replicas; KEDA 2→3→2 under load; scale-out pod served tasks (`rem`, B′, `lc`) | `AGENT_A01`, `AGENT_D01`, `AGENT_B04` — `OWNER_EVIDENCE_PENDING` |
| 27 | Agent fleet, warm pool, Substrate | shared WorkerPool; sharing 12/12; B′ 0.185 vs 0.128 task/s ([Fleet](kagent_bounded_pilot.md)) | `AGENT_E02`, conditional `AGENT_E01` — `OWNER_EVIDENCE_PENDING` |
| 34, 35 | CI/CD for MCP, agents, Coordinator | CI executed; CD not executed | conditional `AGENT_H01` — `OWNER_EVIDENCE_PENDING` |
| 40, 41 | Agent UI and Registry UI behind NGINX | UIs work through port-forward; the edge gateway is validated on kind only and **not deployed** | `AGENT_B01`, `AGENT_A04` show the UIs, not the edge |
| 49 | Distributed traces | one trace across gateways, Coordinator, controller, SandboxAgents, MCP | `AGENT_F03` — `OWNER_EVIDENCE_PENDING` |
| 50 | LLM telemetry | tokens, TTFT, latency | `AGENT_F02` — `OWNER_EVIDENCE_PENDING` |
| 51 | Agent telemetry | calls per agent and tool, failures | `AGENT_F01`, `AGENT_F04` — `OWNER_EVIDENCE_PENDING` |

The status of each row in [RUBRIC_EVIDENCE_INDEX.md](../RUBRIC_EVIDENCE_INDEX.md) changes only after the owner's
evidence is reviewed.

## 14. Verified limitations

- **Edge:** no public UI endpoint. The kagent UI and the Registry UI are reached by port-forward. The Istio edge is
  validated locally only (rows 40–41).
- **CD:** the Jenkins CD jobs for agents and MCP servers were not executed (rows 34–35).
- **Redis:** access is read-only at the application level only (§6.2).
- **Natural language:** combined NL questions are rejected; the CPU model's argument extraction limits them.
- **Pending cleanup:** the 9 leftover objects of the disabled in-release `feature-rag-mcp` (they select no pod) and
  2 unused auth tables remain until `lf-agents` is approved (§4).
- **Rollback:** a manual `helm rollback` or `kubectl rollout undo` outside the project scripts can reach a revision
  that names deleted Secrets. It fails closed and cannot re-trust a retired credential.
- **Fleet limits:** see [Agent Fleet §13](kagent_bounded_pilot.md#13-results-screenshots-and-limitations).

## Appendix: historical architecture and evidence

Before 2026-11-16 the Specialists were also BYO Deployments: `feature-rag-specialist` (BYO) and `drift-specialist`
(first Declarative, then BYO). They were reached by `x-kagent-host` routes, with HPAs and later KEDA on all three
agents, and the MCP servers had a single replica. `sp-cleanup` (rev 55) removed those Deployments. The legacy MCP
cleanup (rev 59) removed the unused in-release `ewallet-agents/drift-mcp`.

The screenshots below were captured on those earlier runtimes. They remain valid for what they showed then, but they
**are not evidence of the final configuration**:

| Earlier evidence | What it showed |
|---|---|
| [Registry agents, v1.0.0](../../evidence/edai2/screenshots/agents/01_registry_catalog_agents.png), [MCP servers](../../evidence/edai2/screenshots/agents/03_registry_catalog_mcp_servers.png), [prompts](../../evidence/edai2/screenshots/agents/04_registry_catalog_prompts.png) | the registry before the 2026-11-16 republish |
| [Coordinator answer in the kagent UI](../../evidence/edai2/screenshots/agents/08_kagent_coordinator_answer.png), [refusals](../../evidence/edai2/screenshots/agents/10_kagent_rejection_missing_timestamp.png), [PII masking](../../evidence/edai2/screenshots/agents/12_kagent_pii_prompt_answer.png) | grounded answers and refusals with the BYO Specialist |
| [Tempo waterfall, trace `f341b410…`](../../evidence/edai2/screenshots/agents/14_tempo_trace_waterfall.png), [gateway spans](../../evidence/edai2/screenshots/agents/13_tempo_gateway_spans_table.png) | gateway traffic in a business trace before the Sandbox routes |
| [Agent Deployments and HPAs](../../evidence/edai2/screenshots/runtime/11_agents_hpa_pdb_live_runtime.png), [Fraud API health](../../evidence/edai2/screenshots/runtime/08a_fraud_api_health_live.png) | N15 configuration of the BYO Specialist runtime |
| [MCP negative authentication](../../evidence/edai2/text/mcp_gateway_negative_auth.txt) | 401 responses with the legacy tokens, now retired |
| [Sandbox pilot capture](../../evidence/edai2/screenshots/runtime/03_sandbox_substrate_live_runtime.png) | the mock-model pilot ([Fleet §2](kagent_bounded_pilot.md#2-the-initial-bounded-pilot-2026-11-02)) |

The AGI-era contract map and the pre-campaign audit are archived; see
[HISTORICAL_DOCUMENTS.md](../HISTORICAL_DOCUMENTS.md).
