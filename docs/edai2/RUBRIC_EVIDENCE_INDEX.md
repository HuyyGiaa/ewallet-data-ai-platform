# EDAI-2 rubric evidence index (rows 3–57, 100 points)

**Authority:** the original workbook `EDAI K11.xlsx` (sha256 `1b0f8801…`),
sheet `edai-2 (50%)`. The row table below uses short reviewer-facing labels;
the exact requirement, Deliverables wording and raw points are preserved by
sheet and row in the
[93-row workbook extract](../rubric/edai_k11_workbook_extract.json).

**Status vocabulary.** Statuses are literal-compliance verdicts:

| Status | Meaning |
|---|---|
| **COMPLETE** | Every column-D deliverable is present, with traceable runtime evidence and a document |
| **COMPLETE (historical)** | Same, but the runtime evidence predates N13 and was not re-run, by directive |
| **PARTIAL** | Some deliverables are missing; the gap class is given |
| **NOT IMPLEMENTED** | No implementation exists for the requirement |

**Gap classes:**

| Class | Meaning |
|---|---|
| `DOCUMENTATION_MISSING` | The documentation is missing |
| `SCREENSHOT_MISSING` | Runtime is proven but no image was retained |
| `RUNTIME_EVIDENCE_MISSING` | Source exists but was not executed or observed |
| `IMPLEMENTATION_MISSING` | The implementation does not exist |

The details are in [GAP_LEDGER.md](GAP_LEDGER.md). Screenshot classification (NATIVE_UI / HISTORICAL_UI / REAL_TERMINAL / DERIVED_RENDER) and per-row screenshot coverage are in [SCREENSHOT_AUDIT.md](SCREENSHOT_AUDIT.md).

**Provenance.** Screenshots are curated copies with SHA-256 verified against the originals ([`PROVENANCE.json`](../evidence/edai2/screenshots/PROVENANCE.json)). The `runtime/` renders are N13 read-only captures from Helm r41 (2026-11-04T09:32Z).

**Sources of the verdicts:**
- the N10-E2 93-row matrix (2026-11-04);
- the N11 and N12 evidence packages;
- the repository matrix @`084c8de1`.

**No row was promoted without new evidence.** The external N13 inventory was located later and is reconciled at the end of this file.

| Row | Pts | Requirement | Deliverable (D) | Status | Document | Key evidence | Gap |
|---:|---:|---|---|---|---|---|---|
| 3 | 2 | Deploy LLM inference platform | Setup + benchmark + results | COMPLETE | [LLM_PLATFORM](llm/LLM_PLATFORM.md) | vllm-primary live at r41 (fallback scaled to 0; failover proven in G1 run 6); llm/01 native UI | — |
| 4 | 2 | Custom LLM model | same | COMPLETE | [LLM_PLATFORM](llm/LLM_PLATFORM.md) | Qwen3-0.6B, served name `ewallet-fraud-llm`; llm/01 | — |
| 5 | 1 | agentgateway security & policy | same | PARTIAL | [LLM_PLATFORM §3](llm/LLM_PLATFORM.md#32-agentgateway) | apiKey strict 401 matrix, rate limits, timeouts, failover (G1 raw) | SCREENSHOT_MISSING |
| 6 | 2 | Benchmark & optimize | same | COMPLETE (historical) | [LLM_PLATFORM §6](llm/LLM_PLATFORM.md#5-performance-and-optimisation), [O1](llm/vllm_cpu_o1_benchmark.md), [G2](llm/llmd_g2_routing_benchmark.md) | C1 +51.4 % tok/s, TTFT −37.4 % | — |
| 7 | 2 | One global ModelConfig | Link to config + explanation | COMPLETE | [LLM_PLATFORM §5](llm/LLM_PLATFORM.md#33-global-modelconfig) | `ewallet-global-llm`; llm/01 | — |
| 8 | 2 | Agent registry deployed | Registry UI with agents + code link | COMPLETE | [AGENTS §3](agents/AGENTS_REGISTRY_GATEWAYS.md#5-agent-registry) | agents/01–05 (agentregistry v0.4.0) | Metadata wording (note) |
| 9 | 2 | RAG pipeline text → chunk → embed → Feast | Airflow success image + DataHub lineage + code | PARTIAL | [RAG_PIPELINE](rag/RAG_PIPELINE.md) | DagRun `manual__2026-11-02T04:47:47` 6/6 success; publication `b85ae796…`; cicd/09; agents/08 | SCREENSHOT_MISSING |
| 10 | 2 | RAG governance | same | PARTIAL | [RAG_PIPELINE §4](rag/RAG_PIPELINE.md#5-governance) | DataHub GMS 25 records read back, 5 assertion runs | SCREENSHOT_MISSING |
| 11 | 2 | Incremental Offline → Online materialization pipeline | Airflow success image + code | PARTIAL | [ML_PIPELINE_SERVING §3](mlops/ML_PIPELINE_SERVING.md#32-online-store-and-materialization), [MLOPS](mlops/MLOPS_FEATURE_STORE.md) | K8s operator jobs, parity 155/155 | IMPLEMENTATION_MISSING (no Airflow DAG) |
| 12 | 2 | Streaming push jobs to Offline + Online | Jobs success + data + code | PARTIAL | [MLOPS row 12](mlops/MLOPS_FEATURE_STORE.md#row-12--flink-to-offline-and-online-stores) | source only | RUNTIME_EVIDENCE_MISSING |
| 13 | 2 | KFP training pipeline ≥ 4 steps | KFP success image + code | COMPLETE | [ML_PIPELINE_SERVING §4](mlops/ML_PIPELINE_SERVING.md#4-training-pipeline-kubeflow-pipelines) | run `dc29f5cc…`; mlops/01–03 | — |
| 14 | 2 | MLflow versioning + automatic `latest`/`production` tag | Model + data versioned images | PARTIAL | [ML_PIPELINE_SERVING §5](mlops/ML_PIPELINE_SERVING.md#5-experiment-tracking-and-model-registry-mlflow) | mlops/04–08; alias `candidate` | IMPLEMENTATION_MISSING (alias policy) |
| 15 | 2 | KServe, model-inside-image | Pod + YAML + successful call | COMPLETE | [ML_PIPELINE_SERVING §6](mlops/ML_PIPELINE_SERVING.md#6-model-serving-kserve-model-inside-image) | runtime/05–07 (N13 r41); KServe log request ID match | — |
| 16 | 1 | FastAPI + Pydantic + health checks | Registry + multi-replica + Sandbox + UI chat + YAML | PARTIAL | [AGENTS §4–6](agents/AGENTS_REGISTRY_GATEWAYS.md#33-mcp-tool-servers-web-api) | runtime/08 (422, health 200); runtime/01, 09; agents/07–12; multi-replica native: runtime/10 | IMPLEMENTATION_MISSING (business-agent Sandbox) |
| 17 | 1 | async concurrent processing | same | COMPLETE | [AGENTS §4](agents/AGENTS_REGISTRY_GATEWAYS.md#33-mcp-tool-servers-web-api) | async handlers; concurrent MCP calls live (N11) | — |
| 18 | 2 | MCP tool on K8s, Helm, RollingUpdate, auto fallback | same | PARTIAL | [AGENTS §8](agents/AGENTS_REGISTRY_GATEWAYS.md#4-deployment-and-configuration) | runtime/04 (RollingUpdate 0/1, Helm r6); agents/03 | IMPLEMENTATION_MISSING (fallback) |
| 19 | 2 | Agent using MCP, multi-replica, KEDA auto-scale | same | PARTIAL | [AGENTS §5](agents/AGENTS_REGISTRY_GATEWAYS.md#4-deployment-and-configuration) | runtime/01 (2/2, HPA 2–3); runtime/10 native | IMPLEMENTATION_MISSING (KEDA) |
| 20 | 1 | Agent in Sandbox | same | PARTIAL | [AGENTS §9](agents/AGENTS_REGISTRY_GATEWAYS.md#8-sandbox-and-warm-worker-pool-pilot) | runtime/03 (pilot only, gVisor) | IMPLEMENTATION_MISSING |
| 21 | 1 | Publish agent to registry | same | COMPLETE | [AGENTS §3](agents/AGENTS_REGISTRY_GATEWAYS.md#5-agent-registry) | agents/01 | Metadata wording (note) |
| 22 | 8 | Second agent: Online Feature Store data + 2 tools (KServe inference, RAG), same deliverables as 16–21 | same as 16–21 | PARTIAL | [AGENTS §10](#appendix-row-22-reconciliation-8-points) | **Business workflow VERIFIED** (Feast → specialist → KServe + RAG, trace `f341b410…`, agents/08–09); registry, multi-replica (runtime/10 native), UI chat, YAML proven | IMPLEMENTATION_MISSING (Sandbox, KEDA, MCP fallback) |
| 23 | 2 | MCP Gateway (agentgateway) + security/policy | Setup + policies + why + YAML | COMPLETE | [AGENTS §7](agents/AGENTS_REGISTRY_GATEWAYS.md#7-mcp-gateway-and-a2a-gateway-security) | per-tool `mcpAuthorization`; 401 negatives; agents/13 | — |
| 24 | 2 | A2A Gateway (agentgateway) + security/policy | same | COMPLETE | [AGENTS §7](agents/AGENTS_REGISTRY_GATEWAYS.md#7-mcp-gateway-and-a2a-gateway-security) | a2a-gateway spans in trace `f341b410…`; agents/13–14 | — |
| 25 | 2 | Coordinator multi-replica + auto-scale | Registry + replicas + scale test image + kagent chat | PARTIAL | [AGENTS §5–6](agents/AGENTS_REGISTRY_GATEWAYS.md#4-deployment-and-configuration) | runtime/01, 09; agents/07–08; runtime/10 native | IMPLEMENTATION_MISSING (KEDA) + RUNTIME_EVIDENCE_MISSING (scale test) |
| 26 | 2 | Publish Coordinator to registry | same | COMPLETE | [AGENTS §3](agents/AGENTS_REGISTRY_GATEWAYS.md#5-agent-registry) | agents/02 | Metadata wording (note) |
| 27 | 2 | Warm pool / Substrate | Substrate code + image of many agents on few workers | PARTIAL | [AGENTS §9](agents/AGENTS_REGISTRY_GATEWAYS.md#8-sandbox-and-warm-worker-pool-pilot) | runtime/03 | RUNTIME_EVIDENCE_MISSING |
| 28 | 1 | Coverage > 90 % with Web API, fixture, mock | Test results + link | PARTIAL | [TESTING](testing/TESTING.md) | Jenkins JUnit (N9); no coverage % | RUNTIME_EVIDENCE_MISSING |
| 29 | 2 | Equivalence partitioning / boundary values | same | PARTIAL | [TESTING §2](testing/TESTING.md#3-test-design) | partition tests run in N9 CI | SCREENSHOT_MISSING |
| 30 | 2 | Mutation testing (mutmut) | same | NOT IMPLEMENTED | [TESTING](testing/TESTING.md) | targeted smoke only (not mutmut) | IMPLEMENTATION_MISSING |
| 31 | 2 | Property-based idempotency | same | PARTIAL | [TESTING](testing/TESTING.md) | deterministic idempotency tests only | IMPLEMENTATION_MISSING |
| 32 | 2 | Locust load test HTML | same | NOT IMPLEMENTED | [TESTING](testing/TESTING.md) | — | IMPLEMENTATION_MISSING |
| 33 | 2 | CI/CD for RAG pipeline | Pipelines ran + steps + Jenkinsfile | COMPLETE | [CICD §3](cicd/CICD.md#4-continuous-delivery-of-the-rag-ingestion-pipeline) | CD #8 7 stages SUCCESS; cicd/07–09; rollback verified | — |
| 34 | 2 | CI/CD MCP servers + data Agent | same | PARTIAL | [CICD](cicd/CICD.md) | CI N9 agent-ci #2, negative #2 | RUNTIME_EVIDENCE_MISSING (CD not executed) |
| 35 | 2 | CI/CD Coordinator Agent | same | PARTIAL | [CICD](cicd/CICD.md) | CI N9 | RUNTIME_EVIDENCE_MISSING (CD not executed) |
| 36 | 2 | CI/CD stream feature jobs | same | PARTIAL | [CICD §4](cicd/CICD.md#6-limitations) | CI catalog only | RUNTIME_EVIDENCE_MISSING |
| 37 | 2 | Grafana behind NGINX | NGINX settings | NOT IMPLEMENTED | [PLATFORM §1](platform/GATEWAY_IAC_SECURITY_AB.md#5-limitations-and-future-work) | — | IMPLEMENTATION_MISSING |
| 38 | 2 | Log UI behind NGINX | same | NOT IMPLEMENTED | same | — | IMPLEMENTATION_MISSING |
| 39 | 2 | Trace UI behind NGINX | same | NOT IMPLEMENTED | same | — | IMPLEMENTATION_MISSING |
| 40 | 2 | Agent test UI behind NGINX | same | NOT IMPLEMENTED | same | — | IMPLEMENTATION_MISSING |
| 41 | 2 | Registry UI behind NGINX | same | NOT IMPLEMENTED | same | — | IMPLEMENTATION_MISSING |
| 42 | 2 | Basic auth + rate limit (UI) | same | NOT IMPLEMENTED | same | — | IMPLEMENTATION_MISSING |
| 43 | 1 | Domain + HTTPS for Web API | same | NOT IMPLEMENTED | same | — | IMPLEMENTATION_MISSING |
| 44 | 1 | Terraform | Terraform/Ansible ran (images) | PARTIAL | [PLATFORM §2](platform/GATEWAY_IAC_SECURITY_AB.md#1-infrastructure-as-code-terraform) | `r2_terraform_plan.json`: 43 resources applied | SCREENSHOT_MISSING |
| 45 | 1 | Ansible roles | same | NOT IMPLEMENTED | same | — | IMPLEMENTATION_MISSING |
| 46 | 1 | Web API metrics | Telemetry shown | COMPLETE | [OBSERVABILITY §2](observability/OBSERVABILITY.md#2-web-api-metrics-fraud-api) | observability/46_* | — |
| 47 | 1 | Computing telemetry | same | COMPLETE | [OBSERVABILITY §3](observability/OBSERVABILITY.md#3-compute-telemetry-cpu-memory-network-disk) | observability/47_* | — |
| 48 | 1 | Logs | same | COMPLETE | [OBSERVABILITY §4](observability/OBSERVABILITY.md#4-logs) | observability/48_* (8 services) | — |
| 49 | 1 | Traces | same | COMPLETE | [OBSERVABILITY §5](observability/OBSERVABILITY.md#5-distributed-traces) | trace `f341b410…`, 251 spans | — |
| 50 | 2 | LLM telemetry (tokens, RTT, TTFT, PII) | same | COMPLETE | [OBSERVABILITY §6](observability/OBSERVABILITY.md#6-llm-telemetry) | 707/264/971; TTFT 5/38 s; PII 1 caught | — |
| 51 | 2 | Agent telemetry (calls per agent, per tool, failures) | same | COMPLETE | [OBSERVABILITY §7](observability/OBSERVABILITY.md#7-agent-and-mcp-telemetry) | 6/6, 9/9; MCP 17/16/19; failures feature_lookup = 1 | — |
| 52 | 1 | A/B agents with different model config | A/B implementation + dashboard | NOT IMPLEMENTED | [PLATFORM §3](platform/GATEWAY_IAC_SECURITY_AB.md#5-limitations-and-future-work) | — | IMPLEMENTATION_MISSING |
| 53 | 1 | A/B different LLM versions | same | NOT IMPLEMENTED | same | G2 is routing A/B of the same model (does not satisfy) | IMPLEMENTATION_MISSING |
| 54 | 1 | Vault (centralized secrets) | Prove Vault is used | NOT IMPLEMENTED | [PLATFORM §4](platform/GATEWAY_IAC_SECURITY_AB.md#4-secret-management) | K8s Secrets + Workload Identity only | IMPLEMENTATION_MISSING |
| 55 | 2 | README and docs follow the A2 rules | (D empty: the A2 rules) | PARTIAL | [README](../../README.md), this index | business domain, diagram, TOC, repo structure, docs/ per topic | DOCUMENTATION_MISSING (arrow numbering, function docstrings) |
| 56 | 2 | Novel idea 1 | Idea + proof | COMPLETE, pending owner selection | [NOVEL_IDEAS §1](novel_ideas/NOVEL_IDEAS.md#idea-1-grounded-natural-language-intake-the-llm-selects-a-tool-it-never-produces-a-fraud-score) | agents/08, 10–12 | Owner selection / assessor acceptance |
| 57 | 2 | Novel idea 2 | Idea + proof | COMPLETE, pending owner selection | [NOVEL_IDEAS §2](novel_ideas/NOVEL_IDEAS.md#idea-2-exact-agent-call-counts-across-replicas-and-restarts) | observability/51 (9/9) | Owner selection / assessor acceptance |

## Summary

| Bucket | Rows | Points |
|---|---|---:|
| Complete deliverables (including historical row 6) | 3, 4, 6, 7, 8, 13, 15, 17, 21, 23, 24, 26, 33, 46–51 (19 rows) | 32 |
| Complete, pending owner selection (novel ideas) | 56, 57 | 4 |
| Missing only a screenshot or documentation | 5, 9, 10, 29, 44, 55 (6 rows) | 10 |
| Missing runtime evidence | 12, 27, 28, 34, 35, 36 (6 rows) | 11 |
| Missing implementation (including partial rows whose remaining gap is implementation) | 11, 14, 16, 18, 19, 20, 22, 25, 30, 31, 32, 37–43, 45, 52, 53, 54 (22 rows) | 43 |
| **Total** | 55 rows | **100** |

These buckets are evidence-coverage buckets, not a predicted grade. Row 25 has both an implementation gap (KEDA) and a runtime gap (scale test); it is counted under implementation.

## Reconciliation with the external N13 inventory (quality gate, 2026-11-04)

**Inventory:** `FINAL_EVIDENCE_INVENTORY.md` / `FINAL_RUBRIC_STATUS.md` (external, 2026-11-04). It reports EDAI-2 PASS for rows 3, 4, 7, 8, 13, 15, 17, 21, 23, 24, 26, 33 and 46–51 (30 points). This index agrees with all of those rows and differs only where the evidence differs:

| Row | N13 inventory | This index | Decision and evidence |
|---:|---|---|---|
| 6 | PARTIAL ("reference existing benchmark report") | COMPLETE (historical) | The gap was a missing reference. [LLM_PLATFORM §6](llm/LLM_PLATFORM.md#5-performance-and-optimisation) now documents the matched O1 benchmark (4 configurations, 144 requests, C1 +51.4 % tok/s) and G2. The runtime is historical (2026-11-01) and was not re-run |
| 22 | PARTIAL: "UI chat/streaming failure prevents literal two-tool specialist proof" | PARTIAL, for different reasons | The inventory's reason is **superseded** by N11: streaming was fixed (r33), and agents/08–09 show UI chats with real Feast/KServe/RAG tool calls. What remains open is the deployment controls inherited from rows 18–20 (Sandbox, KEDA, MCP fallback). See [AGENTS §10](#appendix-row-22-reconciliation-8-points) |
| 16, 19, 25 | PARTIAL | PARTIAL | Agreed. Multi-replica now also has a native UI image (runtime/10, Grafana Explore, r41); Sandbox, KEDA and the scale test remain open |
| 56, 57 | PARTIAL (candidates: registry publication, 41-input contract) | Documented with runtime proof, pending owner selection | Different candidate ideas ([NOVEL_IDEAS](novel_ideas/NOVEL_IDEAS.md)). Not counted as complete until the owner selects them |
| 3 | PASS | COMPLETE, wording corrected | `vllm-fallback` is scaled to 0 at r41. The documentation no longer claims a live fallback |

All other rows have the same classification in both.

## Appendix: row 22 reconciliation (8 points)

**Workbook (literal):**

> A22: "Làm tương tự, thêm 1 Agent nhận dữ liệu từ Online Feature Store, và có 2 tools: + gọi tới ML Inference Service để make prediction + RAG để trả lời câu hỏi của user dựa trên các dữ liệu đã được ingest thông qua RAG Data Pipeline".
>
> D22: "Document tương tự bên trên".

"Tương tự" ("similarly") refers back to rows 16–21. D22 therefore inherits the D16 deliverable: registry, multi-replica, Sandbox, UI chat and a YAML link. The row also inherits the deployment pattern of rows 17–21.

The agent for this row is **`feature-rag-specialist`**.

#### A. Functional business workflow: Online Feature Store → Agent → ML inference + RAG. VERIFIED

| Sub-requirement | Evidence | Status |
|---|---|---|
| Agent receives data from the Online Feature Store | MCP `feature_lookup` reads Feast online (Redis). The UI answer shows real feature values (agents/08, agents/09). Grafana MCP calls per tool: `feature_lookup` 17 (observability/51) | **Proven** (N11, N12) |
| Tool 1: call the ML Inference Service | MCP `fraud_scoring` → KServe `fraud-detector` V2. The probability 0.0639895560144887 equals the value recorded on 2026-10-31. `fraud_scoring` 16 calls | **Proven** |
| Tool 2: RAG over data ingested by the RAG pipeline | MCP `knowledge_search` returns 3 citations from corpus `fraud-policy-v1-20261030` (publication `b85ae796…`). `knowledge_search` 19 calls | **Proven** |
| One business trace across all hops | Tempo trace `f341b410568fb649ee0f95bba50cd27d`: 251 spans, coordinator → a2a-gateway → specialist → mcp-gateway → feature-rag-mcp → Feast / KServe / RAG | **Proven** (N12 Packet B) |
| Fails closed | An unknown customer causes a `feature_lookup` failure and `specialist_not_completed`, with no fabricated score (observability/51 failures). Missing or unsupported inputs are rejected (agents/10–11) | **Proven** |

The business workflow is **implemented and runtime-verified**. It must not be described as unimplemented.

#### B. Deployment and documentation requirements inherited from rows 16–21

| Inherited sub-requirement | Evidence | Status |
|---|---|---|
| Published on the Agent Registry | agents/01 (agentregistry v0.4.0, v1.0.0) | **Proven**. Note: the registry text says "SandboxAgent" while the runtime is BYO |
| Multi-replica | runtime/01 (TERMINAL RENDER: 2/2); **runtime/10 (native Grafana Explore, r41)** shows pods `feature-rag-specialist-855c67457d-c66mn` and `…-dvttw` | **Proven** |
| Auto-scaling using the rubric-linked tool (KEDA) | Only HPA is deployed (CPU 60 %, min 2 / max 3). No KEDA CRD; no observed scale event | **Missing**: IMPLEMENTATION_MISSING (KEDA) + RUNTIME_EVIDENCE_MISSING (scale test) |
| Permissions limited through a Sandbox | Only `kagent-pilot/pilot-sandbox-agent` (mock model) runs under gVisor. The specialist is an ordinary Deployment | **Missing**: IMPLEMENTATION_MISSING |
| UI chat with the agent | agents/09: direct kagent UI chat with the specialist (request `nl-d61817187a5d735cea1ba3737dce93cb`) | **Proven** (N11) |
| Link to the YAML that deploys the agent | [`templates/agents.yaml`](../../infra/helm/llm-agent-platform/templates/agents.yaml), [`mcp-services.yaml`](../../infra/helm/llm-agent-platform/templates/mcp-services.yaml) | **Proven** |
| FastAPI + Pydantic + health, async (rows 16–17 pattern) | runtime/08; async MCP handlers | **Proven** |
| MCP on K8s with Helm + RollingUpdate (row 18 pattern) | runtime/04 | **Proven** |
| Automatic fallback (row 18 pattern) | Not deployed | **Missing** |

#### Verdict

**PARTIAL.** The core of the row (the agent and its two tools working end to end on real Feast, KServe and RAG data) is verified. The open items are deployment controls inherited from rows 18–20: KEDA autoscaling with a scale test, Sandbox isolation, and MCP fallback.

**Disagreement with the earlier N13 inventory (2026-11-04, `FINAL_RUBRIC_STATUS.md`).** That inventory stated that a "UI chat/streaming failure prevents literal two-tool specialist proof". This is **superseded**:
- N11 fixed A2A streaming for the BYO agents (Helm r33, verdict `KAGENT_UI_STREAMING PASS`);
- agents/08–09 show successful UI chats with real tool calls.

The failing image the inventory cited (`E2-kagent/c4-rag-response.png`, still "Thinking…") predates that fix and is not used here.
