# EDAI-2: ML, LLM and Agent Platform

EDAI-2 extends the data platform into a fraud-intelligence system on GKE:

- a feature store with offline and online stores;
- a Kubeflow training pipeline with MLflow tracking;
- a KServe fraud model behind a FastAPI service;
- a CPU-served LLM behind agentgateway;
- fraud-investigation agents that combine features, model scores and a policy knowledge base through MCP tools;
- Jenkins CI/CD;
- a full observability stack.

The overall architecture, with numbered data and investigation flows, is in the [root README](../../README.md#system-architecture).

## Technical reports

| Topic | Report |
|---|---|
| LLM serving, agentgateway, global ModelConfig, performance experiments | [LLM Inference Platform](llm/LLM_PLATFORM.md) |
| Feast, Kubeflow Pipelines, MLflow, KServe, Fraud API | [Feature Store, Training Pipeline, Registry and Serving](mlops/ML_PIPELINE_SERVING.md) |
| Coordinator and specialist agents, MCP tools, Agent Registry, MCP/A2A gateways, scaling, sandbox pilot | [Agents, MCP Tools, Registry and Gateways](agents/AGENTS_REGISTRY_GATEWAYS.md) |
| RAG ingestion, retrieval and governance | [RAG Knowledge Pipeline](rag/RAG_PIPELINE.md) |
| Test strategy and Jenkins test results | [Testing and Continuous Verification](testing/TESTING.md) |
| Testing rubric rows 28–32 (coverage, partitions, mutation, properties, Locust) | [Row 28](testing/ROW_28_UNIT_TEST_COVERAGE.md), [Row 29](testing/ROW_29_EQUIVALENCE_BOUNDARY.md), [Row 30](testing/ROW_30_MUTATION_TESTING.md), [Row 31](testing/ROW_31_PROPERTY_BASED_IDEMPOTENCY.md), [Row 32](testing/ROW_32_LOCUST_GKE.md) |
| KEDA autoscaling of the Feature and Fraud APIs (G5-B) | [Serving Autoscaling with KEDA](mlops/SERVING_AUTOSCALING_KEDA.md) |
| Jenkins pipelines and continuous delivery | [CI/CD with Jenkins](cicd/CICD.md) |
| Metrics, logs, traces, LLM and agent telemetry | [Observability](observability/OBSERVABILITY.md) |
| Terraform, access control, network security, secrets | [Platform Infrastructure and Security](platform/GATEWAY_IAC_SECURITY_AB.md) |
| Novel ideas | [Grounded Agent Intake and Exact Agent Telemetry](novel_ideas/NOVEL_IDEAS.md) |

**Detailed experiment records:**

- [vLLM serving-parameter benchmark](llm/vllm_cpu_o1_benchmark.md);
- [llm-d routing benchmark](llm/llmd_g2_routing_benchmark.md).

## Evidence

Screenshots used by the reports are in [`docs/evidence/edai2/screenshots/`](../evidence/edai2/screenshots). There are two submission-facing kinds:

- **Application screenshots.** kagent, Agent Registry, Grafana, Tempo, Loki, Kubeflow, MLflow and Jenkins; their provenance is in `PROVENANCE.json`.
- **Native read-only captures of the running cluster.** For example, the kagent agents page and a Grafana query.

The former `runtime/01–08` programmatic renders are retained only as
historical artifacts and are no longer embedded. Submission-facing reports
link their original `runtime/raw/*.txt` transcripts and mark literal
screenshot gaps.

Operating instructions for reaching the UIs are in the [UI access runbook](../runbooks/edai2/UI_REPLAY.md).

## Internal tracking

These files are kept for project tracking and are not part of the technical narrative:

- [Rubric evidence index](RUBRIC_EVIDENCE_INDEX.md)
- [Gap ledger](GAP_LEDGER.md)
- [Screenshot audit](SCREENSHOT_AUDIT.md)
- [Historical documents](HISTORICAL_DOCUMENTS.md)
