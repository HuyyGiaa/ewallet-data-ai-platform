# EXP-V1 SINGLE-RUN comparison: b0-prime vs c1-thread-bind

**Methodology:** `single-run` (EXP_PROFILE=single): 1 baseline run, 1 optimized run. One run per arm: the percentages are observed differences between two runs. There is no repetition, no confidence interval and no significance test, so this is not a statistically proven or stable improvement.

**Execution validity:** VALID

**Observed outcome:** `OBSERVED_IMPROVEMENT`
- output_tokens_per_second observed better by >= 10 % at 1, 2, 4

Primary metric: `output_tokens_per_second`. A level counts as changed when the observed difference is at least 10 % (a fixed reporting rule, not a statistical test). Variable: VLLM_CPU_OMP_THREADS_BIND: auto versus 0-1. Positive change means better.

| Level | Baseline failed | Optimized failed | Baseline timeout rate | Optimized timeout rate |
|---|---:|---:|---:|---:|
| 1 | 1/12 | 0/12 | 0.000 | 0.000 |
| 2 | 0/12 | 0/12 | 0.000 | 0.000 |
| 4 | 0/12 | 0/12 | 0.000 | 0.000 |

| Level | Metric | Better when | Baseline | Optimized | Observed change |
|---|---|---|---:|---:|---:|
| 1 | ttft_p50_ms | lower | 7,874.145 | 5,020.781 | +36.2% |
| 1 | ttft_p95_ms | lower | 63,021.493 | 62,328.854 | +1.1% |
| 1 | e2e_p50_ms | lower | 13,617.900 | 11,163.162 | +18.0% |
| 1 | e2e_p95_ms | lower | 76,544.062 | 76,820.170 | -0.4% |
| 1 | output_tokens_per_second | higher | 1.083 | 2.176 | +100.9% |
| 1 | request_throughput | higher | 0.022 | 0.037 | +64.8% |
| 1 | tpot_ms | lower | 211.287 | 143.368 | +32.1% |
| 1 | cpu_cores_mean | lower | 1.201 | 2.279 | -89.8% |
| 1 | memory_mib_peak | lower | 6,469.000 | 6,629.000 | -2.5% |
| 1 | itl_p50_ms | lower | 205.252 | 159.559 | +22.3% |
| 1 | error_rate | lower | 0.083 | 0.000 | +100.0% |
| 1 | timeout_rate | lower | 0.000 | 0.000 | +0.0% |
| 1 | tool_calls_valid | higher | 2.000 | 2.000 | +0.0% |
| 1 | tokens_per_second_per_allocated_cpu | higher | 0.542 | 1.088 | +100.9% |
| 1 | tokens_per_second_per_used_cpu_core | higher | 0.902 | 0.955 | +5.8% |
| 2 | ttft_p50_ms | lower | 9,746.849 | 5,480.598 | +43.8% |
| 2 | ttft_p95_ms | lower | 15,515.051 | 8,966.588 | +42.2% |
| 2 | e2e_p50_ms | lower | 35,365.692 | 22,876.580 | +35.3% |
| 2 | e2e_p95_ms | lower | 70,132.779 | 46,531.668 | +33.7% |
| 2 | output_tokens_per_second | higher | 3.357 | 5.127 | +52.7% |
| 2 | request_throughput | higher | 0.057 | 0.086 | +52.7% |
| 2 | tpot_ms | lower | 448.398 | 301.188 | +32.8% |
| 2 | cpu_cores_mean | lower | 1.709 | 2.641 | -54.5% |
| 2 | memory_mib_peak | lower | 6,458.000 | 6,625.000 | -2.6% |
| 2 | itl_p50_ms | lower | 397.797 | 270.744 | +31.9% |
| 2 | error_rate | lower | 0.000 | 0.000 | +0.0% |
| 2 | timeout_rate | lower | 0.000 | 0.000 | +0.0% |
| 2 | tool_calls_valid | higher | 2.000 | 2.000 | +0.0% |
| 2 | tokens_per_second_per_allocated_cpu | higher | 1.679 | 2.563 | +52.7% |
| 2 | tokens_per_second_per_used_cpu_core | higher | 1.964 | 1.942 | -1.2% |
| 4 | ttft_p50_ms | lower | 17,297.308 | 10,003.675 | +42.2% |
| 4 | ttft_p95_ms | lower | 24,244.183 | 13,435.219 | +44.6% |
| 4 | e2e_p50_ms | lower | 61,220.393 | 40,911.786 | +33.2% |
| 4 | e2e_p95_ms | lower | 154,745.703 | 98,511.020 | +36.3% |
| 4 | output_tokens_per_second | higher | 3.543 | 5.475 | +54.5% |
| 4 | request_throughput | higher | 0.060 | 0.092 | +54.5% |
| 4 | tpot_ms | lower | 810.514 | 544.928 | +32.8% |
| 4 | cpu_cores_mean | lower | 1.639 | 2.584 | -57.6% |
| 4 | memory_mib_peak | lower | 6,459.000 | 6,625.000 | -2.6% |
| 4 | itl_p50_ms | lower | 709.123 | 511.015 | +27.9% |
| 4 | error_rate | lower | 0.000 | 0.000 | +0.0% |
| 4 | timeout_rate | lower | 0.000 | 0.000 | +0.0% |
| 4 | tool_calls_valid | higher | 2.000 | 2.000 | +0.0% |
| 4 | tokens_per_second_per_allocated_cpu | higher | 1.772 | 2.737 | +54.5% |
| 4 | tokens_per_second_per_used_cpu_core | higher | 2.161 | 2.119 | -2.0% |
