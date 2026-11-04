# Fraud Investigation Agents, MCP Tools, Agent Registry and Gateways

This report describes the agent layer of the platform:

- the agents that answer an analyst's fraud question;
- the MCP tool servers that give them access to features, the fraud model and the policy knowledge base;
- the agentgateway instances that enforce who may call what;
- the Agent Registry where agents, tools and prompts are published;
- how the agents are deployed and operated on GKE.

## 1. Overview and motivation

An analyst asks a question in natural language. For example: *"Payment `dc58ff4e…` of 275,465 VND from account … at 2026-06-01 00:00 — is this fraudulent and which policy applies?"*

A good answer combines three things:

- the customer's recent behaviour, from the online feature store;
- the fraud model's score, from KServe;
- the relevant internal policy, from the RAG knowledge base.

The central design rule is that **the LLM never produces a business value**.

- The LLM interprets the question and chooses a tool.
- Every number in the answer comes from a deterministic tool call: features, probability, citations.
- If a required input is missing, or the question is out of scope, the agent refuses explicitly instead of guessing.

This keeps the explanation layer from altering or inventing the fraud score.

## 2. Architecture

```text
Analyst (browser)
  │ (1) chat message
  ▼
kagent UI ── (2) A2A message/stream ──▶ a2a-gateway (agentgateway; A2A routes, authorization, rate limit)
                                          │ (3) A2A task
                                          ▼
                       fraud-coordinator (BYO, 2 replicas, HPA 2–3)
                         │ (4) OpenAI tool call               │ (5) A2A delegation via a2a-gateway
                         ▼                                    ▼
                agentgateway → vLLM            feature-rag-specialist (BYO, 2 replicas, HPA 2–3)
                                                         │ (6) MCP tools/call
                                                         ▼
                                    mcp-gateway (agentgateway; per-tool authorization, rate limit)
                                                         │ (7) MCP (Streamable HTTP)
                                                         ▼
                                    feature-rag-mcp (FastAPI MCP server)
                                       │ (8a) online features   │ (8b) V2 inference   │ (8c) policy chunks
                                       ▼                        ▼                     ▼
                                 Feast Redis          KServe fraud-detector     RAG store (Feast/Redis)
```

A third agent, `drift-specialist`, is a Declarative kagent agent. It answers data-drift questions with the `drift-mcp` tool server, through the same mcp-gateway.

Every box is a separate Kubernetes Deployment. Workloads run in three namespaces: `ewallet-agents`, `ewallet-mcp` and `kagent`. Traffic between them is constrained by NetworkPolicies and Istio ambient AuthorizationPolicies, so the arrows above are the only allowed paths.

## 3. Implementation

### 3.1 Fraud Coordinator

[`byo_coordinator.py`](../../../ai_platform/llm_agent/byo_coordinator.py) is a "bring-your-own" (BYO) kagent agent. It is a Python A2A server that streams task updates to the kagent UI.

For a natural-language message, the bounded intake in [`nl_intake.py`](../../../ai_platform/llm_agent/nl_intake.py) works in four steps:

1. **PII screen.** The prompt passes through the PII screen first ([`prompt_safety.py`](../../../ai_platform/llm_agent/prompt_safety.py)). E-mail addresses, Vietnamese mobile numbers and Luhn-valid card numbers are replaced by typed placeholders before anything reaches the LLM.
2. **Tool selection.** The LLM is called through agentgateway with exactly one tool, `investigate_transaction`, using native OpenAI tool calling (vLLM `hermes` parser).
3. **Argument validation.** Every extracted argument must appear verbatim in the analyst's text, otherwise the request is rejected. This covers identifiers, the amount and the timestamp.
4. **Delegation.** The validated request is delegated to the Feature-RAG Specialist through the a2a-gateway. The Coordinator assembles the final answer from the specialist's structured result.

### 3.2 Feature-RAG Specialist

[`byo_feature_rag_specialist.py`](../../../ai_platform/llm_agent/byo_feature_rag_specialist.py) accepts the Coordinator's typed request. It calls the mcp-gateway with the official MCP Streamable HTTP client and uses three tools:

| Tool | Backend | Returns |
|---|---|---|
| `feature_lookup` | Feast online store (Redis) | The 31 historical features of the customer, account, device and merchant, each with its event timestamp |
| `fraud_scoring` | KServe `fraud-detector` (V2 protocol) | Fraud probability, prediction and model version |
| `knowledge_search` | RAG chunks published by the RAG ingestion pipeline | Policy passages with document ID, chunk ID, checksum and corpus revision |

Every tool result is validated before use. If a tool fails, for example because a customer has no online features, the specialist ends the task with an explicit `specialist_not_completed` result instead of producing a partial answer.

### 3.3 MCP tool servers (Web API)

The tools are served by a FastAPI application ([`tools/service.py`](../../../ai_platform/llm_agent/tools/service.py)). Tool implementations are in [`tools/`](../../../ai_platform/llm_agent/tools).

| Feature | Implementation |
|---|---|
| Validation | Each tool's arguments are a Pydantic model. Invalid input returns a typed 422 error instead of a stack trace |
| Concurrency | Handlers are `async def` on an ASGI server, so concurrent tool calls of one investigation are served without blocking |
| Health checks | `/health/live` and `/health/ready`; the Kubernetes liveness and readiness probes use them |
| Authentication | Every call must carry a valid service token. The gateway also checks the caller's identity |

The Fraud API ([`ai_platform/inference/app.py`](../../../ai_platform/inference/app.py)) follows the same pattern for direct scoring.

### 3.4 Gateways

All three gateways are [agentgateway](https://agentgateway.dev) instances rendered by the Helm chart:

- the **LLM gateway**, in front of vLLM; see [LLM Platform](../llm/LLM_PLATFORM.md);
- the **mcp-gateway**;
- the **a2a-gateway**.

Agents never call MCP servers or each other directly. This centralises authorization, rate limiting, logging and tracing.

## 4. Deployment and configuration

| Concern | File |
|---|---|
| Agent resources (BYO Coordinator, BYO Specialist, Declarative drift agent), replicas, PodDisruptionBudgets | [`templates/agents.yaml`](../../../infra/helm/llm-agent-platform/templates/agents.yaml) |
| Horizontal Pod Autoscalers (and a KEDA ScaledObject template; see §9) | [`templates/autoscaling.yaml`](../../../infra/helm/llm-agent-platform/templates/autoscaling.yaml) |
| MCP server Deployments (RollingUpdate, probes) | [`templates/mcp-services.yaml`](../../../infra/helm/llm-agent-platform/templates/mcp-services.yaml) |
| MCP and A2A gateway configuration | [`templates/policy-gateways.yaml`](../../../infra/helm/llm-agent-platform/templates/policy-gateways.yaml) |
| AuthorizationPolicies and NetworkPolicies | [`templates/security.yaml`](../../../infra/helm/llm-agent-platform/templates/security.yaml), [`templates/cross-namespace.yaml`](../../../infra/helm/llm-agent-platform/templates/cross-namespace.yaml) |

**Release units:**

- Helm release `llm-agent` (namespace `default`) installs the gateways, the agents and the observability stack.
- Helm release `llm-agent-mcp` installs the MCP servers in `ewallet-mcp`.

Agent and tool images are built by Cloud Build and pinned by digest:

- Coordinator: `g3-byo-coordinator@sha256:483dd943…`;
- Specialist: `g3-byo-feature-rag-specialist@sha256:5f848a1c…`.

**Multi-replica and autoscaling.** Each agent runs two replicas spread over two nodes. A HorizontalPodAutoscaler scales it between 2 and 3 replicas at 60 % CPU, and a PodDisruptionBudget keeps at least one replica available during voluntary disruptions.

![Grafana Explore: two pods per agent](../../evidence/edai2/screenshots/runtime/10_grafana_agent_pods_per_replica.png)

*Grafana Explore (Prometheus/cAdvisor) on the running cluster. The query `count by (pod) (container_memory_working_set_bytes{namespace="ewallet-agents", container="kagent"})` returns two pods for each agent:*

- *`fraud-coordinator-7dccdc448c-{vpnht,xlw8s}`;*
- *`feature-rag-specialist-855c67457d-{c66mn,dvttw}`;*
- *`drift-specialist-778b47fd6f-{fxjrn,stffm}`.*

The read-only `kubectl` output below shows the same state, together with the HPAs and PDBs. These images are **terminal renders**: the real command output, rendered as an image, not an application UI. The raw text is in [`runtime/raw/`](../../evidence/edai2/screenshots/runtime/raw), and the capture script is [`capture_runtime_state.sh`](../../evidence/edai2/screenshots/runtime/capture_runtime_state.sh).

![Agents: replicas, HPA, PDB (terminal render)](../../evidence/edai2/screenshots/runtime/01_agents_replicas_hpa_pdb.png)

*Agent Deployments 2/2; HPAs at CPU 60 %, min 2 / max 3; PDBs `minAvailable 1`.*

![kagent Agent resources (terminal render)](../../evidence/edai2/screenshots/runtime/02_kagent_agent_crs.png)

*kagent Agent resources: `fraud-coordinator` and `feature-rag-specialist` are BYO, `drift-specialist` is Declarative. All are Ready.*

![MCP Deployments and Helm releases (terminal render)](../../evidence/edai2/screenshots/runtime/04_mcp_rollingupdate.png)

*MCP servers use `RollingUpdate` with `maxUnavailable 0` and `maxSurge 1`, so a new version is started before the old one is removed. They use digest-pinned images. Helm releases: `llm-agent` revision 41, `llm-agent-mcp` revision 6.*

![Fraud API validation and health (terminal render)](../../evidence/edai2/screenshots/runtime/08_fraud_api_pydantic_and_health.png)

*`/health/live` and `/health/ready` return 200, and an invalid payload is rejected with 422 `invalid_request`.*

![kagent UI agent list](../../evidence/edai2/screenshots/runtime/09_kagent_ui_agents_r41.png)

*The kagent UI lists the deployed agents, with their roles and image digests.*

## 5. Agent Registry

The agents, MCP servers and system prompts are published to an [agentregistry](https://github.com/agentregistry-dev/agentregistry) v0.4.0 instance in the namespace `ewallet-prompts`. The registry is the catalogue where operators discover what is deployed and which prompt version an agent uses.

| Item | Location |
|---|---|
| Helm values | [`infra/helm/agentregistry/`](../../../infra/helm/agentregistry) |
| Catalogue publisher | [`ops/llm_agent/registry_catalog.py`](../../../ops/llm_agent/registry_catalog.py) |
| Prompt resolver | [`ops/llm_agent/prompt_registry.py`](../../../ops/llm_agent/prompt_registry.py) |

![Registry: agents](../../evidence/edai2/screenshots/agents/01_registry_catalog_agents.png)

*The three agents, published at version 1.0.0.*

![Registry: fraud-coordinator](../../evidence/edai2/screenshots/agents/02_registry_agent_fraud_coordinator.png)

*The Coordinator's entry.*

![Registry: MCP servers](../../evidence/edai2/screenshots/agents/03_registry_catalog_mcp_servers.png)

*The MCP servers `drift-mcp` and `feature-rag-mcp`.*

![Registry: prompts](../../evidence/edai2/screenshots/agents/04_registry_catalog_prompts.png)

*Versioned system prompts.*

![Registry: deployed view](../../evidence/edai2/screenshots/agents/05_registry_deployed.png)

*The deployed view.*

The registry entries were generated when the agents were planned as kagent SandboxAgents. Their description still uses that wording, while the deployed agents are BYO and Declarative Deployments; see §9.

## 6. Agent conversations in the kagent UI

The screenshots in this section are real conversations in the kagent UI on the GKE deployment.

![Analyst question](../../evidence/edai2/screenshots/agents/07_kagent_coordinator_prompt.png)

*An analyst question to the Coordinator (request `nl-eb00ade694f158ff85b004a35a9d1051`). The LLM selects `investigate_transaction` with arguments taken verbatim from the text.*

![Grounded answer](../../evidence/edai2/screenshots/agents/08_kagent_coordinator_answer.png)

*The final answer (trace `7e651a4f38c648bd2ca3ec22e33a5d7b`) contains:*

- *the customer's online features from Feast;*
- *the KServe fraud probability, 0.0639895560144887 (identical to the value the Fraud API returns for the same transaction);*
- *three citations from the policy corpus `fraud-policy-v1-20261030`.*

![Specialist conversation](../../evidence/edai2/screenshots/agents/09_kagent_specialist_answer.png)

*Chatting directly with the Feature-RAG Specialist (request `nl-d61817187a5d735cea1ba3737dce93cb`). The answer is built from real MCP calls to Feast, KServe and the RAG store.*

![Refusal: missing timestamp](../../evidence/edai2/screenshots/agents/10_kagent_rejection_missing_timestamp.png)

*A question without a transaction timestamp is refused, because a score without a point in time would be meaningless.*

![Refusal: unsupported question](../../evidence/edai2/screenshots/agents/11_kagent_rejection_unsupported.png)

*An out-of-scope question is refused.*

![PII-masked prompt](../../evidence/edai2/screenshots/agents/12_kagent_pii_prompt_answer.png)

*A prompt containing a synthetic e-mail address and phone number. Both are masked before the LLM call, counted by the PII metrics, and never echoed in the answer.*

### End-to-end workflow

One investigation, as recorded in Tempo trace `f341b410568fb649ee0f95bba50cd27d` (251 spans):

1. The coordinator receives the A2A task from kagent and calls the LLM gateway for tool selection.
2. It delegates through the a2a-gateway.
3. The specialist calls the mcp-gateway three times: `feature_lookup`, `fraud_scoring`, `knowledge_search`.
4. The MCP server reaches Feast, KServe and the RAG store.

The Grafana MCP panels count every tool call. Over the measurement window: `feature_lookup` 17, `fraud_scoring` 16, `knowledge_search` 19. See [Observability §7](../observability/OBSERVABILITY.md#7-agent-and-mcp-telemetry).

![Trace waterfall](../../evidence/edai2/screenshots/agents/14_tempo_trace_waterfall.png)

*Tempo waterfall of the investigation: fraud-coordinator → a2a-gateway → feature-rag-specialist → mcp-gateway → feature-rag-mcp → Feast / KServe / RAG.*

## 7. MCP Gateway and A2A Gateway security

Policy configuration: [`policy-gateways.yaml`](../../../infra/helm/llm-agent-platform/templates/policy-gateways.yaml).

| Gateway | Policy | Reason |
|---|---|---|
| mcp-gateway | `mcpAuthorization` rules (l.126). The caller's mesh identity (namespace + ServiceAccount) must match, **and** `mcp.tool.name` must be in that agent's allowed list (l.90) | Least privilege per agent: each agent may call only the tools on its own list, and a token issued for one agent is rejected by another agent's MCP server (see the test below) |
| mcp-gateway | The chart refuses to render unless `authorizationMode` is `mesh-identity` or `ambient-backend-token` (l.53–58) | An unauthenticated gateway cannot be deployed by mistake |
| mcp-gateway | `localRateLimit` per MCP server (l.106) | Protects Feast, KServe and Redis behind the tools from a runaway agent loop |
| a2a-gateway | One route per agent with `a2a: {}` and `authorization` on the caller identity (l.171–200) | Only the Coordinator, and the kagent UI, may start a specialist task |
| a2a-gateway | `kagent-a2a` route on `/api/a2a/<namespace>/` (l.204), with rate limit and authorization | The kagent UI reaches agents only through the gateway |
| a2a-gateway | The chart fails if `requestTimeout ≤ 2 × LLM timeout + MCP timeout` (l.34) | A delegation can never time out before the LLM and tool calls inside it |

Requests sent directly to the mcp-gateway with a missing, revoked or wrong-agent token are all rejected:

| Request | Route | Response |
|---|---|---|
| No token | `/feature-rag/mcp` | 401 `authentication_required` |
| No token | `/drift/mcp` | 401 `authentication_required` |
| Revoked token | `/feature-rag/mcp` | 401 `authentication_failed` |
| Specialist token used on the drift server | `/drift/mcp` | 401 `authentication_failed` |

The results are in [`text/mcp_gateway_negative_auth.txt`](../../evidence/edai2/text/mcp_gateway_negative_auth.txt).

![Gateway spans](../../evidence/edai2/screenshots/agents/13_tempo_gateway_spans_table.png)

*Spans emitted by the a2a-gateway, the mcp-gateway and the LLM gateway inside one business trace. This confirms that the agents' traffic actually passes through the gateways.*

## 8. Sandbox and warm worker pool (pilot)

The platform installs the Substrate components for running agents in isolated sandboxes:

| Component | Name |
|---|---|
| CRDs | `actortemplates.ate.dev`, `sandboxconfigs.ate.dev`, `workerpools.ate.dev`, `sandboxagents.kagent.dev` |
| gVisor runtime class | `gvisor-default` (default SandboxConfig) |
| Worker pool | `kagent-pilot`, one ready worker |

A pilot `SandboxAgent` in the namespace `kagent-pilot` runs under gVisor on that pool, backed by a mock model. Manifests are in [`infra/kubernetes/kagent-pilot/`](../../../infra/kubernetes/kagent-pilot) (see [`sandbox-agent.yaml`](../../../infra/kubernetes/kagent-pilot/manifests/sandbox-agent.yaml)).

![Substrate state (terminal render)](../../evidence/edai2/screenshots/runtime/03_sandbox_substrate.png)

*The Substrate CRDs, the Ready and Accepted pilot SandboxAgent, the `kagent-pilot` WorkerPool (1/1) and the gVisor SandboxConfig.*

## 9. Limitations

- **Sandboxing.** The three business agents run as ordinary Deployments; only the pilot agent runs inside the gVisor sandbox. Moving the business agents onto SandboxAgents, and demonstrating many agents on a small worker pool, are future work.
- **Autoscaling.** Autoscaling uses the Kubernetes HPA on CPU. A KEDA ScaledObject template, with a fallback replica count, exists in the chart, but KEDA is not installed in the cluster. No load test has been run to observe a scale-out event.
- **MCP replicas.** Each MCP server runs a single replica. RollingUpdate prevents downtime during upgrades, but there is no automatic fallback if the server fails.
- **Registry wording.** The registry descriptions still refer to SandboxAgents. They should be updated at the next publication.
