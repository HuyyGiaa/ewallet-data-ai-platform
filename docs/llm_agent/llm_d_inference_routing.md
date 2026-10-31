# llm-d inference-aware routing

Status: **SOURCE_READY + TEST_PASS. Not deployed.** Disabled by default
(`llmd.enabled: false`). Rubric edai-2 rows 3–6 (7 points) link the llm-d
quickstart (cell A3 hyperlink).

## Integration mode and why

| Option | Verdict |
|---|---|
| agentgateway Kubernetes Gateway API mode + GAIE InferencePool + llm-d router EPP | **Chosen.** Documented by agentgateway for v1.7.0, our pinned release (inference-routing quickstart pins `agentgateway`/`agentgateway-crds` v1.7.0, GAIE v1.6.2, llm-d router chart v0.9.0, `provider.name=none`). |
| Our existing standalone agentgateway with InferencePool | Not possible: "Standalone request scheduler mode does not support `InferencePool`" (agentgateway standalone inference docs). |
| agentgateway standalone + EPP sidecar (`inferenceRouting`, passthrough) | Not used: no pool discovery, a second EPP lifecycle to own, and no doc example for multi-pod discovery. |
| llm-d Standalone Router (built-in Envoy proxy, `router.proxy.enabled`) | Not used: would add a third proxy beside two agentgateway roles. It duplicates what the agentgateway inference Gateway already does. |
| Full llm-d quickstart (2× Qwen3-32B on NVIDIA GPUs) | Out of scope: CPU-first architecture, laptop and GKE budgets. |

Request path when enabled:

```
kagent ModelConfig ewallet-global-llm
  -> agentgateway (standalone, this chart): auth key, request/token rate limits,
     timeouts, retries, circuit breaking, failover
       priority 0: ewallet-fraud-llm-routed -> llm-inference-gateway:80
                     (agentgateway v1.7.0 controller-provisioned data plane)
                     -> HTTPRoute -> InferencePool ewallet-fraud-llm
                     -> llm-d EPP (ext_proc, port 9002) picks a primary vLLM pod
       priority 1: ewallet-fraud-llm-primary -> vllm-primary:8000 (direct)
       priority 2: ewallet-fraud-llm-fallback -> vllm-fallback:8000 (direct)
```

The response header `x-ewallet-backend` (`llm-d`, `primary` or `fallback`)
shows which path served a request. All three paths serve the same model
identity (`ewallet-fraud-llm-v1`, Qwen3-0.6B at the pinned revision).

The two agentgateway roles stay separate. The standalone instance is the model
policy layer. The controller-provisioned `llm-inference-gateway` only routes to
the pool. Neither is the MCP Gateway (Phase D, still to be built) nor the
external Istio Gateway.

## What llm-d adds, and what it does not

- The EPP scores endpoints with the chart's default profile: `queue-scorer`
  (weight 2), `kv-cache-utilization-scorer` (2) and `prefix-cache-scorer` (3).
  It reads vLLM `/metrics` through `metrics-data-source`.
- With one primary replica (the laptop profile), every request goes to the same
  pod. Showing a real routing decision needs at least two primary replicas.
  That is a runtime-gated step.
- Not enabled: GPU, NIXL/RDMA, prefill/decode disaggregation, KV-event
  distributed cache, latency predictor.
- Not claimed: KV-cache-aware optimization. The scorer needs vLLM's KV-cache
  metric. vLLM 0.32.0 CPU logs "CPU KV cache usage", but the exported
  Prometheus metric name and the EPP's ability to parse it are UNVERIFIED
  until a runtime scrape.

## Failure behaviour

- `failureMode: FailClose` (the GAIE v1 default; the chart default is
  `FailOpen`). If the EPP is unresponsive, the inference Gateway fails the
  request. Two consecutive failures then evict the routed model for
  `circuitBreakDuration`, and agentgateway fails over to the direct primary.
  FailOpen would hide an EPP outage behind unscored routing.
- agentgateway waits up to 10 s for the EPP (agentgateway docs). A request held
  longer by EPP flow control returns 500, which also triggers failover.

## Security and telemetry

- EPP identity: ServiceAccount `ewallet-fraud-llm-epp`, namespaced Role (get,
  list and watch pods and inferencepools, plus leader election). It is declared
  as the `llmd-epp` flow workload.
- Enforced flow: `llmd-epp-to-vllm-metrics` (8000).
- Unresolved until runtime readback: the inference Gateway data-plane identity
  (pod labels and ServiceAccount provisioned by the controller), its flows
  agentgateway → gateway:80, gateway → EPP:9002 and gateway → vLLM:8000, and
  EPP → API server:443. Under default-deny, the routed path therefore cannot
  serve until those flows are read back and declared. Failover keeps the direct
  path working meanwhile.
- The EPP image runs without a chart-configurable `securityContext`. It passes
  Pod Security Baseline but not Restricted, which is a residual gap of the
  upstream chart.
- EPP tracing exports OTLP to `otel-collector:4317`. The collector is still
  open (Gate G).

## Versions (infra/helm/llm-d/versions.env)

| Component | Pin |
|---|---|
| agentgateway controller + CRDs | v1.7.0, chart digests recorded |
| GAIE CRDs | v1.6.2 manifest, sha256 recorded; InferencePool `inference.networking.k8s.io/v1` |
| llm-d router chart / EPP image | v0.9.0, chart digest recorded |
| Gateway API CRDs | kind serves v1.7.0 (installed with Istio 1.30.7); agentgateway docs use v1.6.3. Not downgraded. Controller compatibility is a runtime gate. |

`scripts/llm_agent/render_llmd.sh` re-pulls the chart (digest-checked) and the
GAIE manifest (sha256-checked). It re-renders
`infra/helm/llm-d/rendered/router-v0.9.0.yaml` and the extracted InferencePool
v1 contract that `tests/llm_agent/test_llmd_routing.py` validates against.

## Runtime

See `docs/llm_agent/runtime_runbook.md`, milestones R1 (direct path) and R2
(llm-d). Neither has run: DP2 was active on 2026-10-31.
