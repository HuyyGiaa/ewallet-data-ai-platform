# CI/CD, observability and gateway equivalence

## CI/CD (rubric rows 33–35)

`Jenkinsfile.llm-agent` (extended, not replaced). Stages:

1. **Checkout exact SHA** (`EXPECTED_GIT_SHA`).
2. **Changed components.** `ops/llm_agent/changed_components.py` now also maps
   the image build context, prompts, agent templates, policy gateways, llm-d,
   agentregistry, KEDA and the kagent controller values.
3. **System tests.** All of `tests/llm_agent`.
4. **MCP and RAG tests in the locked uv project** (`uv lock --check`,
   `uv run --frozen`).
5. **Gateway and collector config validation**
   (`scripts/llm_agent/validate_gateway_configs.sh`: pinned agentgateway and
   otelcol binaries).
6. **Preflight, Helm lint and render.**
7. With `DEPLOY_APPROVED` only:
   - `build_changed_images.sh`: builds only images whose components changed.
     Today that is `mcp-tools` (`--target mcp-server`) and `llm-agent-tooling`.
     Images are tagged by the Git SHA with OCI revision labels. Agents,
     gateways, llm-d and the registry use pinned upstream images, so changes
     to them are configuration-only deploys.
   - `scan_and_push.sh`: Trivy 0.75.0 pinned by digest; any fixable
     CRITICAL/HIGH finding fails the stage before the push. After the push it
     records `repo@sha256`.
   - `deploy_atomic.sh`: `helm upgrade --install --atomic --wait` with an
     explicit `KUBE_CONTEXT`. The image is set by digest only (a tag is
     refused), together with `mcpServices.identity` (digest and Git SHA).
     Then rollout status for every Deployment, plus Helm history and workload
     evidence.
   - `rollback.sh` (post-failure): `helm rollback` to the last good revision.

Secrets stay in Jenkins credentials and Kubernetes Secrets, never in Git
(`ARTIFACT_REGISTRY` is configuration, not a secret).
`tests/llm_agent/test_ci_scripts.py` runs the four scripts against fake
`docker`/`helm`/`kubectl`, covering:
- inputs and image selection;
- scan-before-push, and that a vulnerable image is never pushed;
- digest-only deploys;
- the rollback target.

The test also checks that every script the Jenkinsfile calls exists.

**Not done:** a real Jenkins run (no Jenkins execution evidence), an image
build and push to Artifact Registry, and a RAG DAG deploy job. The DAG is
synced by the Airflow deployment, outside this pipeline.

## Observability (rows 46–51)

| Signal | Source | Verified |
|---|---|---|
| Request rate, failures, latency per route | agentgateway `agentgateway_requests_total`, `agentgateway_request_duration_seconds` (labels route, status, gateway, backend) | Names and labels read from the v1.7.0 binary (`agentgateway_metric_names.txt`) |
| MCP tool calls per tool | `agentgateway_mcp_requests_total{method="tools/call", resource=<tool>}` | Same |
| LLM end-to-end latency, errors, retries | `agentgateway_gen_ai_server_request_duration` (gen_ai_request_model, error_type), `agentgateway_retries_total` | Same |
| Tokens per request, TTFT | `agentgateway_gen_ai_client_token_usage`, `agentgateway_gen_ai_server_time_to_first_token` | **UNVERIFIED**: these appear only after a successful LLM response (R1) |
| Agent calls and failures per agent/tool | Collector `spanmetrics` over kagent traces (`ewallet_traces_span_metrics_calls_total`, dimensions `gen_ai.agent.name`, `gen_ai.tool.name`) | **UNVERIFIED**: kagent span attribute names need an R3 trace |
| CPU and RAM | cAdvisor `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes` | Standard |
| Traces | W3C `traceparent` propagated by the MCP servers; kagent controller, EPP and gateways export OTLP to `otel-collector:4317`, then to Jaeger/Tempo when `otelCollector.tracesEndpoint` is set | Collector config validated |
| Logs | JSON audit logs (MCP servers, gateways); the collector logs pipeline is redacted | — |

- **Collector:** `otel/opentelemetry-collector-contrib:0.162.0`, pinned by
  digest. The pipeline is memory_limiter → `attributes/redact` (deletes
  credentials and prompt/completion bodies, hashes user ids) → `redaction`
  (masks card numbers, emails and +84 phone numbers in any value) → batch.
- **Redaction smoke** (`scripts/llm_agent/otel_redaction_smoke.sh`, local
  Docker): a span carrying a synthetic bearer token, card number, email, user
  id and prompt was exported with none of them; the non-sensitive attribute was
  kept. Evidence: `campaign_20261031/otel_redaction_smoke.json`. This is the
  PII safety-event proof. The redaction processor also counts masked values on
  each span.
- **Dashboard:** `infra/helm/llm-agent-platform/dashboards/llm-agent.json`,
  14 panels, each marked verified or UNVERIFIED. It ships as a Grafana sidecar
  ConfigMap (`dashboards.enabled`).
- **Not done:** Prometheus, Grafana, Jaeger and Kibana installs, and
  screenshots.

## External gateway: rubric NGINX vs canonical Istio (rows 37–43)

The rubric names the NGINX Ingress Controller. The frozen architecture uses
Istio Gateway API, and NGINX is not added. Equivalence, as a stated
instructor-acceptance risk:

| Row | Requirement | Canonical equivalent | Status |
|---|---|---|---|
| 37–39 | Grafana / log / trace UIs behind the gateway | HTTPRoutes on the Istio Gateway (not yet in source; the services are not installed) | OPEN |
| 40 | Agent test UI | kagent UI (`ui.replicas: 1` in kagent-values), route OPEN | OPEN |
| 41 | Registry UI | agentregistry UI on 12121 (verified locally), route OPEN | PARTIAL |
| 42 | Basic auth + rate limit at the gateway | Istio has no basic auth. Options: an agentgateway route with basicAuth/JWT and localRateLimit, or `oauth2-proxy` from the kagent chart. Needs a decision. | DECISION_REQUIRED |
| 43 | Domain + HTTPS for the data Web API | Gateway HTTPS listener with `tlsSecretName` (source exists); certificate and DNS OPEN | PARTIAL |
