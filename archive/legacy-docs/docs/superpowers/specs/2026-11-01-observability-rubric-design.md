# Observability Rubric Implementation Design
## Intent

Implement the smallest reproducible observability solution that covers EDAI-2
rows 46-51 and the matching EDAI-1 observability requirements without changing
application ownership boundaries or claiming unexecuted GKE evidence.

The solution must let a reviewer trace real telemetry contracts from existing
E-Wallet services through OpenTelemetry into one metrics backend, one log
backend and one trace backend, then inspect the results in Grafana.

## Source of truth

- The original workbook `EDAI K11(20261101-022539).xlsx`, especially EDAI-2
  rows 46-51 and the corresponding EDAI-1 rows 46-51.
- The user's Observability Implementation brief dated 2026-11-01.
- Repository architecture and Living Decision snapshots. Direct Google Drive
  access is unavailable in this session, so the repository snapshots are the
  explicit fallback.

## Goals

- API request count, rate, active requests, failures and latency distributions.
- Kubernetes CPU, memory, disk, network, pod health, restarts, OOMKilled and
  requests/limits from standard kubelet/cAdvisor/kube-state metrics.
- JSON logs with trace/span correlation and fail-closed sensitive-field
  redaction.
- W3C trace context and spans for existing gateway, inference and MCP paths.
- Real vLLM/agentgateway metrics only, with unverified or missing telemetry
  displayed as missing rather than zero.
- Agent and MCP call, duration and semantic-failure contracts for existing
  components.
- Three provisioned dashboards: Platform Overview, LLM Runtime, Agent & MCP.
- Bounded resource profiles, validation tests, local evidence and a fail-closed
  rubric mapping.

## Non-goals

- No GKE deployment, cloud resources, workload restart, scaling or load test.
- No fake metrics, screenshots or runtime evidence.
- No CI/CD, ML, Feast, KServe, RAG, G1/O1 benchmark or agent business-logic
  changes.
- No Langfuse, PushGateway, Elasticsearch, VictoriaLogs or duplicate backends.
- No public metrics endpoint.

## Architecture

Existing services emit OTLP and native Prometheus metrics. A single
OpenTelemetry Collector applies memory limiting, sensitive-data deletion,
value redaction and batching. Prometheus stores application and Kubernetes
metrics. Loki stores bounded JSON logs. Tempo stores traces. Grafana provisions
all three data sources and the three dashboards. Every component is optional
and disabled in the default and GKE values; local/source validation explicitly
enables it.

The collector remains the aggregation boundary. Prometheus scrapes the
collector, agentgateway, vLLM, kubelet/cAdvisor and kube-state-metrics. The
collector exports logs to Loki and traces to Tempo only when configured.

## Privacy and cardinality

Authorization, cookies, API keys, credentials, prompt/completion content,
tool arguments and direct identifiers are deleted or hashed before export.
Metric labels are restricted to bounded service, route, method, status,
outcome, model, agent and tool dimensions. User, transaction and request IDs
are forbidden as metric labels. Raw chain-of-thought is never collected.

## Status semantics

- `SOURCE_READY`: source/configuration exists.
- `STATIC_VALIDATION_PASS`: deterministic validation passed.
- `LOCAL_RUNTIME_VERIFIED`: a local runtime emitted and read back telemetry.
- `GKE_RUNTIME_VERIFIED`: an approved GKE run emitted and read back telemetry.
- `RUBRIC_VERIFIED`: the rubric evidence requirement is fully satisfied.

This campaign may reach the first three states only where a bounded local
runtime is actually executed. No source-only result is promoted to either GKE
or rubric verified.

## Approaches considered

1. Extend the existing Helm chart and Python hooks. Chosen because it preserves
   the approved architecture, resource ownership and current contracts.
2. Add a separate observability chart. Rejected because it duplicates values,
   policy and release wiring for a coursework-sized system.
3. Add a hosted or LLM-specific telemetry platform. Rejected because it adds
   technology without a rubric requirement and conflicts with the approved
   collector/Prometheus/Grafana architecture.
