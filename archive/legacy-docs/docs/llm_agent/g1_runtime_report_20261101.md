# G1 runtime report: CPU vLLM + agentgateway on GKE (2026-10-31 / 11-01 UTC)

Cluster `ewallet-dev` (asia-southeast1-b), `cpu-pool` e2-standard-4, shared
with the MLOps workloads (KFP, MLflow, Feast Redis, KServe `fraud-detector`,
`fraud-api`), which stayed up except during the Calico enablement window.
Runbook and operator: `docs/llm_agent/gke_cpu_runtime.md`,
`scripts/llm_agent/g1_operator.sh`.

## Result (run 6, `docs/evidence/llm_agent/g1_20261031T231145Z`, git 563e699 / 980e5bf)

| Check | Result | Evidence |
|---|---|---|
| NetworkPolicy (Calico) enforced | `networkPolicy.enabled: true`, CALICO | `g1_20261031T201629Z/networkpolicy.json` |
| Image | `vllm-cpu-model@sha256:f695b23e…`; Trivy vuln + secret gate: 0 / 0 | `g1_20261031T212736Z/image-digests.json`, `scans/` |
| Istio ambient 1.30.7 | CNI bin dir `/home/kubernetes/bin` read back; cni + ztunnel rolled out | `istio/` |
| vLLM Qwen3-0.6B CPU, primary + fallback | Ready after a real tool-shaped warm-up | `deploy/`, `vllm-ready.txt`, `image-ids.txt` |
| Allow/deny matrix | 6/6 rows as expected (denies are curl network errors, not pod failures) | `netpol-verify.tsv` |
| Direct vLLM tool call | stream + non-stream PASS, valid `feature_lookup` tool_calls; wrong / missing key 401 | `smoke-direct-*.json`, `smoke-direct-auth.txt` |
| Through agentgateway | stream + non-stream PASS (`x-ewallet-backend: primary`); wrong, missing and backend key 401 | `smoke-gateway-*.json`, `smoke-gateway-auth.txt` |
| Failover | primary scaled to 0: probe 1 served by fallback; smoke PASS via fallback; fallback `vllm:request_success_total` 1 -> 6 | `failover/` |
| Cleanup | release uninstalled, pool back to 2 nodes, MLOps healthy | `cleanup.txt` |

## Baseline benchmark (`benchmark.json`)

8 streamed requests, concurrency 1, max 128 tokens, through agentgateway,
primary backend, on nodes shared with MLOps.

| Metric | Value |
|---|---|
| TTFT p50 / p95 | 3.48 s / 3.69 s |
| ITL mean / p95 | 206 ms / 306 ms |
| E2E latency p50 / p95 / p99 | 11.7 s / 12.0 s / 12.1 s |
| Output throughput | 3.48 tokens/s, 0.085 requests/s |
| vLLM pods CPU mean / peak (sum) | 1.51 / 1.88 cores |
| vLLM pods memory (sum of 2) | ~12.2 GiB (~6.1 GiB each, limit 10 GiB) |
| Errors / fallback | 0 % / 0 % |

## Defects found by running it (each fixed, tested and committed)

| Run | Failure | Root cause | Fix |
|---|---|---|---|
| 1 | rollout "exceeded its progress deadline" | default 600 s < pull + warm-up | `progressDeadlineSeconds: 1800` (885a88b) |
| 2 | vLLM CrashLoop, path treated as HF repo id | `COPY --chmod=0444` made model dirs untraversable with capabilities dropped | dirs 0555, capless read check in build (fb8cab8) |
| 3 | netpol rows "deny" but curl never ran | non-numeric image user + runAsNonRoot | uid 100, only curl network codes count as deny (2097059) |
| 3 | external DNS failed (rc=6) | NodeLocal DNSCache answers on the kube-dns ClusterIP, never DNATed | allow 34.118.224.10/32:53 (cec626d) |
| 4 | connection dropped mid tool call | vLLM primary OOMKilled at 8 Gi (4 GiB KV) | KV 2 GiB, memory 6/10 Gi (8ed5b94) |
| 5 | gateway accepted a wrong key | no client authentication (backend key swapped in) | apiKey strict + separate client key (563e699) |
| 5 | first real request 190 s | warm-up prompt too small | tool-shaped warm-up (563e699) |
| 6 | failover 503 on first request | passive eviction after 2 failures | probe until fallback serves (980e5bf) |

Plus operator fixes: absolute Trivy report dir, refreshed registry login,
digests self-copy, deploy diagnostics, crashed-container logs.

## Cost and time

Calico enablement ~15 min (pool at 0). Six sessions with 4 nodes, about 3 h
of 4-node time in total (≈ 2 USD at 0.69 USD/h); pool returned to 2 nodes after
each. Final run: scale-up 1.5 min, Istio <1 min, deploy (pull + load + warm-up)
~19 min, verification + smoke ~3 min, failover ~20 min (primary re-warm-up),
benchmark ~19 min.

## Not covered by G1

ModelConfig readback (needs kagent CRDs, G3), llm-d routing (G2), agent and
MCP traffic, business E2E. The benchmark is a single-concurrency baseline on
shared nodes, not an optimisation result.
