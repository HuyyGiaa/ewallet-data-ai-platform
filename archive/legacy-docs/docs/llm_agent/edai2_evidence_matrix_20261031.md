# EDAI-2 evidence matrix: LLM + Agent campaign, 2026-10-31

Rubric: `EDAI K11.xlsx`, sheet `edai-2 (50%)`, sha256 `1b0f8801…6081`. The
`EDAI K11(7).xlsx` file named in the brief is not on this machine. Hyperlinks
were read from the workbook: A3→llm-d quickstart, C5/B23/B24→agentgateway,
A7→kagent BYO-OpenAI, A8→aregistry.ai, C19/C25→KEDA, C20→kagent agent
sandbox, A27→Agent Substrate, C30→mutmut, C31→Hypothesis.

States are independent:

| Code | Meaning |
|---|---|
| S | SOURCE_READY |
| T | TEST_PASS |
| L | LOCAL_RUNTIME_VERIFIED (this machine: Docker or kind) |
| C | CLOUD_RUNTIME_VERIFIED |
| E | EVIDENCE_COMPLETE (proof as the rubric asks: doc, screenshot, …) |
| R | RUBRIC_VERIFIED |

No row is E or R. No points are awarded here.

| Row | Pts | Requirement (short) | S | T | L | C | Evidence / what is missing |
|---|---|---|---|---|---|---|---|
| 3 | 2 | Deploy LLM inference platform (llm-d guide) | ✔ | ✔ | partial | – | vLLM CPU direct Docker: 4× HTTP 200 (earlier). llm-d topology `docs/llm_agent/llm_d_inference_routing.md`. **Missing:** in-cluster vLLM → agentgateway 200 (R1) and an llm-d pool route (R2) |
| 4 | 2 | Custom LLM model | ✔ | ✔ | partial | – | Qwen3-0.6B pinned revision, staged and checksummed. **Missing:** served through the gateway |
| 5 | 1 | agentgateway + security/policy | ✔ | ✔ | config | – | All 6 rendered configs pass the pinned binary. **Missing:** successful LLM request (past 504; timeout cause fixed in source) |
| 6 | 2 | Benchmark and optimize | ✔ | – | – | – | `run_llm_benchmark.py` exists; ad-hoc latency only. **Missing:** benchmark run, llm-d A/B |
| 7 | 2 | Global ModelConfig via gateway | ✔ | ✔ | – | – | Timeout now a value. **Missing:** kagent readback with an agent using it |
| 8 | 2 | Agent registry (aregistry) | ✔ | ✔ | ✔ | – | Local registry v0.4.0: 8 entries created and read back (`registry_local_smoke.json`). **Missing:** UI screenshot, in-cluster install |
| 9 | 2 | RAG pipeline text→chunk→embedding→feature store | ✔ | ✔ | – | – | DAG + publication + exact retriever over Feast Online. **Missing:** DAG run, materialize, real embeddings (DP2 active) |
| 10 | 2 | RAG governance | ✔ | ✔ | – | – | Versioned corpus, checksums, DataHub lineage plan. **Missing:** DataHub readback |
| 16 | 1 | FastAPI + Pydantic + k8s healthcheck | ✔ | ✔ | stale | – | Transport readiness. The kind smoke predates hardening. **Missing:** image build + readback |
| 17 | 1 | async | ✔ | ✔ | – | – | — |
| 18 | 2 | MCP on Helm + rolling update + fallback | ✔ | ✔ | – | – | **Missing:** rollout and rollback on a cluster |
| 19 | 2 | Agent with MCP, multi-replica + KEDA | ✔ | ✔ | – | – | drift-specialist + ScaledObject on its WorkerPool. **Missing:** R3/R5 |
| 20 | 1 | Agent in sandbox | ✔ | ✔ | – | – | SandboxAgent (gVisor) validated against the CRD schema |
| 21 | 1 | Publish agent to registry | ✔ | ✔ | ✔ | – | Local registry readback. **Missing:** screenshot |
| 22 | 8 | Agent: online features + ML inference + RAG | ✔ | ✔ | – | – | feature-rag-specialist. **BLOCKED:** Feast online data, KServe contract (`139ba7e` not in branch), RAG materialization, MCP identity |
| 23 | 2 | agentgateway as MCP Gateway + security | ✔ | ✔ | policy smoke | – | Deny path proven locally; a credential leak was found and fixed. **DEPLOY_NO_GO:** identity |
| 24 | 2 | agentgateway as A2A Gateway | ✔ | ✔ | config | – | a2a-gateway in front of native kagent A2A (`A2A_BASE_URL`). Instructor-acceptance risk |
| 25 | 2 | Coordinator multi-replica + KEDA | ✔ | ✔ | – | – | fraud-coordinator, ScaledObject |
| 26 | 2 | Publish Coordinator to registry | ✔ | ✔ | ✔ | – | Local readback |
| 27 | 2 | Warm-up / Agent Fleet (Substrate) | ✔ | ✔ | – | – | Per-agent WorkerPool, warm floor 1 + cron window. **Missing:** warm/cold measurement |
| 28 | 1 | Coverage > 90% + Web API tests | ✔ | partial | – | – | Coverage not re-measured this campaign. **Missing:** coverage report and screenshot |
| 29 | 2 | EP/BVA parametrized tests | ✔ | ✔ | – | – | Partial evidence in the Phase D tests |
| 30 | 2 | mutmut | – | – | – | – | Targeted mutation smoke 40/40 (not mutmut). **Missing:** a mutmut run |
| 31 | 2 | Property-based (Hypothesis) | – | – | – | – | **Missing** |
| 32 | 2 | Locust load test (HTML) | – | – | – | – | **Missing** |
| 33 | 2 | CI/CD RAG pipeline | partial | ✔ | – | – | Tests and validation in Jenkins source. **Missing:** DAG deploy job, Jenkins run |
| 34 | 2 | CI/CD MCP + Agents, prompts via a tool | ✔ | ✔ | partial | – | Scripts + agentregistry Prompts (locally verified). **Missing:** Jenkins run. Langfuse-vs-agentregistry acceptance risk |
| 35 | 2 | CI/CD Coordinator | ✔ | ✔ | – | – | Same pipeline |
| 37–39 | 6 | Grafana / logs / traces UIs behind gateway | partial | ✔ | – | – | Collector + dashboard. **Missing:** UIs and routes. NGINX→Istio equivalence risk |
| 40 | 2 | Agent test UI | partial | – | – | – | kagent UI enabled in values; route missing |
| 41 | 2 | Registry UI | partial | – | ✔ (API) | – | UI served locally; screenshot missing |
| 42 | 2 | Basic auth + rate limit on UI | – | – | – | – | DECISION_REQUIRED |
| 43 | 1 | Domain + HTTPS for data API | partial | – | – | – | TLS listener source; certificate and DNS missing |
| 46–49 | 4 | API/compute/log/trace telemetry | ✔ | ✔ | partial | – | Metric names read from the binary; collector validated; redaction smoke PASS |
| 50 | 2 | LLM token / latency metrics | ✔ | partial | – | – | Token/TTFT names UNVERIFIED until R1 |
| 51 | 2 | Agent / tool call metrics | ✔ | ✔ | partial | – | MCP tool calls verified; per-agent via spanmetrics UNVERIFIED |
| 52–53 | 2 | A/B agents / LLM versions | – | – | – | – | **Missing:** primary/fallback + llm-d is a base, but no A/B |
| 54 | 1 | Vault | – | – | – | – | **Missing** |
| 55 | 2 | README / docs | partial | – | – | – | Docs per gate; repository README not updated |
| 56–57 | 4 | Novel ideas | partial | – | – | – | Candidates: llm-d inference-aware routing, agentregistry prompt governance, fail-closed MCP identity design |

## Runtime blockers by dependency

- **DP2 active** (`gcloud dataproc batches submit` running all day):
  - no vLLM/llm-d, kagent, KEDA or registry install on kind;
  - no RAG DAG run.
- **Identity:** Substrate 0.0.9 cannot pin the worker ServiceAccount, and
  `source.identity` under Ambient is unproven. As a result the MCP servers
  stay on DenyAll and E2E row 22 cannot pass.
- **kind:** the ztunnel release is `failed` (host inotify, which needs an
  owner-approved sysctl).
- **Business backends:**
  - Feast online store is `null` in the repo configs;
  - the KServe wire contract is unreviewed (`139ba7e` outside this branch);
  - no embedding endpoint;
  - no drift baselines.
