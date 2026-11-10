# EXP-V1: b0-prime vs c1-thread-bind

**Verdict:** `IMPROVED`

- output_tokens_per_second better at 1, 2, 4

Runs: 3 baseline, 3 optimized. Variable: VLLM_CPU_OMP_THREADS_BIND: auto versus 0-1. Positive improvement means better.

| Level | Baseline failed | Optimized failed |
|---|---:|---:|
| 1 | 0/36 | 0/36 |
| 2 | 0/36 | 0/36 |
| 4 | 0/36 | 0/36 |

| Level | Metric | Better when | Baseline | Optimized | Improvement | Within spread |
|---|---|---|---:|---:|---:|---|
| 1 | ttft_p50_ms | lower | 7,707.148 | 4,194.293 | +45.6% | False |
| 1 | ttft_p95_ms | lower | 100,036.413 | 50,220.759 | +49.8% | False |
| 1 | e2e_p50_ms | lower | 14,602.317 | 8,517.801 | +41.7% | False |
| 1 | e2e_p95_ms | lower | 116,261.389 | 60,541.678 | +47.9% | False |
| 1 | output_tokens_per_second | higher | 1.492 | 2.786 | +86.7% | False |
| 1 | request_throughput | higher | 0.025 | 0.047 | +86.7% | False |
| 1 | tpot_ms | lower | 164.754 | 100.442 | +39.0% | False |
| 1 | cpu_cores_mean | lower | 1.231 | 2.243 | -82.1% | False |
| 1 | memory_mib_peak | lower | 6,500.000 | 6,465.000 | +0.5% | True |
| 2 | ttft_p50_ms | lower | 8,447.609 | 4,537.178 | +46.3% | False |
| 2 | ttft_p95_ms | lower | 13,420.774 | 7,169.072 | +46.6% | False |
| 2 | e2e_p50_ms | lower | 28,971.486 | 16,464.449 | +43.2% | False |
| 2 | e2e_p95_ms | lower | 56,873.050 | 33,383.486 | +41.3% | False |
| 2 | output_tokens_per_second | higher | 4.101 | 7.108 | +73.3% | False |
| 2 | request_throughput | higher | 0.069 | 0.120 | +73.3% | False |
| 2 | tpot_ms | lower | 335.848 | 214.749 | +36.1% | False |
| 2 | cpu_cores_mean | lower | 1.650 | 2.599 | -57.5% | False |
| 2 | memory_mib_peak | lower | 6,500.000 | 6,461.000 | +0.6% | True |
| 4 | ttft_p50_ms | lower | 11,570.891 | 6,398.749 | +44.7% | False |
| 4 | ttft_p95_ms | lower | 20,048.226 | 10,513.695 | +47.6% | False |
| 4 | e2e_p50_ms | lower | 50,223.581 | 28,729.154 | +42.8% | False |
| 4 | e2e_p95_ms | lower | 128,649.998 | 72,035.871 | +44.0% | False |
| 4 | output_tokens_per_second | higher | 4.257 | 7.507 | +76.4% | False |
| 4 | request_throughput | higher | 0.072 | 0.126 | +76.4% | False |
| 4 | tpot_ms | lower | 686.737 | 404.708 | +41.1% | False |
| 4 | cpu_cores_mean | lower | 1.636 | 2.646 | -61.7% | False |
| 4 | memory_mib_peak | lower | 6,500.000 | 6,461.000 | +0.6% | True |
