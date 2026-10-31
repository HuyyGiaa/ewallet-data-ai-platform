# kagent Coordinator and Specialists

Status: **SOURCE_READY + TEST_PASS. Not deployed.** Disabled by default
(`agents.enabled: false`). kagent 0.10.3, Agent Substrate 0.0.9.

## Agents

| Resource name | Business role | Rubric | Tools (allow-list) |
|---|---|---|---|
| `drift-specialist` | Feature / Risk specialist | 19–21 | `feature-rag-mcp`: `feature_lookup`; `drift-mcp`: `drift_detection` |
| `feature-rag-specialist` | Fraud Investigation / Evidence specialist | 22 | `feature-rag-mcp`: `feature_lookup`, `fraud_scoring`, `knowledge_search` |
| `fraud-coordinator` | Coordinator | 25–26 | `type: Agent` → both specialists (native kagent A2A); no MCP tools |

The resource names are the existing source names (release-v1.json, chart
ServiceAccounts). They are not duplicated under the business names; the
business role is the `ewallet.io/display-name` annotation and the SandboxAgent
description.

Each agent is a `SandboxAgent` (gVisor sandbox, rubric row 20):

- `modelConfig: ewallet-global-llm`, which routes ModelConfig → agentgateway →
  (llm-d) → vLLM.
- `systemMessageFrom` a versioned prompt ConfigMap (see below).
- Its own Substrate `WorkerPool` and snapshot prefix.
- `sandbox.network.allowedDomains`: LLM gateway plus MCP Gateway (specialists)
  or A2A gateway (Coordinator). No MCP server is reachable directly.
- Tools reach MCP only through `RemoteMCPServer` objects whose URL is the MCP
  Gateway (`/feature-rag/mcp`, `/drift/mcp`), Streamable HTTP, 30 s timeout. No
  `headersFrom`: no static credential is carried.

Rendered manifests are validated against the served CRD schemas
(`infra/kubernetes/kagent-platform/crd-schemas-kagent-0.10.3.json`). The
schemas are extracted by `scripts/llm_agent/extract_crd_schemas.py` from the
kagent-crds chart pulled by its pinned digest. The check covers unknown
(pruned) fields, required fields, enums and types.

## Typed contracts and fail-closed behaviour

- Every prompt fixes a JSON answer shape with `prompt_revision`, and requires
  reporting unavailable tools by error code. Prompts forbid estimating
  probabilities, feature values or policy text.
- Tool results already carry `status`, `versions` (git SHA, image digest,
  model, feature and corpus revisions) and `citations` (Phase D).
- `fraud_scoring` stays an error until the KServe wire contract is reviewed.
  `knowledge_search` returns `vector_search_unavailable` until a retriever is
  approved. An agent run therefore cannot produce a fabricated score.
- Timeouts: ModelConfig `timeoutSeconds`, RemoteMCPServer `timeout: 30s`, and
  `KAGENT_A2A_CLIENT_TIMEOUT=120s` on the controller.

## Native A2A through agentgateway (rubric row 24)

kagent agents call each other at `A2A_BASE_URL/api/a2a/<namespace>/<agent>`.
`infra/kubernetes/kagent-platform/kagent-values.yaml` sets
`controller.a2aBaseUrl` to `a2a-gateway:8083`. That is an agentgateway route
with the `a2a` policy, in front of the kagent controller's A2A endpoint (Gate
D). The protocol stays native kagent A2A, and no standalone A2A runtime is
added. Instructor acceptance of "agentgateway as A2A Gateway" is still a risk
until a runtime trace shows Coordinator → a2a-gateway → specialist.

## Prompt versioning

- Source of truth: `infra/helm/llm-agent-platform/prompts/<agent>/<version>.md`,
  with sha256 in `prompts/manifest.json`.
- The chart renders a ConfigMap `<agent>-prompt-<version>`. Rendering fails if
  the file and the manifest sha256 differ, so a changed prompt needs a new
  version.
- The SandboxAgent and the ConfigMap carry the version and sha256. Langfuse
  publication is covered under Gate E (`scripts/llm_agent/publish_prompts.py`).

## Identity finding (security blocker)

Substrate 0.0.9 `WorkerPool.spec.template` has only `nodeAffinity`,
`nodeSelector`, `priorityClassName`, `resources` and `tolerations`. There is no
`serviceAccountName`, and unknown fields are pruned. The pilot values
(`substrateWorkerPool.template.serviceAccountName: kagent-pilot-worker`) are
therefore dropped by the API server. Which ServiceAccount worker pods run as is
decided by Substrate and must be read back at runtime. Per-agent WorkerPools
are created anyway, so that per-pool identity becomes possible if Substrate
assigns one. Until a readback proves distinct identities, MCP authentication
stays fail-closed (see `docs/llm_agent/mcp_gateway.md`).

## Runtime (not run)

`docs/llm_agent/runtime_runbook.md` R3 covers: Substrate and the kagent
controller with `kagent-values.yaml`, then `agents.enabled=true`; readback of
WorkerPool, SandboxAgent and worker pod ServiceAccounts; and an A2A
delegation trace.
