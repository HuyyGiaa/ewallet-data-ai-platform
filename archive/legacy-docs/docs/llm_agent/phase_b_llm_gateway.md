# Phase B CPU-first LLM and model gateway

Status: source-only implementation. No model image was pulled into the cluster,
no Kubernetes resource was applied and no benchmark result is claimed.

## Pinned runtime

- vLLM `0.32.0` CPU x86 image is pinned by OCI digest.
- The custom model is `Qwen/Qwen3-0.6B` at immutable Hugging Face revision
  `c1899de289a04d12100db370d81485cdf75e47ca`.
- agentgateway `1.7.0` is pinned by multi-architecture OCI digest.
- kagent remains `0.10.3`, Agent Substrate remains `0.0.9`, and the canonical
  ModelConfig API remains `kagent.dev/v1alpha2`.

The primary vLLM backend enables prefix caching and admits at most four running
sequences plus a bounded eight-request in-flight queue. The fallback backend
uses a conservative two/four bound without prefix caching. Both expose the
OpenAI-compatible API only through ClusterIP services.

## Routing and resilience

The global kagent ModelConfig addresses only `agentgateway:3000/v1` and requests
the public virtual model `ewallet-fraud-llm`. agentgateway maps that model to an
internal primary and fallback backend. The configured order is:

`30s request timeout → two total attempts → health eviction circuit → fallback`

The circuit opens after two consecutive unhealthy responses for 30 seconds.
Request and token token-buckets are keyed by requested model. vLLM separately
bounds active and queued requests. No direct ModelConfig-to-vLLM route exists.

## Benchmark evidence contract

`ai_platform.llm_agent.benchmark` rejects summaries without actual request,
resource and wall-clock measurements. A valid result includes end-to-end
p50/p95/p99, TTFT p50/p95/p99, ITL/TPOT, input/output/total tokens, output
tokens/s, requests/s, CPU, memory, error rate and fallback rate. Planned values
must not be serialized as actual measurements.

Runtime verification requires the same prompt corpus and model revision for the
baseline and optimized profiles, warm-up exclusion, raw request rows, pod
resource samples, immutable image/model/config identities, correctness checks
and a rollback exercise. Source tests and Helm rendering do not satisfy this
gate.

## Rollout and rollback gate

After explicit cloud approval, deploy with Helm `--atomic`, wait for both vLLM
Deployments and agentgateway, call `/v1/models` through agentgateway, run one
non-streaming and one streaming completion, then execute the controlled
benchmark. Preserve the previous Helm revision and image digests. Roll back by
Helm revision if readiness, correctness, latency/error budget or telemetry
readback fails. Do not retag mutable images or overwrite benchmark evidence.
