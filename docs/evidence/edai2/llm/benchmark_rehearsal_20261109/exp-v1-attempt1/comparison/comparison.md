# EXP-V1: b0-prime vs c1-thread-bind

**Verdict:** `INCONCLUSIVE`

- needs 3 runs per arm, has 1 and 2

Runs: 1 baseline, 2 optimized. Variable: VLLM_CPU_OMP_THREADS_BIND: auto versus 0-1. Positive improvement means better.

| Level | Baseline failed | Optimized failed |
|---|---:|---:|
| 1 | 0/12 | 0/24 |
| 2 | 0/12 | 0/24 |
| 4 | 0/12 | 0/24 |

| Level | Metric | Better when | Baseline | Optimized | Improvement | Within spread |
|---|---|---|---:|---:|---:|---|
| 1 | ttft_p50_ms | lower | 7,698.562 | 4,169.298 | +45.8% | False |
| 1 | ttft_p95_ms | lower | 100,418.630 | 50,734.910 | +49.5% | False |
| 1 | e2e_p50_ms | lower | 14,271.859 | 8,413.042 | +41.1% | False |
| 1 | e2e_p95_ms | lower | 117,469.287 | 61,927.226 | +47.3% | False |
| 1 | output_tokens_per_second | higher | 1.491 | 2.733 | +83.3% | False |
| 1 | request_throughput | higher | 0.025 | 0.046 | +83.3% | False |
| 1 | tpot_ms | lower | 160.982 | 99.772 | +38.0% | False |
| 1 | cpu_cores_mean | lower | 1.230 | 2.256 | -83.4% | False |
| 1 | memory_mib_peak | lower | 6,223.000 | 6,508.000 | -4.6% | False |
| 2 | ttft_p50_ms | lower | 7,892.829 | 4,492.907 | +43.1% | False |
| 2 | ttft_p95_ms | lower | 9,863.696 | 6,465.027 | +34.5% | False |
| 2 | e2e_p50_ms | lower | 28,671.959 | 16,374.329 | +42.9% | False |
| 2 | e2e_p95_ms | lower | 56,318.005 | 33,019.145 | +41.4% | False |
| 2 | output_tokens_per_second | higher | 4.128 | 7.188 | +74.2% | False |
| 2 | request_throughput | higher | 0.069 | 0.121 | +74.2% | False |
| 2 | tpot_ms | lower | 374.878 | 218.262 | +41.8% | False |
| 2 | cpu_cores_mean | lower | 1.642 | 2.622 | -59.7% | False |
| 2 | memory_mib_peak | lower | 6,212.000 | 6,504.500 | -4.7% | False |
| 4 | ttft_p50_ms | lower | 14,381.493 | 7,719.285 | +46.3% | False |
| 4 | ttft_p95_ms | lower | 20,223.409 | 10,889.144 | +46.2% | False |
| 4 | e2e_p50_ms | lower | 50,863.489 | 29,403.428 | +42.2% | False |
| 4 | e2e_p95_ms | lower | 128,890.071 | 71,815.480 | +44.3% | False |
| 4 | output_tokens_per_second | higher | 4.234 | 7.513 | +77.4% | False |
| 4 | request_throughput | higher | 0.071 | 0.126 | +77.4% | False |
| 4 | tpot_ms | lower | 670.261 | 381.749 | +43.0% | False |
| 4 | cpu_cores_mean | lower | 1.569 | 2.621 | -67.1% | False |
| 4 | memory_mib_peak | lower | 6,213.000 | 6,505.000 | -4.7% | False |
