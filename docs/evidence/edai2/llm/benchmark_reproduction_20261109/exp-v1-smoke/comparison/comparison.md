# EXP-V1: b0-prime vs c1-thread-bind

**Verdict:** `INCONCLUSIVE`

- needs 3 runs per arm, has 1 and 1

Runs: 1 baseline, 1 optimized. Variable: VLLM_CPU_OMP_THREADS_BIND: auto versus 0-1. Positive improvement means better.

| Level | Baseline failed | Optimized failed |
|---|---:|---:|
| 1 | 1/12 | 0/12 |

| Level | Metric | Better when | Baseline | Optimized | Improvement | Within spread |
|---|---|---|---:|---:|---:|---|
| 1 | ttft_p50_ms | lower | 7,126.903 | 4,487.898 | +37.0% | False |
| 1 | ttft_p95_ms | lower | 54,926.579 | 52,757.295 | +3.9% | False |
| 1 | e2e_p50_ms | lower | 14,847.694 | 10,647.450 | +28.3% | False |
| 1 | e2e_p95_ms | lower | 65,707.342 | 65,286.034 | +0.6% | False |
| 1 | output_tokens_per_second | higher | 1.217 | 2.485 | +104.1% | False |
| 1 | request_throughput | higher | 0.025 | 0.042 | +67.5% | False |
| 1 | tpot_ms | lower | 170.129 | 117.508 | +30.9% | False |
| 1 | cpu_cores_mean | lower | 1.198 | 2.256 | -88.3% | False |
| 1 | memory_mib_peak | lower | 6,293.000 | 6,123.000 | +2.7% | False |
