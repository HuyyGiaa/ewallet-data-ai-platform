# Observability

## Phạm vi và trạng thái

Implementation này cung cấp source, cấu hình, contract test và static
validation cho API, Kubernetes, LLM, Agent và MCP telemetry. Không có GKE
deployment hoặc screenshot mới trong campaign này.

Các mức trạng thái được dùng nhất quán:

- `SOURCE_READY`: source và cấu hình đã tồn tại.
- `STATIC_VALIDATION_PASS`: JSON/YAML, Helm render, contract và privacy gate đã
  qua kiểm tra tĩnh.
- `LOCAL_RUNTIME_VERIFIED`: local runtime đã emit và read back telemetry thật.
- `GKE_RUNTIME_VERIFIED`: GKE runtime đã emit và read back telemetry thật.
- `RUBRIC_VERIFIED`: evidence đáp ứng đầy đủ yêu cầu rubric.

Trạng thái hiện tại là `STATIC_VALIDATION_PASS`. Redaction smoke cũ vẫn là
local runtime evidence hợp lệ cho policy redaction tại SHA cũ, nhưng không được
dùng để nâng toàn bộ implementation hiện tại lên `LOCAL_RUNTIME_VERIFIED`.

## Kiến trúc

```text
E-Wallet services / agentgateway / vLLM
                |
                v
      OpenTelemetry Collector
       /          |          \
Prometheus       Loki        Tempo
       \          |          /
                Grafana
```

Collector áp dụng `memory_limiter`, xóa field nhạy cảm, redaction theo pattern
và batch trước export. Prometheus scrape collector, agentgateway, vLLM,
kubelet/cAdvisor và kube-state-metrics. Loki là log backend duy nhất, Tempo là
trace backend duy nhất. Cả bốn backend cùng Collector đều dùng resource
requests/limits; retention mặc định của local stack là 24 giờ. Stack là opt-in,
disabled trong default, local mặc định và GKE values.

Prometheus, Loki, Tempo và Grafana được chọn vì chúng khớp kiến trúc đã phê
duyệt, có thể provision bằng file và đủ nhẹ cho local verification có kiểm
soát. Không dùng Langfuse, PushGateway, Elasticsearch, VictoriaLogs, backend
trùng chức năng hoặc alerting platform riêng vì rubric hiện tại không yêu cầu.

## Metrics inventory

| Nhóm | Metrics/contracts | Trạng thái |
|---|---|---|
| API | request count/rate, error rate, in-progress, p50/p95/p99 latency, dependency stage latency | Source và query sẵn sàng; runtime readback pending |
| Kubernetes | CPU, RAM, disk, network, pod restart, OOMKilled, requests/limits | Standard cAdvisor/kube-state queries; runtime readback pending |
| LLM | input/output tokens, TTFT, E2E, queue, running/waiting requests, errors, retries, CPU/RAM | vLLM 0.32.0 names cần xác nhận trên runtime; agentgateway names đã kiểm tra từ binary |
| Agent | execution count/failure/duration và agent/tool span relation | Span attribute names runtime pending |
| MCP | gateway transport calls, application semantic outcomes, tool duration | Source ready; `isError=true` và non-`ok` structured status là failure |

Dashboard không dùng `or vector(0)`. Series chưa emit sẽ hiển thị missing, không
biến thành số 0. Không có user ID, transaction ID, request ID hoặc trace ID làm
metric label.

## Logging và tracing

MCP boundary phát JSON event gồm timestamp, severity, service, event, tool,
outcome, duration, trace ID và span ID. Authorization, credentials, API keys,
cookies, prompt/completion, messages, tool arguments, account/user/transaction
identifiers và chain-of-thought bị redacted. Raw arguments không được log.

W3C `traceparent` được validate hoặc tạo tại service boundary. MCP metrics và
events giữ correlation trace/span. Collector có span-metrics cho
`gen_ai.agent.name` và `gen_ai.tool.name`; tên attribute này vẫn
`runtime-pending` cho đến khi có trace readback thật.

## Dashboards

- `E-Wallet Platform Overview`: API rate/error/p50/p95/p99, CPU/RAM/disk/network,
  restarts, OOMKilled và requests/limits.
- `E-Wallet LLM Runtime`: tokens, TTFT, E2E, queue/running/waiting, errors,
  retries và CPU/RAM.
- `E-Wallet Agent & MCP`: agent executions, MCP calls, tool latency, semantic
  failures và trace-derived failures.

Grafana datasource provisioning dùng đúng một Prometheus, Loki và Tempo.

## Rubric mapping từ workbook gốc

Sheet `edai-2 (50%)`:

| Row | Requirement | Implementation | Test | Runtime evidence | Status |
|---|---|---|---|---|---|
| 46 | Web API metrics | agentgateway + inference OTel, Platform Overview | foundation/chart contracts | Chưa có dashboard readback mới | STATIC_VALIDATION_PASS |
| 47 | CPU/RAM/disk/network | cAdvisor + kube-state Prometheus jobs | scrape/query contracts | Chưa có cluster readback | STATIC_VALIDATION_PASS |
| 48 | Logs | redacted OTLP logs -> Loki | redaction/config contracts | Local redaction smoke cũ; Loki readback pending | STATIC_VALIDATION_PASS |
| 49 | Traces | W3C traceparent + OTLP -> Tempo | propagation/collector contracts | Trace continuity readback pending | STATIC_VALIDATION_PASS |
| 50 | LLM tokens, RTT, TTFT, PII safety | vLLM/agentgateway metrics + LLM dashboard | metric/query/redaction contracts | Native metric labels/readback pending | STATIC_VALIDATION_PASS |
| 51 | Agent and MCP calls/failures | spanmetrics + semantic MCP OTel | semantic failure/low-cardinality tests | Agent trace attributes pending | STATIC_VALIDATION_PASS |

Sheet `rubic final-coursework (final -` (EDAI-1 rubric liên quan):

| Row | Requirement | Implementation | Test | Runtime evidence | Status |
|---|---|---|---|---|---|
| 46 | Web API metrics | Giống EDAI-2 row 46 | Contract tests | Pending | STATIC_VALIDATION_PASS |
| 47 | Computing telemetry | Giống EDAI-2 row 47 | Contract tests | Pending | STATIC_VALIDATION_PASS |
| 48 | Logs | Giống EDAI-2 row 48 | Privacy/config tests | Pending | STATIC_VALIDATION_PASS |
| 49 | Traces | Giống EDAI-2 row 49 | Propagation/config tests | Pending | STATIC_VALIDATION_PASS |
| 50 | ML drift telemetry | Thuộc MLOps owner, không sửa trong workstream này | N/A | Không có evidence mới | SOURCE_READY |
| 51 | Trigger retraining | Thuộc MLOps owner, không sửa trong workstream này | N/A | Không có evidence mới | SOURCE_READY |

Không row nào được nâng lên `RUBRIC_VERIFIED`; awarded points vẫn bằng 0 trong
fail-closed evidence matrix.

## Reproduction

```bash
python3 -m unittest discover -s tests/observability -v
python3 scripts/observability/validate.py
helm lint infra/helm/llm-agent-platform -f infra/helm/llm-agent-platform/values-local.yaml
helm template llm-agent infra/helm/llm-agent-platform \
  -f infra/helm/llm-agent-platform/values-local.yaml \
  --set dashboards.enabled=true \
  --set otelCollector.enabled=true \
  --set otelCollector.tracesEndpoint=tempo:4317 \
  --set otelCollector.logsEndpoint=http://loki:3100/otlp \
  --set observabilityStack.enabled=true >/dev/null
```

GKE verification sau này phải capture dashboard screenshots, Prometheus/Loki/
Tempo queries, trace continuity, service identity, Git SHA, config hash và
resource usage. Campaign này không được phép deploy.

## Living Decision copy-ready entry

**LD21.x — Observability source/static milestone — SYNC_PENDING**

Ngày 01/11/2026 (ICT). Triển khai một collector OpenTelemetry có memory limiter,
PII/credential redaction và batch; một Prometheus, Loki và Tempo backend; ba
Grafana dashboard cho Platform, LLM và Agent/MCP; OTel MCP metrics phân biệt
semantic failure với HTTP transport success; W3C trace correlation; digest-
pinned images, bounded resources và 24-hour local retention. Default/GKE
profiles không deploy backend. Kết quả đạt `STATIC_VALIDATION_PASS`; không có
GKE action, dashboard screenshot hoặc runtime readback mới, do đó EDAI-2 rows
46-51 vẫn PARTIAL, 0 điểm awarded và chưa `RUBRIC_VERIFIED`.

