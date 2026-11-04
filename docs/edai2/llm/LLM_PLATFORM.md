# LLM Inference Platform

This report describes the platform that serves the custom LLM used by the fraud-investigation agents:

- vLLM on CPU nodes in GKE;
- agentgateway in front of vLLM;
- one global kagent ModelConfig through which every agent reaches the model.

It also reports the two optimisation experiments that shaped the current serving configuration.

## 1. Overview and motivation

The agents use the LLM for a narrow task: understand an analyst's question and select a tool with its arguments (see [Agents](../agents/AGENTS_REGISTRY_GATEWAYS.md)). This task does not need a large model, so the platform serves **Qwen3-0.6B** on CPU nodes. Three reasons drove that choice:

- the GKE project has no GPU quota;
- the same nodes also run the MLOps workloads;
- tool calling with a small model is accurate enough when every argument is validated against the analyst's text.

**Design goals:**

- **One entry point.** Agents never talk to vLLM directly. agentgateway authenticates clients, enforces rate and token budgets, applies timeouts, and fails over between backends.
- **One model configuration.** All agents reference a single ModelConfig, so the model or endpoint can change in one place.
- **Reproducible serving.** The model weights are baked into a digest-pinned image, so a pod never downloads weights at start-up.

## 2. Architecture

```text
 (1) chat / A2A task                  (2) OpenAI-compatible request
kagent agents ───────────────▶ ModelConfig ewallet-global-llm ─────────────▶ agentgateway :3000 (llm listener)
(coordinator, specialists)       (provider OpenAI, baseUrl = agentgateway)       │ (3) apiKey strict, rate limit, timeouts
                                                                                  ▼
                                                     virtual model ewallet-fraud-llm (failover)
                                                     (4a) vllm-primary :8000 ──┐   (4b) vllm-fallback :8000
                                                                               ▼
                                                                   Qwen3-0.6B on vLLM 0.32 CPU
```

**Deployable units:**

- `vllm-primary` and `vllm-fallback` are Deployments;
- `agentgateway` is a Deployment;
- the ModelConfig is a kagent resource.

Arrows follow the request; the generated tokens stream back along the same path.

## 3. Implementation

### 3.1 Model server

| Item | Value |
|---|---|
| Model | `Qwen/Qwen3-0.6B`, revision `c1899de2…`, bfloat16; vLLM serves it as `ewallet-fraud-llm-v1`, and agentgateway exposes it as the virtual model `ewallet-fraud-llm` |
| Image | vLLM 0.32.0 CPU image, with the model baked into `vllm-cpu-model@sha256:f695b23e…` |
| Resources per pod | requests 2 CPU / 6 GiB; limits 3 CPU / 10 GiB; KV cache 2 GiB; `max-model-len 4096` |
| Scheduling | primary `max-num-seqs 4`; fallback `max-num-seqs 2` |
| Tool calling | `--tool-call-parser hermes`; `--reasoning-parser qwen3`; thinking disabled |
| Readiness | A warm-up request shaped like a real tool call is sent before the pod reports ready, so the first user request does not pay the cold compile and prefill cost |

Source:

- [`templates/vllm.yaml`](../../../infra/helm/llm-agent-platform/templates/vllm.yaml);
- values [`values-gke-cpu.yaml`](../../../infra/helm/llm-agent-platform/values-gke-cpu.yaml) and [`values-gke-cpu-g1.yaml`](../../../infra/helm/llm-agent-platform/values-gke-cpu-g1.yaml).

### 3.2 agentgateway

The gateway is configured in [`templates/agentgateway.yaml`](../../../infra/helm/llm-agent-platform/templates/agentgateway.yaml).

| Policy | Setting | Reason |
|---|---|---|
| Client authentication | `apiKey: mode: strict` with a dedicated client key | Clients cannot reach the model without their own key, and the vLLM key never leaves the gateway |
| Backend authentication | a separate `apiKey` for vLLM | vLLM rejects any caller other than the gateway |
| Rate limits | two `localRateLimit` buckets per model: requests and tokens | Bounds both request rate and token spend on a 2-core server |
| Timeouts | nested budgets. The chart refuses to render unless `modelConfig.timeoutSeconds > requestTimeout > llmRequestTimeout > backendRequestTimeout ≥ responseIdleTimeout` | Each layer gives up after the one beneath it, so a slow generation is never cut by an outer hop while still running inside |
| Retry | 502/503 only, at most one attempt. The chart refuses any 500/504 retry | A timed-out CPU generation is never replayed |
| Failover | virtual model `ewallet-fraud-llm` with primary → fallback; a backend is evicted after 2 consecutive failures | Keeps serving if one vLLM pod is lost |
| Observability | response header `x-ewallet-backend`; OpenTelemetry spans | Shows which backend served a request; gateway hops appear in traces |

### 3.3 Global ModelConfig

[`templates/modelconfig.yaml`](../../../infra/helm/llm-agent-platform/templates/modelconfig.yaml) renders one `kagent.dev/v1alpha2 ModelConfig`, named `ewallet-global-llm`:

| Field | Value | Effect |
|---|---|---|
| `provider` | `OpenAI` | kagent uses the OpenAI Chat Completions API |
| `model` | `ewallet-fraud-llm` | the gateway's virtual model, which maps to the versioned vLLM model |
| `openAI.baseUrl` | `http://agentgateway.<ns>.svc.cluster.local:3000/v1` | every request goes through the gateway |
| `apiKeySecret` / `apiKeySecretKey` | the gateway client key | the key agentgateway checks |
| `timeout` | outermost timeout budget | aligned with the gateway timeouts |

The Declarative drift agent references this ModelConfig directly. The BYO agents read the same endpoint and client key from their deployment configuration.

## 4. Runtime verification

**Deployment readback.**

- `vllm-primary` runs 1/1 behind agentgateway.
- `vllm-fallback` is deployed but currently scaled to zero replicas to save CPU on the shared nodes.

![Drift agent answering through the global ModelConfig](../../evidence/edai2/screenshots/llm/01_kagent_drift_chat_modelconfig.png)

*A kagent conversation with the Declarative drift agent (session `01a1a419`). The header shows provider `OpenAI` and model `ewallet-fraud-llm`: the answer was generated by the deployed Qwen3-0.6B, through ModelConfig `ewallet-global-llm` and agentgateway.*

**Gateway verification.** The gateway was tested on the cluster against both backends. The artefacts are in [`docs/evidence/llm_agent/g1_20261031T231145Z/`](../../evidence/llm_agent/g1_20261031T231145Z).

| Check | Result |
|---|---|
| Tool calls through the gateway | Streaming and non-streaming requests returned valid `feature_lookup` tool calls, served by `x-ewallet-backend: primary` |
| Authentication | A wrong key, a missing key, and the backend key used as a client key were each rejected with 401 |
| Failover | With the primary scaled to zero, requests were served by the fallback; the fallback's `vllm:request_success_total` rose from 1 to 6 |

![LLM gateway spans](../../evidence/edai2/screenshots/llm/05_tempo_llm_gateway_spans.png)

*Tempo spans of the LLM gateway (route `internal/llm`) inside a real agent trace (`f341b410…`): the agent's planning call passes through agentgateway to vLLM.*

## 5. Performance and optimisation

### 5.1 Baseline

The first deployment was measured through agentgateway: 8 streamed requests, concurrency 1, up to 128 output tokens.

| Metric | Value |
|---|---|
| TTFT p50 / p95 | 3.48 s / 3.69 s |
| Inter-token latency, mean | 206 ms |
| End-to-end p50 / p95 | 11.7 s / 12.0 s |
| Output throughput | 3.48 tokens/s |
| vLLM CPU (two pods) | 1.51 cores mean |
| Errors | 0 % |

### 5.2 Experiment 1: serving parameters

**Problem.** Under concurrent load the server used much less CPU than it was given. Mean usage was about 1.66 cores for two requested cores. At concurrency 4 the output rate was only 3.6 tokens/s.

**Hypothesis.** With `VLLM_CPU_OMP_THREADS_BIND=auto`, the worker effectively computed on one core.

**Method.**

- Three candidate configurations were compared with the baseline. Each changed **exactly one** parameter.
- Every configuration ran on the same two dedicated nodes, with the same resources and the same fixed workload:
  - 3 cold-prefix requests;
  - 2 warm-up requests;
  - 12 measured requests at each of concurrency 1, 2 and 4 (short chats, long ~1.6k-token prompts, repeated and unique 600-token prefixes, streamed tool calls).
- A guard process watched the MLOps workloads on the other nodes throughout.

| Configuration | Single change |
|---|---|
| Baseline | `VLLM_CPU_OMP_THREADS_BIND=auto`, prefix caching on |
| Thread binding | `VLLM_CPU_OMP_THREADS_BIND=0-1` |
| Smaller prefill batches | `--max-num-batched-tokens 512` |
| No prefix cache | `--no-enable-prefix-caching` |

**Results at concurrency 4**, relative to the baseline:

| Configuration | Output tokens/s | TTFT p50 | End-to-end p95 | Errors |
|---|---|---|---|---|
| **Thread binding** | **+51.4 %** (3.62 → 5.48) | **−37.4 %** (12.9 s → 8.1 s) | **−34.7 %** | 0 / 36 |
| Smaller prefill batches | −0.5 % | +23.8 % | +0.2 % | 1 / 36 |
| No prefix cache | −100 % (all requests timed out) | n/a | n/a | 22 / 36 |

**Interpretation.**

- **Thread binding** raised mean vLLM CPU from about 1.66 to 2.65 cores, which confirms the hypothesis. A cold 800-token prefill halved, from 84.8 s to 43.8 s. The cost is mild CPU throttling (4.5 % of CFS periods), because the pod now uses its 3-core limit.
- **Smaller prefill batches** add scheduling steps but free no compute on a single effective core, so nothing improves.
- **Removing the prefix cache** makes every request pay a full prefill (about 87 s for 800 tokens, against 4.4 s with a warm cache). Prefix caching is essential on CPU.
- MLOps workloads were unaffected in every run.

The automated selection script reported "no gain", because one baseline request timed out and its rule disqualifies all candidates whenever the baseline has an error. The thread-binding configuration met every recorded qualification criterion and was selected after manual review. The evaluator output is preserved unchanged with the raw results.

**Raw data:**

- [`o1_20261101T052939Z/o1/comparison.md`](../../evidence/llm_agent/o1_20261101T052939Z/o1/comparison.md) and per-request `raw_requests.jsonl`;
- full experiment record: [vllm_cpu_o1_benchmark.md](vllm_cpu_o1_benchmark.md).

### 5.3 Experiment 2: cache-aware routing with two replicas

**Question.** With two replicas, a plain Kubernetes Service balances per connection. The question was whether routing each request with the llm-d Endpoint Picker (EPP), which scores pods by queue depth, KV-cache use and prefix-cache locality, improves throughput.

**Method.**

- Two thread-bound replicas, the same gateway and the same workload. The workload was extended to concurrency 8, with 12 requests per level.
- Service routing ran first, then EPP routing, in one session.
- Fairness checks (identical prompts, identical output token totals, valid tool calls) were fixed before the runs.

| Concurrency | Throughput, Service → EPP | TTFT p50 | End-to-end p95 | Prefix-cache hit ratio |
|---:|---|---|---|---|
| 1 | 2.23 → 1.99 tok/s (−10.7 %) | +36.7 % | +11.5 % | 0.43 → 0.41 |
| 2 | 5.94 → 5.26 tok/s (−11.5 %) | +27.9 % | −21.4 % | 0.66 → 0.89 |
| 4 | 7.28 → 9.28 tok/s (**+27.5 %**) | **−33.9 %** | **−15.7 %** | 0.74 → 0.89 |
| 8 | 6.77 → 9.87 tok/s (**+45.9 %**) | **−23.4 %** | **−27.3 %** | 0.87 → 0.89 |

All 96 requests succeeded in both runs.

**Interpretation.**

- **Higher concurrency.** The EPP keeps requests that share a prompt prefix on the pod that already holds it. At concurrency 4 and 8 this cuts queueing and prefill work: mean vLLM queue time at concurrency 8 fell by 68 %.
- **Lower concurrency.** At concurrency 1 and 2 the extra router hop and its decisions cost more than they save.
- **Verdict.** The pre-registered verdict is *mixed*.
- **Not in production.** The EPP was installed only for this experiment and is not part of the current deployment.

**Raw data:**

- [`g2_20261101T124444Z/g2/comparison.json`](../../evidence/llm_agent/g2_20261101T124444Z/g2/comparison.json);
- full experiment record: [llmd_g2_routing_benchmark.md](llmd_g2_routing_benchmark.md).

### 5.4 Production telemetry

Under real agent traffic the deployed server reports:

- 707 / 264 / 971 input / output / total tokens per request;
- TTFT p50 / p95 of 5 s / 38 s;
- end-to-end generation time p50 / p95 of 50 s / 114 s.

These are longer than the benchmark figures, because agent prompts (about 700 tokens, including the tool schema) are much longer than the benchmark's short chats. See [Observability §6](../observability/OBSERVABILITY.md#6-llm-telemetry).

![Tokens per request](../../evidence/edai2/screenshots/llm/02_grafana_tokens_per_request.png)

*Grafana `llm-runtime` dashboard: tokens per request, measured both by vLLM and by agentgateway.*

## 6. Limitations

- **CPU latency.** Serving on CPU gives first-token latencies of seconds and generations of tens of seconds. This is acceptable for an analyst assistant, but not for interactive chat at scale.
- **Fallback.** The fallback replica is scaled to zero in the current deployment. Failover is configured and was demonstrated, but it is not available until the fallback is scaled up.
- **Model versions.** Only one model version is served. Comparing model versions, or agent model configurations, side by side has not been done.
