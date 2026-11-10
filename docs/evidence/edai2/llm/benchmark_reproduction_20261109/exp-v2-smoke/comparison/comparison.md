# EXP-V2: k8s-service vs llmd-epp

**Verdict:** `INCONCLUSIVE`

- needs 3 runs per arm, has 1 and 1

Runs: 1 baseline, 1 optimized. Variable: Routing path: Kubernetes Service versus llm-d EPP. Positive improvement means better.

| Level | Baseline failed | Optimized failed |
|---|---:|---:|
| 1 | 0/12 | 0/12 |
| 4 | 0/12 | 0/12 |

| Level | Metric | Better when | Baseline | Optimized | Improvement | Within spread |
|---|---|---|---:|---:|---:|---|
| 1 | ttft_p50_ms | lower | 5,303.989 | 6,807.297 | -28.3% | False |
| 1 | ttft_p95_ms | lower | 61,856.555 | 65,477.261 | -5.9% | False |
| 1 | e2e_p50_ms | lower | 11,296.994 | 15,797.804 | -39.8% | False |
| 1 | e2e_p95_ms | lower | 77,280.204 | 82,246.877 | -6.4% | False |
| 1 | output_tokens_per_second | higher | 2.168 | 2.002 | -7.6% | False |
| 1 | request_throughput | higher | 0.036 | 0.034 | -7.6% | False |
| 1 | tpot_ms | lower | 143.361 | 161.010 | -12.3% | False |
| 1 | cpu_cores_mean | lower | 2.297 | 2.621 | -14.1% | False |
| 1 | memory_mib_peak | lower | 12,824.000 | 12,876.000 | -0.4% | False |
| 1 | prefix_cache_hit_ratio | higher | 0.427 | 0.408 | -4.5% | False |
| 1 | queue_time_ms | lower | 0.449 | 0.522 | -16.3% | False |
| 4 | ttft_p50_ms | lower | 5,379.008 | 6,553.224 | -21.8% | False |
| 4 | ttft_p95_ms | lower | 83,597.941 | 13,115.068 | +84.3% | False |
| 4 | e2e_p50_ms | lower | 14,495.696 | 24,631.332 | -69.9% | False |
| 4 | e2e_p95_ms | lower | 117,522.775 | 102,307.258 | +12.9% | False |
| 4 | output_tokens_per_second | higher | 5.701 | 6.201 | +8.8% | False |
| 4 | request_throughput | higher | 0.096 | 0.104 | +8.8% | False |
| 4 | tpot_ms | lower | 144.709 | 512.044 | -253.8% | False |
| 4 | cpu_cores_mean | lower | 3.960 | 2.965 | +25.1% | False |
| 4 | memory_mib_peak | lower | 12,789.000 | 12,873.000 | -0.7% | False |
| 4 | prefix_cache_hit_ratio | higher | 0.641 | 0.894 | +39.4% | False |
| 4 | queue_time_ms | lower | 0.029 | 0.024 | +17.1% | False |
