# EDAI-2 unified CI/CD and cross-platform observability: runtime proof

| Item | Value |
|---|---|
| Date | 2026-11-03 (UTC 04:22–04:50) |
| Branch | `codex/llm-g2-llmd-routing` (local only, not pushed) |
| Jenkins source snapshot | base `8d34a1a`, bundle sha256 `880b7d74…2911` |
| Observability fix | `2078a79`, deployed as Helm `llm-agent` revision 32 |
| Cluster | GKE `ewallet-dev`, `asia-southeast1-b`, project `ewallet-data-ai-platform-hg26` |
| Evidence | [`docs/evidence/edai2/unified_cicd_observability_20261102T160000Z/`](../evidence/edai2/unified_cicd_observability_20261102T160000Z/README.md) |

This run resumes the campaign at its real checkpoint. It does not rebuild the
Data or ML platforms, regenerate datasets or retrain models.

## 1. Jenkins

### Runtime

One bounded controller runs in namespace `ewallet-ci`:

- Jenkins 2.588 with digest `jenkins-controller@sha256:9c301d1a…565d`, built from `9e53b13`.
- No Ingress, ServiceAccount token, Docker socket or deploy credential.
- A ResourceQuota, a default-deny NetworkPolicy and an 8 Gi PVC.
- Access is only through `kubectl port-forward`.

An init container copies an immutable Git bundle from
`jenkins-source-snapshot@sha256:45f2a58c…ed10`, built from `8d34a1a`. Trivy
reports 0 HIGH/CRITICAL findings for both images. The bundle holds three proof
branches, and each one adds a single marker file on top of `8d34a1a`:

| Ref | SHA | Changed path |
|---|---|---|
| `proof/ml` | `8074651` | `ai_platform/inference/.ci-proof` |
| `proof/agent` | `8fad87f` | `ai_platform/llm_agent/tools/.ci-proof` |
| `proof/docs` | `7570de8` | `docs/ci-proof.md` |

The bundle served by the pod was copied back and re-hashed. Its sha256 matches
`source-metadata.txt`, so the snapshot replaces webhook delivery of a local,
unpushed branch with exact provenance. It does not prove GitHub webhook
delivery.

### CPS correction

`edai2-ml-ci#1` failed during Groovy CPS compilation. The cause was that
`def run(Map configuration = [:])` generates a zero-argument `run()` overload,
which collides with the script's implicit `Script.run()`.

Commit `8d34a1a` removes the default value, and
`tests/ci/test_jenkins_runtime_contract.py` asserts the new signature. All
three callers (`Jenkinsfile`, `Jenkinsfile.ml`, `Jenkinsfile.llm-agent`) pass a
named Map. The shared `jenkins/pipeline/componentPipeline.groovy` remains the
single implementation.

Before the cloud builds, every `sh` step of the pipeline was replayed locally
inside the same controller image against the same bundle. All three proof refs
passed there first.

### Builds

Every parameter was passed explicitly. `properties([parameters(...)])` in the
shared pipeline overwrites the seeded Job DSL defaults (see §6).

| Build | Source SHA | Result | Stages | Scope detected | Tests |
|---|---|---|---|---|---|
| `edai2-ml-ci#2` | `8074651` | SUCCESS (45.5 s) | Checkout, Validate+scope, Component CI | `fraud-api` (unit `fraud_api`) | `uv lock --check`, `uv sync --locked`, 56 inference tests |
| `edai2-agent-ci#1` | `8fad87f` | SUCCESS (34.9 s) | same | `mcp-agents` | lock/sync `llm-agent-tooling`, 138 tests |
| `edai2-docs-change-detection#1` | `7570de8` | SUCCESS (16.3 s) | same | none, `non_runtime_paths_only` | no component executed (Component CI 0.3 s) |
| `edai2-agent-negative#1` | `8fad87f` | FAILURE (expected) | Component CI passed, then `Intentional negative contract gate` failed with exit 42 | `mcp-agents` | 138 tests passed before the gate |

In all four builds, `DEPLOY_APPROVED=false` and `Controlled release gate` never
ran. No deployment or rollback was triggered by Jenkins. Each build archives:

- `scope.json`
- `catalog-validation.json`
- `manifest.json` (status per stage)
- `lock-hashes.json`
- per-stage logs
- `SHA256SUMS`

The negative build also archives `negative-gate.txt` with the value
`blocked_before_release`.

## 2. Observability (shared stack, no second deployment)

The existing stack in `ewallet-agents` (OTel Collector, Prometheus, Tempo, Loki
and Grafana 12.3.0) was reused. All three Grafana datasources returned health
OK.

### G3 trace readback

Trace `29e3c317b7ac34854c08e62088fda88d` belongs to request
`edai2-regression-20261103-001` and contains 109 spans.

| Service | Server-side spans | Notes |
|---|---|---|
| fraud-coordinator | 27 SERVER | A2A handler, `g3.coordinator.execute` |
| feature-rag-specialist | 27 SERVER | `g3.feature_rag_specialist.execute` |
| feature-rag-mcp | 7 SERVER | `mcp.tool feature_lookup / fraud_scoring / knowledge_search` |

Some spans are client-side dependency spans emitted inside feature-rag-mcp.
They are **not** counted as server instrumentation of these services:

- `feast-online.get_features`
- `kserve.infer`
- the kagent-controller `/api/tasks` calls

A2A Gateway and MCP Gateway (agentgateway) propagate context but emit no spans
of their own.

### Logs

Loki holds 3 redacted `mcp.tool.completed` events that carry the same
`trace_id`, so logs correlate with traces. The request id is redacted from
these events. The only Loki `service_name` is `feature-rag-mcp`.

### Metrics coverage

| Area | Component | Live series |
|---|---|---|
| ML | KFP | `ewallet_httpcheck_status` 2xx = 1 (health only) |
| ML | MLflow | `ewallet_httpcheck_status` 2xx = 1 (health only) |
| ML | Feast Online | `ewallet_backend_client_*{backend="feast-online"}`, `ewallet_feast_feature_freshness_seconds`, Feast SDK metrics inside fraud-api |
| ML | KServe | `up{job="kserve-fraud-detector"}`=1, 18 predictor series, `ewallet_fraud_inference_stage_latency_seconds{stage="model_inference"}` |
| ML | Fraud API | `up`=1, `http_server_*` (probe traffic only) |
| ML | ML workflow status | **none** (no KFP run-state exporter) |
| LLM/Agent | vLLM/Qwen | `up{instance="vllm-primary:8000"}`=1, 42 successful requests (fallback scaled to 0 by design; its target reports down) |
| LLM/Agent | agentgateway (LLM) | 407 series, `agentgateway_requests_total`, `agentgateway_gen_ai_*` |
| LLM/Agent | Coordinator and Specialists | span metrics `ewallet_traces_span_metrics_calls_total` per `service_name` |
| LLM/Agent | MCP and RAG | `ewallet_ewallet_mcp_tool_calls_total{tool}` incl. `knowledge_search`, duration histogram |
| LLM/Agent | A2A/MCP gateways | **not scraped** |
| LLM/Agent | Idempotency Redis | **no exporter** |
| LLM/Agent | Agent HPA | **no kube-state-metrics**; verified through the Kubernetes API only |

### Grafana panel readback

Every Prometheus target of all 8 provisioned dashboards was executed through
Grafana `/api/ds/query`.

Before the fix, 28 of 57 targets returned data. The MCP panels were empty
because they queried `ewallet_mcp_tool_*`, while the collector's exporter
(`namespace: ewallet`) serves those series as `ewallet_ewallet_mcp_tool_*`.

Commit `2078a79` corrects `agent-mcp.json` and `ml-drift-pipeline.json` and
adds a contract test. Before rollout, the semantic diff of revision 31 against
the candidate contained only these changes:

- the 2 dashboard keys
- the Grafana `checksum/dashboards` annotation

After revision 32, 30 of 57 targets return data. "MCP calls by tool" and "MCP
tool latency p95" moved from empty to data. Rollback is
`helm rollback llm-agent 31 -n default`.

The remaining 27 empty targets fall into three groups:

- **Legitimately empty (4).** These series exist only after an event that did
  not occur in the window: MCP semantic failures, drift tool outcomes, Feast
  retrieval errors and backend errors.
- **Absent at the source (10).**
  - `agentgateway_mcp_requests_total`: the A2A/MCP gateways are not scraped.
  - `agentgateway_retries_total` and the TTFT histogram: agentgateway v1.7.0
    does not emit them.
  - `gen_ai_agent_name`: not a span-metrics dimension.
  - `otelcol_exporter_*`: the collector's internal telemetry is not scraped.
  - `fraud_inference_requests_total`: the Fraud API received no prediction
    traffic, because G3 reaches KServe through MCP.
- **No kubelet/cAdvisor or kube-state-metrics scrape (13).** These are the CPU,
  RAM, disk, network, restart and OOM targets.

## 3. Cross-platform G3 regression

Path: Coordinator → A2A Gateway → Feature-RAG Specialist → MCP Gateway →
feature-rag-mcp → Feast/KServe/RAG.

| Check | Result |
|---|---|
| Business response | `completed`, `status=ok`, `unavailable=[]`, 1.2 s |
| Feast | 4 features at `2026-05-31T23:59:56Z` |
| KServe | `fraud-detector/1`, p=0.06399, schema `03c0ea04…3176` |
| RAG | 3 citations from corpus `fraud-policy-v1-20261030` with checksums |
| Cross-replica idempotency | Same request replayed directly to both Coordinator pods. The business artifact is identical, and feature-rag-mcp logged exactly one execution (3 tool calls). |
| MCP authentication | Positive call with the service token succeeded. Missing, forged and revoked tokens each got HTTP 401. A drift token calling `fraud_scoring` got `Unknown tool`. |
| HPA | coordinator, feature-rag and drift each min 2 / max 3, at 2/2 Ready |
| Sandbox | `kagent-pilot` worker and its egress NetworkPolicy present; Substrate pods Running |
| Helm | Only `llm-agent` changed (31→32). Unchanged: `llm-agent-mcp` r6, `kagent` r5, `istiod` r13, `substrate` r1. |

The frozen ML feature contract was not touched.

## 4. EDAI-2 rubric classification

| Row | Requirement | Status | Reason |
|---|---|---|---|
| 33 | CI/CD RAG Data Pipeline | OPEN | `Jenkinsfile.rag` was not executed on this runtime |
| 34 | CI/CD Agent & MCP servers | PARTIAL | Jenkins test stage and negative gate ran; Jenkins did not build images or auto-deploy |
| 35 | CI/CD Coordinator | PARTIAL | Same pipeline (`mcp-agents` covers the coordinator unit); no Jenkins build or deploy |
| 36 | CI/CD streaming feature jobs | OPEN | Not executed |
| 46 | Web API req/s and failure metrics | VERIFIED | Gateway and HTTP request metrics, plus Grafana panels with data |
| 47 | CPU/RAM/disk/network in Prometheus/Grafana | PARTIAL | No container/node metrics are scraped |
| 48 | Centralized logs | PARTIAL | Loki works, but only feature-rag-mcp ships logs |
| 49 | Distributed traces | VERIFIED | Cross-service trace with server spans in 3 services |

## 5. Rollback

```bash
helm rollback llm-agent 31 -n default --wait --timeout 10m   # dashboards only
```

The Jenkins runtime is namespace-local. Removing it means deleting namespace
`ewallet-ci` and its PVC. This was not done.

## 6. Known gaps

1. No JUnit XML. The catalog runs `unittest`, so Jenkins archives only stage
   logs as test reports.
2. `properties([parameters(...)])` resets the seeded Job DSL defaults after the
   first build. Callers must pass every parameter explicitly.
3. Jenkins does not build or push images and does not deploy, because it has no
   Docker socket and no deploy credential.
4. Missing observability coverage:
   - A2A/MCP gateway metrics are not scraped.
   - Redis has no exporter.
   - There is no KFP run-state metric.
   - kubelet/cAdvisor and kube-state-metrics are absent; adding them needs a
     reviewed ClusterRole.
   - Loki has a single log source.
   - Gateways emit no spans.
5. The instrument name `ewallet_mcp_tool_*` combined with the exporter
   namespace produces a double prefix. Renaming the instrument needs an MCP
   image rebuild.
