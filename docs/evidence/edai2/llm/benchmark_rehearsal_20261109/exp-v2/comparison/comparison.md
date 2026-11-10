# EXP-V2: k8s-service vs llmd-epp

**Verdict:** `NO_SIGNIFICANT_CHANGE`

- no level changed beyond the threshold and the run-to-run spread

Runs: 3 baseline, 3 optimized. Variable: Routing path: Kubernetes Service versus llm-d EPP. Positive improvement means better.

| Level | Baseline failed | Optimized failed |
|---|---:|---:|
| 1 | 0/36 | 0/36 |
| 2 | 0/36 | 0/36 |
| 4 | 0/36 | 0/36 |
| 8 | 0/36 | 0/36 |

| Level | Metric | Better when | Baseline | Optimized | Improvement | Within spread |
|---|---|---|---:|---:|---:|---|
| 1 | ttft_p50_ms | lower | 4,191.229 | 4,882.483 | -16.5% | True |
| 1 | ttft_p95_ms | lower | 51,404.526 | 51,504.534 | -0.2% | True |
| 1 | e2e_p50_ms | lower | 8,373.162 | 11,169.421 | -33.4% | True |
| 1 | e2e_p95_ms | lower | 61,949.853 | 63,226.511 | -2.1% | True |
| 1 | output_tokens_per_second | higher | 2.737 | 2.654 | -3.0% | True |
| 1 | request_throughput | higher | 0.046 | 0.045 | -3.0% | True |
| 1 | tpot_ms | lower | 100.460 | 111.078 | -10.6% | True |
| 1 | cpu_cores_mean | lower | 2.182 | 2.498 | -14.5% | False |
| 1 | memory_mib_peak | lower | 12,425.000 | 13,033.000 | -4.9% | True |
| 1 | prefix_cache_hit_ratio | higher | 0.427 | 0.408 | -4.5% | True |
| 1 | queue_time_ms | lower | 0.401 | 0.556 | -38.5% | True |
| 2 | ttft_p50_ms | lower | 4,180.996 | 4,440.473 | -6.2% | True |
| 2 | ttft_p95_ms | lower | 36,901.022 | 7,424.397 | +79.9% | True |
| 2 | e2e_p50_ms | lower | 16,368.115 | 11,607.709 | +29.1% | True |
| 2 | e2e_p95_ms | lower | 50,769.720 | 39,207.766 | +22.8% | True |
| 2 | output_tokens_per_second | higher | 6.983 | 7.609 | +9.0% | True |
| 2 | request_throughput | higher | 0.118 | 0.128 | +9.0% | True |
| 2 | tpot_ms | lower | 197.819 | 140.651 | +28.9% | True |
| 2 | cpu_cores_mean | lower | 2.329 | 2.859 | -22.8% | True |
| 2 | memory_mib_peak | lower | 12,421.000 | 12,747.000 | -2.6% | True |
| 2 | prefix_cache_hit_ratio | higher | 0.661 | 0.894 | +35.3% | True |
| 2 | queue_time_ms | lower | 0.038 | 0.031 | +18.7% | True |
| 4 | ttft_p50_ms | lower | 5,023.457 | 4,624.830 | +7.9% | False |
| 4 | ttft_p95_ms | lower | 17,343.125 | 10,007.313 | +42.3% | True |
| 4 | e2e_p50_ms | lower | 16,131.728 | 16,602.254 | -2.9% | True |
| 4 | e2e_p95_ms | lower | 67,990.450 | 44,932.402 | +33.9% | True |
| 4 | output_tokens_per_second | higher | 7.988 | 12.025 | +50.5% | True |
| 4 | request_throughput | higher | 0.134 | 0.202 | +50.5% | True |
| 4 | tpot_ms | lower | 194.896 | 225.255 | -15.6% | True |
| 4 | cpu_cores_mean | lower | 3.698 | 3.590 | +2.9% | True |
| 4 | memory_mib_peak | lower | 12,363.000 | 12,747.000 | -3.1% | True |
| 4 | prefix_cache_hit_ratio | higher | 0.797 | 0.894 | +12.2% | True |
| 4 | queue_time_ms | lower | 0.019 | 0.207 | -1011.0% | True |
| 8 | ttft_p50_ms | lower | 12,900.116 | 11,265.774 | +12.7% | True |
| 8 | ttft_p95_ms | lower | 30,667.235 | 22,923.016 | +25.3% | True |
| 8 | e2e_p50_ms | lower | 39,280.201 | 32,396.124 | +17.5% | True |
| 8 | e2e_p95_ms | lower | 66,128.306 | 56,889.481 | +14.0% | True |
| 8 | output_tokens_per_second | higher | 10.773 | 12.527 | +16.3% | True |
| 8 | request_throughput | higher | 0.181 | 0.211 | +16.3% | True |
| 8 | tpot_ms | lower | 416.505 | 367.173 | +11.8% | True |
| 8 | cpu_cores_mean | lower | 3.216 | 4.114 | -27.9% | True |
| 8 | memory_mib_peak | lower | 12,362.000 | 12,746.000 | -3.1% | True |
| 8 | prefix_cache_hit_ratio | higher | 0.816 | 0.894 | +9.5% | True |
| 8 | queue_time_ms | lower | 3,385.081 | 2,379.847 | +29.7% | True |
