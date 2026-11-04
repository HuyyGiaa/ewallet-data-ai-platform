# G3 EDAI-2 rubric evidence, 2026-11-02

Source: original `EDAI K11.xlsx`, worksheet `edai-2 (50%)`, range `A1:E58`.
This matrix applies the strict campaign gate: source, tests, runtime, raw
evidence, required screenshot and reproduction steps must all exist before a
row is `VERIFIED`.

| Row | Points | Requirement | Current evidence | Status | Supported points |
|---:|---:|---|---|---|---:|
| 8 | 2 | Agent Registry deployment and UI | kagent Agent CRDs/UI exist, but Agent Registry is not deployed | BLOCKED | 0 |
| 9 | 2 | RAG pipeline: text → chunks → embeddings → Feast | Source and immutable Redis/ConfigMap readback exist; no successful live Airflow DagRun | PARTIAL | 0 |
| 10 | 2 | RAG governance/DataHub | Source contracts exist; DataHub runtime is absent | BLOCKED | 0 |
| 16 | 1 | FastAPI, Pydantic validation and health checks | MCP FastAPI/Pydantic services are Ready and strict validation is tested; required screenshot remains local-pending | SCREENSHOT_PENDING | 0 |
| 17 | 1 | Async concurrent processing | MCP SDK/ASGI async path and concurrent Agent tool calls passed locked tests and live calls | VERIFIED | 1 |
| 18 | 2 | MCP tools on Kubernetes with Helm, rolling update and fallback | Helm `llm-agent-mcp` rev. 3, two Ready Deployments, immutable digest and rollback target; GKE screenshot not persisted | SCREENSHOT_PENDING | 0 |
| 19 | 2 | Specialist Agent multi-replica and autoscaling | Specialist works with real tools, but one replica and no KEDA runtime | BLOCKED | 0 |
| 20 | 1 | Agent Sandbox | No Substrate/Sandbox runtime is deployed | BLOCKED | 0 |
| 21 | 1 | Publish Specialist to Agent Registry | Agent CRD exists; separate Registry runtime absent | BLOCKED | 0 |
| 22 | 8 | Second Agent with Feast, inference and RAG tools | Feature-RAG has three registered tools; direct Feast/KServe and cited RAG calls passed; Coordinator final response remains partial/unavailable; multi-replica gate absent | PARTIAL | 0 |
| 23 | 2 | MCP Gateway with security/policy | Real authenticated routing, deny tests and traces pass; required persisted screenshot missing | SCREENSHOT_PENDING | 0 |
| 24 | 2 | A2A Gateway with security/policy | Coordinator delegates only through exact Gateway route; direct/unauthorized paths remain denied; persisted screenshot missing | SCREENSHOT_PENDING | 0 |
| 25 | 2 | Coordinator multi-replica and autoscaling | Coordinator/A2A runtime works, but one replica and no KEDA scale evidence | BLOCKED | 0 |
| 26 | 2 | Publish Coordinator to Agent Registry | Agent CRD exists; separate Registry runtime absent | BLOCKED | 0 |
| 27 | 2 | Warm-up/Agent Fleet with Substrate | Substrate was intentionally not installed in this campaign | BLOCKED | 0 |
| 28 | 1 | Unit tests with coverage >90% | 163/163 focused tests pass; `coverage` is absent from the locked environment, so no percentage or screenshot exists | PARTIAL | 0 |
| 29 | 2 | Equivalence partitioning and boundary-value tests | Exact nested/flat/ambiguous/unknown argument partitions and boundary tests pass; terminal screenshot pending | SCREENSHOT_PENDING | 0 |
| 30 | 2 | Mutation score >80% | `mutmut` absent from locked environment; no score | NOT_VERIFIED | 0 |
| 31 | 2 | Property-based idempotency | Deterministic/idempotency tests exist, but `hypothesis` is absent and rubric-specific execution was not produced | NOT_VERIFIED | 0 |
| 32 | 2 | Locust HTML load report | `locust` absent; no bounded load run or HTML report | NOT_VERIFIED | 0 |
| 33 | 2 | RAG pipeline CI/CD | Jenkins source catalog and pipeline contract exist; no live Jenkins build/UI | PARTIAL | 0 |
| 34 | 2 | MCP/Specialist CI/CD | `service.py` correctly selects MCP/Agent/observability/RAG scopes; 7/7 Jenkins contract tests pass; Jenkins runtime absent | PARTIAL | 0 |
| 35 | 2 | Coordinator CI/CD | Source pipeline and gated deploy policy exist; Jenkins runtime absent | PARTIAL | 0 |
| 39 | 2 | Agent test UI | Genuine kagent cards/chat were rendered; image bytes could not be persisted | SCREENSHOT_PENDING | 0 |
| 40 | 2 | Agent Registry UI | No live Agent Registry service | BLOCKED | 0 |
| 41 | 2 | Basic authentication and rate limit for Agent UI | Internal Gateway/MCP authentication works; no approved external UI gateway/auth evidence | NOT_VERIFIED | 0 |
| 45 | 1 | Web API metrics | Tool durations and status events exist; no live Grafana dashboard | PARTIAL | 0 |
| 46 | 1 | CPU/RAM/disk/network visualization | `kubectl top` captured CPU/RAM; no visualization/dashboard | PARTIAL | 0 |
| 47 | 1 | Centralized logs | Kubernetes structured logs captured; no log UI | PARTIAL | 0 |
| 48 | 1 | Distributed traces | Gateway trace IDs exist, but OTLP export fails with zero resolver addresses and Tempo is absent | PARTIAL | 0 |
| 49 | 2 | LLM token, RTT, TTFT and PII-safety metrics | kagent response metadata has token counts and wall time; TTFT/PII dashboard evidence absent | PARTIAL | 0 |
| 50 | 2 | Agent and MCP invocation/failure metrics | Structured per-tool success/failure events exist; no complete dashboard | PARTIAL | 0 |
| 51 | 1 | Agent A/B testing | Not executed; unrelated G1/G2 benchmarking prohibited | NOT_VERIFIED | 0 |
| 52 | 1 | LLM version A/B testing | Not executed; G1/G2 benchmarking prohibited | NOT_VERIFIED | 0 |
| 53 | 1 | Centralized secret management | Immutable namespace-scoped Kubernetes Secrets and rotation contract work; no organization-wide Vault-equivalent proof | PARTIAL | 0 |

Strict evidence-supported score for the rows above: **1/64**. This is not a
product-functionality score. It is deliberately reduced by missing persisted
screenshots, missing shared runtimes, and explicit rubric tools. The remaining
36 EDAI-2 points cover G1/G2, Data/ML Platform, external routing, IaC,
repository design and novel ideas and are outside this G3 campaign; they are
not re-scored here.

## Provenance and primary raw evidence

- Source commit: `642553f40cf395fed06eba52d04f6fa0db3fec94`
- Image digest: `sha256:b80c07c76e4d240328804218c46ff66ecc7e4ba45694072f94774c57548fa058`
- Helm: `llm-agent=12`, `llm-agent-mcp=3`, `kagent=2`
- Direct Feast/KServe result: `runtime/direct-flat-mcp-sdk-result.json`
- Direct cited RAG result: `runtime/direct-rag-knowledge-search.json`
- Coordinator responses: `runtime/coordinator-business-response-adapter*.json`
- Gateway/tool traces: `runtime/*adapter-e2e.log`
- Security negatives: `security/mcp-negative-runtime.json` and
  `security/direct-backend-auth-negative.log`
- Tests: `tests/mcp-envelope-regression-corrected.log` and
  `tests/jenkins-contract.log`
- Reproduction: `docs/llm_agent/g3_runtime_evidence_runbook.md`

No Airflow/DataHub/Jenkins/Grafana/Tempo/Agent Registry success is inferred
from source configuration alone.
