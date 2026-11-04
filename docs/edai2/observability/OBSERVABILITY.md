# Observability: Metrics, Logs, Traces, LLM and Agent Telemetry

This report describes the observability stack of the GKE deployment and the telemetry it collects for:

- the Fraud API;
- the cluster's compute resources;
- application logs;
- distributed traces;
- the LLM server;
- the agents and their MCP tools.

Every value quoted below was read from the running system under real or bounded synthetic traffic. Exact PromQL, LogQL and TraceQL queries accompany each section.

## 1. Architecture

```text
Fraud API ─┐ (1) /metrics scrape                 BYO agents ─(4) OTLP metrics, traces, logs─┐
KServe ────┤                                     gateways  ─(5) OTLP traces──────────────────┤
vLLM ──────┤                                                                                 ▼
kubelet/cAdvisor ─(2) TLS scrape, nodes/metrics─▶ Prometheus ◀─(6) Prometheus exporter── otel-collector ─(7)─▶ Tempo
pod stdout ─(3) Alloy pod-log forwarder──────────────────────────────────────────────────▶ Loki ◀─(8) OTLP logs─┘
                                                     ▼
                                           Grafana (dashboards as code)
```

The stack runs inside the `ewallet-agents` namespace and is deployed by the `llm-agent` Helm chart.

**Collection paths:**

- **Pull.** Prometheus scrapes Prometheus endpoints directly: the Fraud API, KServe, vLLM and the kubelets.
- **Push.** Applications instrumented with OpenTelemetry (the BYO agents and the agentgateway instances) send OTLP to a single collector. The collector exports metrics to Prometheus, traces to Tempo and logs to Loki.
- **Log forwarding.** Workloads that only write to stdout (the gateways, the Fraud API, KServe) are picked up by a Grafana Alloy forwarder. It has namespaced read-only RBAC and redacts known secret patterns before shipping.

Dashboards are provisioned from JSON in the chart, so they are versioned with the code.

| Concern | Source |
|---|---|
| Prometheus, Loki, Tempo, Grafana, kubelet scrape jobs | [`templates/observability-stack.yaml`](../../../infra/helm/llm-agent-platform/templates/observability-stack.yaml) |
| OpenTelemetry collector | [`templates/otel-collector.yaml`](../../../infra/helm/llm-agent-platform/templates/otel-collector.yaml) |
| Alloy pod-log forwarder | [`templates/pod-logs.yaml`](../../../infra/helm/llm-agent-platform/templates/pod-logs.yaml) |
| Gateway tracing configuration | `gatewayTracing` in [`templates/_helpers.tpl`](../../../infra/helm/llm-agent-platform/templates/_helpers.tpl) |
| Dashboards | [`dashboards/`](../../../infra/helm/llm-agent-platform/dashboards): `ml-platform`, `platform-overview`, `logs-operations`, `distributed-tracing`, `llm-runtime`, `agent-mcp` |
| Fraud API metrics | [`ai_platform/inference/observability.py`](../../../ai_platform/inference/observability.py) |
| Agent counters and PII screen | [`agent_metrics.py`](../../../ai_platform/llm_agent/agent_metrics.py), [`prompt_safety.py`](../../../ai_platform/llm_agent/prompt_safety.py), [`byo_runtime.py`](../../../ai_platform/llm_agent/byo_runtime.py) |
| Value overlays | [`infra/helm/llm-agent-platform/n12/`](../../../infra/helm/llm-agent-platform/n12) |
| Tests | [`test_n12_agent_telemetry.py`](../../../tests/llm_agent/test_n12_agent_telemetry.py), [`test_n12_chart.py`](../../../tests/llm_agent/test_n12_chart.py) |

## 2. Web API metrics (Fraud API)

The Fraud API exports `fraud_inference_requests_total{outcome, error_code}` and a latency histogram.

To make the failure panels meaningful, a bounded batch of 30 requests was sent:

- 25 requests for known customers. Each returned its expected fraud probability.
- 5 requests for an unknown customer. Each was rejected with `424 missing_feature`, because no online features exist for that customer.

| Metric | PromQL | Value |
|---|---|---|
| Requests per second | `sum by (outcome)(rate(fraud_inference_requests_total{job="fraud-api"}[5m]))` | success 0.093, error 0.019 |
| Number of requests | `sum(increase(fraud_inference_requests_total{job="fraud-api"}[15m]))` | 31.0 for 30 sent (`increase()` extrapolates at the window edges) |
| Number of failures | `sum by (error_code)(increase(…{outcome="error"}[15m]))` | `missing_feature` 5.17 |

![Fraud API requests per second](../../evidence/edai2/screenshots/observability/46_fraud_api_req_per_sec.png)

![Fraud API total requests](../../evidence/edai2/screenshots/observability/46_fraud_api_total_requests.png)

![Fraud API failed requests](../../evidence/edai2/screenshots/observability/46_fraud_api_failed_requests.png)

*Grafana `ml-platform` dashboard: request rate by outcome, total requests, and failed requests by error code. The five failures are the five unknown-customer requests.*

## 3. Compute telemetry (CPU, memory, network, disk)

Prometheus scrapes each kubelet directly over verified TLS at `https://<node>:10250/metrics/cadvisor` and `/metrics`. All 8 kubelet targets are up.

**Least privilege.** The ClusterRole grants only `get` on `nodes` and `nodes/metrics`. `kubectl auth can-i --subresource` confirms that the service account cannot reach `nodes/proxy`, logs, stats, pods or Secrets. A chart test ([`test_n12_chart.py`](../../../tests/llm_agent/test_n12_chart.py)) prevents this from regressing.

Values observed at capture time:

| Signal | Example |
|---|---|
| CPU | Grafana pod 0.012 cores (top consumer at idle) |
| Memory | `vllm-primary` 6.43 GB working set |
| Network | receive: Prometheus 22.8 kB/s; transmit: drift-specialist 0.8 kB/s |
| Disk I/O | Grafana 57.9 kB/s |
| Node root filesystem | 20–29 % used on the four nodes |
| Persistent volumes | usage ratio for 15 PVCs |

![CPU](../../evidence/edai2/screenshots/observability/47_cpu.png)

![Memory](../../evidence/edai2/screenshots/observability/47_memory.png)

![Network](../../evidence/edai2/screenshots/observability/47_network.png)

![Disk I/O](../../evidence/edai2/screenshots/observability/47_disk_io.png)

![Node filesystem](../../evidence/edai2/screenshots/observability/47_node_fs.png)

![PVC usage](../../evidence/edai2/screenshots/observability/47_pvc.png)

*Grafana `platform-overview` dashboard: per-pod CPU, memory, network and disk I/O from cAdvisor; node filesystem and PVC usage from the kubelet.*

## 4. Logs

Loki receives logs from eight services:

| Services | Delivery |
|---|---|
| fraud-coordinator, feature-rag-specialist, feature-rag-mcp | OTLP audit logs from the applications |
| a2a-gateway, agentgateway, mcp-gateway, fraud-api, fraud-detector (KServe) | Alloy pod-log forwarder |

**Correlation.** Every agent log line carries the business `request_id` and the W3C trace ID, so one investigation can be followed across services. For trace `6de09d68d7a4b70fc9012b00e41d13d7`:

- mcp-gateway: 8 lines;
- a2a-gateway: 1 line;
- feature-rag-mcp: 3 lines.

**Redaction check.** A scan of 6,256 shipped lines found none of the real credential values and no credential patterns.

![Gateway and MCP logs by trace ID](../../evidence/edai2/screenshots/observability/48_loki_gateways_by_trace.png)

*Loki query by trace ID across the gateways and the MCP server.*

![Fraud API and KServe logs](../../evidence/edai2/screenshots/observability/48_loki_fraud_api_kserve.png)

*Fraud API and KServe predictor logs in Loki.*

![Log volume by service](../../evidence/edai2/screenshots/observability/48_loki_volume_by_service.png)

*Log volume for each of the eight services.*

![Agent audit logs by request ID](../../evidence/edai2/screenshots/observability/48_loki_agents_by_request_id.png)

*Agent audit events for request `nl-eb00ade694f158ff85b004a35a9d1051`.*

## 5. Distributed traces

The BYO agents create spans for:

- the A2A task;
- the LLM planning call;
- each MCP tool call;
- the Feast and KServe clients behind the tools.

The three agentgateway instances (LLM, MCP and A2A) export their own spans, configured through `gatewayTracing`. Context propagates through every hop with W3C `traceparent`.

Trace `f341b410568fb649ee0f95bba50cd27d` is one real analyst question asked in the kagent UI. It contains **251 spans** and follows the full path:

1. kagent;
2. fraud-coordinator;
3. LLM gateway → vLLM;
4. a2a-gateway;
5. feature-rag-specialist;
6. mcp-gateway;
7. feature-rag-mcp;
8. Feast, KServe and RAG.

Every cross-service edge has a correct parent link. The spans contain no credentials and no PII.

![Trace waterfall](../../evidence/edai2/screenshots/observability/49_tempo_trace.png)

*Grafana Tempo trace view of `f341b410…`.*

![Gateway spans](../../evidence/edai2/screenshots/observability/49_tempo_gateway_spans.png)

*TraceQL span table: spans emitted by the A2A, MCP and LLM gateways within the same trace.*

## 6. LLM telemetry

vLLM exports token counts and latency histograms. agentgateway independently exports `gen_ai` token metrics. The two sources agree.

**PII screening.** Before any prompt leaves an agent, the PII screen ([`prompt_safety.py`](../../../ai_platform/llm_agent/prompt_safety.py)) runs:

- It masks e-mail addresses, Vietnamese mobile numbers and Luhn-valid card numbers.
- It counts the prompt as `clean` or `pii_masked`, and counts detections by type.
- Detected values are never exported or logged.

| Measure | Source | Value |
|---|---|---|
| Input tokens per request | `vllm:request_prompt_tokens` (sum/count); agentgateway `gen_ai_token_type="input"` | 707 |
| Output tokens per request | `vllm:request_generation_tokens` | 264 |
| Total tokens per request | sum of the two | 971 |
| End-to-end generation time | `vllm:e2e_request_latency_seconds` p50 / p95 | 50 s / 114 s |
| Time to first token | `vllm:time_to_first_token_seconds` p50 / p95 | 5 s / 38 s |
| Prompts caught by the PII screen | `ewallet_llm_prompts_screened_total{result="pii_masked"}`; `…pii_detections_total{pii_type}` | 1 prompt (email 1, phone 1) |
| PII in logs or answers | Loki search | none |

The latencies reflect a 0.6B-parameter model served on 2 CPU cores, with agent prompts of about 700 tokens.

![Tokens per request](../../evidence/edai2/screenshots/llm/02_grafana_tokens_per_request.png)

![TTFT](../../evidence/edai2/screenshots/llm/03_grafana_ttft.png)

![Generation round-trip](../../evidence/edai2/screenshots/llm/04_grafana_generation_rtt.png)

*Grafana `llm-runtime` dashboard: tokens per request, TTFT and end-to-end generation latency.*

![Prompts caught by the PII screen](../../evidence/edai2/screenshots/observability/50_pii_caught.png)

![PII detections by type](../../evidence/edai2/screenshots/observability/50_pii_by_type.png)

*Request `nl-89422d35e28d62650705782f2f25b1f0` was a kagent UI prompt containing a synthetic e-mail address and phone number. The answer is shown in [Agents §6](../agents/AGENTS_REGISTRY_GATEWAYS.md#6-agent-conversations-in-the-kagent-ui).*

## 7. Agent and MCP telemetry

Each BYO agent exports `ewallet_agent_invocations_total{agent, mode, outcome}`. The MCP server exports per-tool call and failure counters.

**Bounded labels.** Every label comes from a closed set, so the number of series is fixed.

**Correct counts across replicas and restarts.** Three design choices make the counts exact:

- each pod reports its own `service.instance.id`, so the two replicas never overwrite each other in the collector;
- every series is created at zero when the process starts, so the first real call is visible to range queries;
- the dashboard computes `max − min` per series over the selected window and sums across pods, so a pod restart does not lose or double-count calls.

The dashboard was compared with the agents' audit log over fixed windows:

| Measure | Dashboard | Audit / server logs |
|---|---|---|
| Agent calls, two replicas serving | 6 | 6 |
| Agent calls across a pod replacement | 9 | 9 |
| MCP calls per tool | `feature_lookup` 17, `fraud_scoring` 16, `knowledge_search` 19 | equal |
| MCP failures per tool | `feature_lookup` 1 (an unknown customer; the specialist refused to answer rather than invent features) | equal |

![Agent calls across a pod replacement](../../evidence/edai2/screenshots/observability/51_agent_calls_replacement_window.png)

![Agent calls by outcome](../../evidence/edai2/screenshots/observability/51_agent_calls_by_outcome.png)

![MCP calls per tool](../../evidence/edai2/screenshots/observability/51_mcp_calls_per_tool.png)

![MCP failures per tool](../../evidence/edai2/screenshots/observability/51_mcp_failures_per_tool.png)

*Grafana `agent-mcp` dashboard: calls per agent, calls by outcome, calls per MCP tool, and failures per tool.*

## 8. Lessons from the rollout

Several issues appeared only with real traffic. They shaped the final design:

| Observation | Cause | Resolution |
|---|---|---|
| Per-agent counts were too low with two replicas | Both replicas exported identical series, which overwrote each other in the collector | Per-pod `service.instance.id` |
| The first call after a pod started was missing | A counter appeared already at 1, so `increase()` saw no change | Zero-initialised series |
| Totals were slightly too high | `increase()` extrapolates to the window edges | `max − min` panels for exact counts |
| Gateway spans were absent after a configuration reload | agentgateway starts its tracer only at process start | One controlled restart after enabling tracing |
