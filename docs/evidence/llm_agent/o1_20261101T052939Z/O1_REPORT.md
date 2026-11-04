# O1 results — `o1_20261101T052939Z`

Selected: **NO_GAIN** (baseline valid: False; issues: ['level 1: errors 8%, timeouts 0%'])

## Capacity (safe budget per node, after headroom)

| Node | Role | Allocatable CPU / GiB | Requested CPU / GiB | Safe CPU / GiB |
|---|---|---|---|---|
| gke-ewallet-dev-cpu-pool-58daed59-08s1 | MLOps | 3.92 / 12.96 | 2.36 / 5.77 | 1.26 / 6.44 |
| gke-ewallet-dev-cpu-pool-58daed59-4z9k | O1 | 3.92 / 12.96 | 0.83 / 1.48 | 2.79 / 10.73 |
| gke-ewallet-dev-cpu-pool-58daed59-9gpp | O1 | 3.92 / 12.96 | 0.83 / 1.48 | 2.79 / 10.73 |
| gke-ewallet-dev-cpu-pool-58daed59-mb8v | MLOps | 3.92 / 12.96 | 2.21 / 4.85 | 1.41 / 7.36 |

Verdict: FITS_SAFE_BUDGET; placement: vllm-fallback→gke-ewallet-dev-cpu-pool-58daed59-4z9k, vllm-primary→gke-ewallet-dev-cpu-pool-58daed59-9gpp, agentgateway→gke-ewallet-dev-cpu-pool-58daed59-4z9k, agentgateway→gke-ewallet-dev-cpu-pool-58daed59-9gpp

## Matched benchmark (same workload, resources, replicas)

| Config | Level | TTFT p50/p95 (ms) | ITL p50/p95 (ms) | TPOT p50 (ms) | E2E p50/p95 (ms) | req/s | out tok/s | tok/s per CPU | queue mean (ms) | prefix hit | errors | vLLM CPU mean/peak (m) | vLLM RAM peak (MiB) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| b0-prime | 1 | 7810/61393 | 205/294 | 204 | 13282/74456 | 0.023 | 1.10 | 0.55 | 0 | 0.43 | 8% | 1238/1768 | 6197 |
| b0-prime | 2 | 8886/11563 | 389/511 | 463 | 34050/69608 | 0.058 | 3.44 | 1.72 | 0 | 0.89 | 0% | 1660/1806 | 6197 |
| b0-prime | 4 | 12870/23424 | 699/822 | 817 | 59184/151065 | 0.061 | 3.62 | 1.81 | 0 | 0.89 | 0% | 1663/1799 | 6196 |
| c1-thread-bind | 1 | 4918/60265 | 154/251 | 146 | 11161/74732 | 0.037 | 2.21 | 1.10 | 0 | 0.43 | 0% | 2312/2696 | 6210 |
| c1-thread-bind | 2 | 5261/7495 | 274/352 | 318 | 22561/46602 | 0.087 | 5.18 | 2.59 | 0 | 0.89 | 0% | 2676/2764 | 6206 |
| c1-thread-bind | 4 | 8058/13183 | 511/614 | 569 | 39800/98717 | 0.092 | 5.48 | 2.74 | 0 | 0.89 | 0% | 2654/2834 | 6207 |
| c2-batched-tokens | 1 | 7685/61313 | 211/307 | 212 | 13620/74906 | 0.023 | 1.13 | 0.57 | 0 | 0.43 | 8% | 1295/2007 | 6191 |
| c2-batched-tokens | 2 | 9449/17985 | 395/511 | 441 | 34818/69350 | 0.056 | 3.31 | 1.65 | 0 | 0.89 | 0% | 1646/2005 | 6189 |
| c2-batched-tokens | 4 | 15931/23349 | 709/815 | 800 | 60860/151337 | 0.061 | 3.60 | 1.80 | 0 | 0.89 | 0% | 1645/1891 | 6189 |
| c3-no-prefix-cache | 1 | 40994/96694 | 209/307 | 215 | 46051/110719 | 0.014 | 0.54 | 0.27 | 1 | - | 17% | 1125/1670 | 6203 |
| c3-no-prefix-cache | 2 | 57582/104422 | 337/429 | 235 | 61272/110531 | 0.015 | 0.15 | 0.08 | 0 | - | 67% | 1047/1511 | 6197 |
| c3-no-prefix-cache | 4 | -/- | -/- | - | -/- | 0.025 | 0.00 | 0.00 | - | - | 100% | 1047/1354 | 6194 |

## Cold vs warm prefix (TTFT ms of 3 requests sharing a new prefix)

| Config | 1st (cold) | 2nd | 3rd |
|---|---|---|---|
| b0-prime | 84758 | 4476 | 4244 |
| c1-thread-bind | 43839 | 2647 | 2670 |
| c2-batched-tokens | 85092 | 4447 | 4288 |
| c3-no-prefix-cache | 87788 | 88258 | 87460 |

## Candidates against the selection rules

- **c1-thread-bind**: qualifies=True; tokens/s +51.4 %, TTFT p50 -37.4 %, E2E p95 -34.7 % (level 4); reasons: none
- **c2-batched-tokens**: qualifies=False; tokens/s -0.5 %, TTFT p50 +23.8 %, E2E p95 +0.2 % (level 4); reasons: ['level 1: errors 8%, timeouts 0%', 'no defensible gain at level 4 (tokens/s -0%, TTFT p50 +24%)']
- **c3-no-prefix-cache**: qualifies=False; tokens/s -100.0 %, TTFT p50 -, E2E p95 - (level 4); reasons: ['level 1: errors 17%, timeouts 0%', 'level 1: E2E p95 +49%', 'level 2: errors 67%, timeouts 0%', 'level 2: E2E p95 +59%', 'level 4: errors 100%, timeouts 0%', 'level 4: fewer valid tool calls', 'no defensible gain at level 4 (tokens/s -100%, TTFT p50 +0%)']

## Resource isolation and MLOps coexistence (per config window)

| Config | Guard samples | MLOps regressions | Guard stop | vLLM restarts | CPU throttled periods | MLOps node CPU / mem peak % | MLOps pods CPU peak (m) | Gateway CPU peak (m) |
|---|---|---|---|---|---|---|---|---|
| b0-prime | 54 | 0 | no | 0 | 0/10746 | 10 / 44 | 127 | 4 |
| c1-thread-bind | 34 | 0 | no | 0 | 304/6709 | 10 / 44 | 137 | 3 |
| c2-batched-tokens | 54 | 0 | no | 0 | 0/10726 | 10 / 44 | 124 | 4 |
| c3-no-prefix-cache | 123 | 0 | no | 0 | 0/24158 | 10 / 44 | 127 | 3 |

- MLOps health before: healthy=True, pods=24, InferenceServices={'fraud-serving/fraud-detector': True}
- MLOps health during: healthy=True, pods=24, InferenceServices={'fraud-serving/fraud-detector': True}
- MLOps health after: healthy=True, pods=24, InferenceServices={'fraud-serving/fraud-detector': True}

## Cleanup readback

```
pool_size=2
nodes_after=gke-ewallet-dev-cpu-pool-58daed59-08s1 gke-ewallet-dev-cpu-pool-58daed59-mb8v 
mlops_nodes_before=gke-ewallet-dev-cpu-pool-58daed59-08s1 gke-ewallet-dev-cpu-pool-58daed59-mb8v
```

## Raw evidence

- per request: `o1/<config>/raw_requests.jsonl`
- per level summary: `o1/<config>/summary.json`; window: `o1/<config>/window.json`
- guard samples: `state/resources.jsonl`; workload: `o1/<config>/workload.json`
- G1 gates on the kept config: `ops/`
