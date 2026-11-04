# llm-d routing vs standard routing on two C1 CPU replicas (G2): benchmark report

| Item | Value |
|---|---|
| Status | Runtime complete, documentation closed. Verdict **`MIXED`**: not a gain, not a failure |
| Accepted run | attempt 3, [`g2_20261101T124444Z`](../../evidence/llm_agent/g2_20261101T124444Z), exit 0 |
| Runtime source SHA | `0c89af3b08561e4f9e58b7cc80273457ab6e4d0f` (the code that produced every number below) |
| Reporting commits | later commits on `codex/llm-g2-llmd-routing`; runner fixes, evidence and documentation only, see §13 |
| Branch base | O1 `9b47546` + G1/MLOps checkpoint `e7dbbc2` (merge `d26b9d1`) |
| Integration | not merged into canonical/main/I7; no post-merge reproduction |

## 1. Executive summary

G2 compares two ways of routing to the same two C1 vLLM CPU replicas, both
behind agentgateway, which does the API-key auth, rate limits and failover:
- **B2** routes through a Kubernetes Service;
- **O2** routes through the llm-d-router v1.0.0 Endpoint Picker (EPP).

Results:
- **Matched run:** both runs served 48/48 measured requests, with 0 errors,
  0 timeouts and valid tool calls. The fairness gate passed with no issues.
- **Concurrency 4 and 8:** O2 is better. Throughput is +27.46 % and
  +45.90 %, TTFT p50 is −33.85 % and −23.36 %, and E2E p95 is −15.70 % and
  −27.25 %. Both levels are above the best of three B2 runs.
- **Concurrency 1:** O2 is worse. Throughput is −10.74 % and TTFT p50 is
  +36.74 %; it is below all three B2 runs.
- **Concurrency 2:** the fixed rules classify a regression (−11.54 %
  throughput, +27.90 % TTFT p50). O2's throughput, however, lies inside the
  observed spread of B2 across attempts (§6.2).
- **Mechanism:** measured prefix-cache locality. The EPP keeps each prompt on
  the pod that holds its prefix, giving a prefix-cache hit ratio of 0.894 at
  levels 2–8, against 0.661–0.874 for B2. B2's connection-level balancing put
  all 12 requests on one pod at level 1 in every attempt.
- **Verdict:** `MIXED`, from rules fixed before any run. Gains at 4 and 8,
  regressions at 1 and 2.

## 2. System architecture

```
B2  client ─► agentgateway ─► Service vllm-primary (connection-level LB) ─► 2 × C1 vLLM
                           └► vllm-fallback (failover only)
O2  client ─► agentgateway ─► llmd-router-epp:8081 ─ Envoy ⇄ EPP (ext_proc) ─► 2 × C1 vLLM
                           ├► vllm-primary Service (failover priority 1)
                           └► vllm-fallback        (failover priority 2)
```

**Router.** llm-d-router v1.0.0 **standalone** chart. Its pins are in
[`versions.env`](../../../infra/helm/llm-d/versions.env):
- chart OCI `sha256:c1d1af55…024c`;
- EPP `…endpoint-picker:v1.0.0@sha256:024188e9…df2c`;
- Envoy `distroless-v1.33.2@sha256:85500e28…e4e8`.

Its values file is
[`standalone-values-g2.yaml`](../../../infra/helm/llm-d/standalone-values-g2.yaml):
- the EPP finds the pods with
  `--endpoint-selector app.kubernetes.io/instance=primary,app.kubernetes.io/name=vllm`;
- it scrapes vLLM `/metrics` every 50 ms (the default);
- it scores each request with `queue-scorer` (weight 2),
  `kv-cache-utilization-scorer` (2) and `prefix-cache-scorer` (3);
- the proxy fails closed;
- there are no CRDs and no cluster-scoped objects.

**Why not Gateway mode.** Gateway mode needs the Gateway API, GAIE
InferencePool and agentgateway-controller CRDs. None exist on `ewallet-dev`.
Installing the Gateway API CRDs would also activate Istio 1.30's shared Gateway
controller. That is the cluster-wide change the G2 prompt says to stop at.

**Chart switch.** The llm-agent chart selects the path with `llmd.mode=standalone`.
agentgateway then gets the router as its priority-0 target. The chart also adds
three least-privilege flows: agentgateway→router:8081, router→vLLM:8000 and
router→API server:443. The API server addresses were read back:
`10.20.0.8/32` and `34.118.224.1/32`.

## 3. Experimental setup

| Item | Value |
|---|---|
| Cluster | GKE `ewallet-dev` 1.36.4-gke.2046003, `asia-southeast1-b`, Istio ambient 1.30.7, Calico NetworkPolicy |
| Nodes | 2 MLOps nodes untouched, plus **3 temporary e2-standard-4** labelled `ewallet.io/g2-llm=true` (2 primaries and the fallback). One vLLM pod per node, by CPU request |
| Model | `Qwen/Qwen3-0.6B` @ `c1899de289a04d12100db370d81485cdf75e47ca`, baked into `vllm-cpu-model@sha256:f695b23e…d07c` |
| C1 per pod | requests 2 CPU / 6 GiB, limits 3 CPU / 10 GiB, KV 2 GiB, `OMP_NUM_THREADS=2`, `VLLM_CPU_OMP_THREADS_BIND=0-1`, prefix caching on, max-model-len 4096, `max-num-seqs 4`, hermes tool parser, qwen3 reasoning parser, thinking off |
| Gateway (session overlay [`g2/benchmark-gateway.yaml`](../../../infra/helm/llm-agent-platform/g2/benchmark-gateway.yaml), identical for B2 and O2) | 150 000 tokens/h (production 24 000/h), 12 requests/s, timeouts 180/170/160/160 s, passive eviction **300 s** (production 30 s) |
| Workload | O1's `o1-workload-v1`, sha256 `c4816ca1…695f`, imported unchanged |
| Workload contents | 3 cold-prefix requests, 2 warm-up requests, then **the same 12 prompts** at each level: 2 short, 2 long (~1 617 input tokens, 128 output), 3 repeated-prefix, 2 unique-prefix, 2 streamed tool calls, 1 non-streamed. Every level produced 713 output tokens in both runs |
| Levels | 1, 2, 4, 8, in that order; B2 run first, then O2, in one campaign |
| Correctness | a request counts only with HTTP 200, usage reported, more than 0 output tokens and valid tool calls |
| Measurement | client: TTFT, ITL, TPOT, E2E. Per pod: vLLM counters read by `kubectl exec` before and after every level. EPP: metrics and decision log. Guard: one sample every 15 s. `cpu.stat` per pod |

**Aggregation.** Each level's statistics cover its 12 requests:
- TTFT, E2E and TPOT percentiles are over successful requests, and ITL is over
  all inter-token gaps (`ai_platform.llm_agent.benchmark.percentile`, linear
  interpolation). With n = 12, p95 sits near the maximum.
- Throughput = output tokens ÷ the level's wall time.
- Prefix-cache hit ratio = Σ `vllm:prefix_cache_hits` ÷ Σ `vllm:prefix_cache_queries`
  over both pods, as deltas across the level.

## 4. Fairness validation

These criteria were fixed before the run, in
[`g2_preflight.py`](../../../scripts/llm_agent/g2_preflight.py) and
[`g2_compare.py`](../../../scripts/llm_agent/g2_compare.py). The live readback
is in [b2](../../evidence/llm_agent/g2_20261101T124444Z/g2/b2/serving.json) and
[o2](../../evidence/llm_agent/g2_20261101T124444Z/g2/o2/serving.json)
`serving.json`.

| Criterion | B2 | O2 | Result |
|---|---|---|---|
| vLLM image and resolved imageID | `…@sha256:f695b23e…d07c`, one imageID | same | match |
| Model revision | `c1899de2…7ca` (image path) | same | match |
| Container args and environment | identical; Secret refs by name only | identical | match |
| CPU/memory requests and limits | 2/3 CPU, 6/10 GiB | same | match |
| Replicas, distinct nodes | 2, yes (nodes `…336c`, `…z2n5`) | 2, yes (same two nodes) | match |
| Node class | e2-standard-4 | e2-standard-4 | match |
| Token / request policy | 150 000/h, 12/s | same | match |
| Workload digest, levels, warm-up, timeouts | `c4816ca1…`, 1/2/4/8 | same | match |
| Fresh pods (created after the failover gate began; served only the startup warm-up) | yes | yes | match |
| Every response from the path under test | 48/48 `backend=primary` | 48/48 `backend=llm-d` | 0 attribution mismatches |

Fairness gate output: `"fairness_issues": []`.

## 5. Benchmark results (attempt 3, all levels)

Relative change = (O2 − B2) / B2 × 100. For throughput, positive is better.
For latency, **negative is better**. Every value below was recomputed from
`summary.json`. The four rule metrics (throughput, TTFT p50, E2E p95, error
rate) match `comparison.json` exactly.

### 5.1 Concurrency 1

| Metric | B2 | O2 | Difference | Relative |
|---|---|---|---|---|
| Throughput (output tokens/s) | 2.229 | 1.990 | −0.239 | −10.74 % |
| Requests/s | 0.0375 | 0.0335 | −0.0040 | −10.74 % |
| TTFT p50 (ms) | 4 959 | 6 781 | +1 822 | +36.74 % |
| TTFT p95 (ms) | 59 256 | 65 399 | +6 143 | +10.37 % |
| ITL p50 (ms) | 158.8 | 168.6 | +9.8 | +6.18 % |
| TPOT p50 (ms) | 144.7 | 161.2 | +16.5 | +11.43 % |
| E2E p50 (ms) | 10 996 | 14 723 | +3 727 | +33.90 % |
| E2E p95 (ms) | 73 851 | 82 364 | +8 513 | +11.53 % |
| Requests completed | 12/12 | 12/12 | 0 | — |
| Error / timeout rate | 0 % / 0 % | 0 % / 0 % | 0 | — |
| Prefix-cache hit ratio | 0.427 | 0.408 | −0.019 | — |
| Requests per pod | 0 / 12 | 4 / 8 | — | — |

### 5.2 Concurrency 2

| Metric | B2 | O2 | Difference | Relative |
|---|---|---|---|---|
| Throughput (output tokens/s) | **5.943** | **5.258** | −0.686 | **−11.54 %** |
| Requests/s | 0.1000 | 0.0885 | −0.0115 | −11.54 % |
| TTFT p50 (ms) | **4 450** | **5 691** | +1 241 | **+27.90 %** |
| TTFT p95 (ms) | 45 300 | 9 603 | −35 697 | −78.80 % |
| ITL p50 (ms) | 150.5 | 283.2 | +132.7 | +88.19 % |
| TPOT p50 (ms) | 145.4 | 254.1 | +108.7 | +74.77 % |
| E2E p50 (ms) | **10 985** | **19 748** | +8 763 | **+79.77 %** |
| E2E p95 (ms) | **65 166** | **51 250** | −13 916 | **−21.35 %** |
| Requests completed | 12/12 | 12/12 | 0 | — |
| Error / timeout rate | 0 % / 0 % | 0 % / 0 % | 0 | — |
| Prefix-cache hit ratio | 0.661 | 0.894 | +0.233 | — |
| Requests per pod | 2 / 10 | 4 / 8 | — | — |

### 5.3 Concurrency 4

| Metric | B2 | O2 | Difference | Relative |
|---|---|---|---|---|
| Throughput (output tokens/s) | 7.280 | 9.279 | +1.999 | +27.46 % |
| Requests/s | 0.1225 | 0.1562 | +0.0337 | +27.46 % |
| TTFT p50 (ms) | 8 503 | 5 625 | −2 878 | −33.85 % |
| TTFT p95 (ms) | 31 916 | 10 238 | −21 678 | −67.92 % |
| ITL p50 (ms) | 388.3 | 293.8 | −94.5 | −24.33 % |
| TPOT p50 (ms) | 288.7 | 322.8 | +34.2 | +11.83 % |
| E2E p50 (ms) | 25 190 | 20 641 | −4 549 | −18.06 % |
| E2E p95 (ms) | 71 592 | 60 350 | −11 242 | −15.70 % |
| Requests completed | 12/12 | 12/12 | 0 | — |
| Error / timeout rate | 0 % / 0 % | 0 % / 0 % | 0 | — |
| Prefix-cache hit ratio | 0.738 | 0.894 | +0.155 | — |
| Requests per pod | 4 / 8 | 4 / 8 | — | — |

### 5.4 Concurrency 8

| Metric | B2 | O2 | Difference | Relative |
|---|---|---|---|---|
| Throughput (output tokens/s) | 6.767 | 9.873 | +3.106 | +45.90 % |
| Requests/s | 0.1139 | 0.1662 | +0.0523 | +45.90 % |
| TTFT p50 (ms) | 15 004 | 11 499 | −3 506 | −23.36 % |
| TTFT p95 (ms) | 61 827 | 35 911 | −25 915 | −41.92 % |
| ITL p50 (ms) | 474.6 | 468.4 | −6.2 | −1.30 % |
| TPOT p50 (ms) | 488.4 | 512.9 | +24.5 | +5.01 % |
| E2E p50 (ms) | 58 933 | 43 647 | −15 286 | −25.94 % |
| E2E p95 (ms) | 99 134 | 72 119 | −27 014 | −27.25 % |
| Mean vLLM queue time (ms, `request_queue_time` counters) | 15 738 | 5 071 | −10 667 | −67.78 % |
| Requests completed | 12/12 | 12/12 | 0 | — |
| Error / timeout rate | 0 % / 0 % | 0 % / 0 % | 0 | — |
| Prefix-cache hit ratio | 0.874 | 0.894 | +0.019 | — |
| Requests per pod | 3 / 9 | 4 / 8 | — | — |

**Measured elsewhere.** Mean queue time at levels 1–4 is below 0.5 ms in
both runs. Preemptions were 0 everywhere. Tool calls were valid 2/2 per level
in both runs.

**`NOT_AVAILABLE`:**
- TTFT p99;
- per-request pod assignment in B2 (only per-level pod counters exist);
- per-request EPP scorer values (not in the v=3 log);
- confidence intervals, since there is one O2 run.

### 5.5 Rule outcome per level ([comparison.json](../../evidence/llm_agent/g2_20261101T124444Z/g2/comparison.json), unmodified)

| Level | Gain rule | Regression triggers | Level result |
|---|---|---|---|
| 1 | no | tokens/s, TTFT p50, E2E p95 | regression |
| 2 | no | tokens/s, TTFT p50 | regression |
| 4 | **yes** | — | gain |
| 8 | **yes** | — | gain |

Gains and regressions both present, so the verdict is **`MIXED`**. The
thresholds are O1's (gain ±10 %, throughput loss ≤ 5 %, tail ≤ +10 %), fixed
before the run and not changed.

## 6. Per-concurrency analysis

### 6.1 Concurrency 1: a robust regression

O2 is worse than **every** B2 attempt:
- throughput 1.990 vs 2.198–2.247;
- TTFT p50 6 781 vs 4 895–5 025 ms.

Level 1 is the first pass over the 12 prompts, so locality cannot help yet.
B2 puts every request on one pod; that pod had also served the tool-shaped
warm-up. O2 spreads requests, and tool calls landed on the pod without the
tool schema prefix: `tool_call` TTFT p50 4 304 → 8 106 ms. O2's backend pods
also throttled more (§9). That contribution is not separated.

### 6.2 Concurrency 2: classified as a regression, throughput within observed B2 variability

**B2 across the three attempts.** These are the same deployment and workload.
Attempts 1–2 differ only in runner SHA and a 30 s eviction window, which
never acts when no request fails, and B2 had 0 errors:

| Metric (level 2) | B2 A1 | B2 A2 | B2 A3 | B2 min–max | O2 (A3) | O2 vs B2 range |
|---|---|---|---|---|---|---|
| Throughput (tokens/s) | 5.730 | 5.243 | 5.943 | 5.243–5.943 (spread 13.4 %) | 5.258 | **inside** (+0.3 % above min) |
| TTFT p50 (ms) | 4 566 | 5 266 | 4 450 | 4 450–5 266 | 5 691 | above max by 8.1 % |
| TTFT p95 (ms) | 46 094 | 7 557 | 45 300 | 7 557–46 094 | 9 603 | inside |
| E2E p50 (ms) | 11 526 | 21 911 | 10 985 | 10 985–21 911 | 19 748 | inside |
| E2E p95 (ms) | 66 702 | 46 124 | 65 166 | 46 124–66 702 | 51 250 | inside |
| Requests per pod | 2/10 | 0/12 | 2/10 | — | 4/8 | — |

**Classification:**
- The fixed rules compare only the matched pair, B2 A3 with O2 A3:
  - throughput −11.54 %, past the −10 % threshold;
  - TTFT p50 +27.90 %, past the +10 % threshold;
  - so level 2 is a **regression**, and this classification stands.
- Against the three B2 runs, O2's throughput (5.258) lies inside B2's observed
  range (5.243–5.943). Only its TTFT p50 is above the B2 maximum, by 8.1 %.
- So level 2 is a weak regression. That is why the overall verdict is
  `MIXED` and not a clean split between low-load loss and high-load gain.
- No statistical test or confidence interval was computed. A spread across
  three runs is not a confidence interval.

**Why level-2 TTFT p95 and E2E p95 improve while throughput and E2E p50 get worse:**

| Request | B2 TTFT (ms) | O2 TTFT (ms) | Note |
|---|---|---|---|
| `long-1` | 91 993 | 5 895 | B2: a cold ~1 617-token prefill on the pod that had not prefilled it at level 1 |
| `long-2` | 5 448 | 7 609 | |

- The tails improve because B2's slow long prefill disappears in O2.
- Throughput depends on decode. Every level has the same 713 output tokens,
  so throughput is set by wall time: 120.0 s for B2, 135.6 s for O2.
- O2's per-token decode was slower at this level: ITL p50 283 vs 151 ms and
  TPOT p50 254 vs 145 ms.
- An explanation consistent with the data (**hypothesis, not verified per
  request**): O2 more often decoded the two concurrent requests on the same
  2-core pod. B2's per-request pod placement is not recorded, so this cannot
  be checked.

### 6.3 Concurrency 4 and 8: robust gains

- **Throughput:** O2 is above the **maximum** of three B2 attempts:
  - level 4: 9.279 vs B2 6.028–7.280;
  - level 8: 9.873 vs B2 5.022–6.767.
- **Level 8 E2E p95:** 72 119 ms, below the best B2 attempt (99 134 ms).
- **Level 8 mean vLLM queue time:** 5.1 s against 15.7 s.
- B2's level-8 placement was 5/7 in A1, 4/8 in A2 and 3/9 in A3. O2's was
  4/8, with prefix locality.

## 7. Cache-aware routing evidence

| Level | B2 hit ratio | O2 hit ratio | B2 prompt-token split | O2 prompt-token split |
|---|---|---|---|---|
| 1 | 0.427 | 0.408 | 0.00 / 1.00 | 0.61 / 0.39 |
| 2 | 0.661 | 0.894 | 0.25 / 0.75 | 0.61 / 0.39 |
| 4 | 0.738 | 0.894 | 0.27 / 0.73 | 0.61 / 0.39 |
| 8 | 0.874 | 0.894 | 0.12 / 0.88 | 0.61 / 0.39 |

- The hit ratios come from vLLM's own counters per pod, not from the EPP.
- **O2 sends every prompt to the same pod at every level.** Its split is an
  identical 0.61/0.39 at all four levels. The prefix-cache scorer (weight 3)
  keeps a prompt on the pod that holds its prefix.
- **B2's placement varies** with connection reuse.
- **EPP decisions:** 53 `Request handled` log lines (3 cold + 2 warm-up + 48),
  36 to pod `…xxjwz` and 17 to `…4zjmb`. EPP `scheduler_attempts` was 12 per
  level, and `request_error`, extract errors and poll errors were 0.
- **Scheduler decision latency:** p50 1.2 ms, p95 6.1 ms, max 17.2 ms, from
  log timestamps "EPP received request" → "Request handled".
- **Endpoint discovery:** `llm_d_epp_ready_endpoints = 2` after deploy and
  before the O2 benchmark. This gauge counts pods with freshly scraped metrics.
- **Signals the scorers read,** present on both pods: `num_requests_waiting`,
  `num_requests_running`, `kv_cache_usage_perc` and `cache_config_info`.
  `lora_requests_info` is absent; no LoRA is used.

**Long-prompt TTFT.** Earlier wording quoted "48.7 s (B2) vs 6.8 s (O2)". The
precise reading:
- These are **medians of the two `long_chat` requests** (`long-1`, `long-2`,
  ~1 617 input tokens each) at **concurrency 2 in attempt 3**.
- With two values, the median is their mean:
  - B2: (91 993 + 5 448) / 2;
  - O2: (5 895 + 7 609) / 2.
- At concurrency 8 the same statistic is 37.7 s (15 005 and 60 341) against
  8.7 s (5 729 and 11 613).
- This is a two-request subset, not the overall TTFT. The overall TTFT p50 at
  level 2 is worse in O2 (§6.2).

The raw rows are in each run's `raw_requests.jsonl`. EPP decisions are in
[comparison.recomputed.json](../../evidence/llm_agent/g2_20261101T124444Z/g2/comparison.recomputed.json)
and [epp.log](../../evidence/llm_agent/g2_20261101T124444Z/g2/o2/epp.log).

## 8. Security and runtime verification

| Gate (attempt 3) | B2 deployment | O2 deployment |
|---|---|---|
| NetworkPolicy allow/deny matrix | **6/6** ([tsv](../../evidence/llm_agent/g2_20261101T124444Z/ops/b2/netpol-verify.tsv)) | **9/9**, incl. no-bypass probes: unlabelled→router, other-namespace→router and router-label-with-wrong-SA→vLLM all denied ([tsv](../../evidence/llm_agent/g2_20261101T124444Z/ops/o2/netpol-verify.tsv)) |
| Wrong / missing / backend API key | HTTP 401 on the direct and gateway paths | HTTP 401 through the llm-d path |
| Real tool-call smoke (stream and non-stream, two turns) | PASS direct and gateway, `backend=primary` | PASS gateway, `backend=llm-d` |
| Primary → fallback | 503 `primary`, then 200 `fallback`; fallback `request_success_total` 1 → 6 | 503 `llm-d`, 503 `primary`, then 200 `fallback`; 6 → 11 |
| EPP endpoint discovery | — | `ready_endpoints = 2` (after install and before benchmark) |
| MLOps | healthy before, during and after (24 pods, `fraud-detector` Ready), 0 guard stops, 0 regressions | same |

**Not verified by these gates:**
- OAuth/OIDC (agentgateway uses API keys);
- every Istio authorization case (only the probes listed);
- all circuit-breaker state transitions (only eviction under scale-to-zero);
- G3 specialist authentication;
- MCP business end-to-end;
- distributed tracing (EPP tracing was off).

G1's gates ran on B0' in O1. G2 is the first run of those gates on C1.

## 9. Resource and cost analysis

| Item | Measured |
|---|---|
| EPP container | requests 100m / 256Mi, limits 1 CPU / 1Gi; used ~72–74m CPU mean (peak 77m), 23–24 MiB. Flat across levels |
| Envoy sidecar | requests 100m / 128Mi, limits 1 CPU / 512Mi; used 5–8m CPU mean (peak 9m), 14 MiB |
| vLLM memory peak per pod | ~6.4 GiB in both runs (limit 10 GiB) |
| vLLM CFS throttling, pod lifetime (start → end of its run) | B2 0.8 % and 2.7 %; O2 13.0 % and 13.4 % of periods |
| vLLM CFS throttling, benchmark window only (one pod per run has a before/after pair) | B2 `…d7ggp` 1.6 % (120/7 374); O2 `…4zjmb` **28.0 %** (2 086/7 460). Before the window: 8/8 594 and 9/8 617 |
| O2 pod `…4zjmb` load | 17 of 53 EPP decisions, yet ~1.67 cores average CPU over the window. B2 `…d7ggp` averaged ~0.79 cores |

**Hypothesis, not confirmed:** the EPP's `/metrics` scraping (20 times per
second per pod) adds work to vLLM's API process in a pod already near its
3-CPU limit, which would explain the extra throttling. It was not isolated.
The scrape interval was not varied.

**Cost:**
- Each attempt held 3 extra e2-standard-4 nodes. The windows were 1.53 h,
  1.22 h and 1.38 h, so at most **~12.4 node-hours**.
- At an assumed list price of ≈ US$0.165 per node-hour, that is
  **≈ US$2.0–2.1**. The billing API and credit balance are not readable from
  this session, so this is an estimate.

## 10. Failed attempts and corrections

| # | Run | SHA | Window (UTC) | Exit | Classification | Cause and correction |
|---|---|---|---|---|---|---|
| 1 | [`g2_20261101T095348Z`](../../evidence/llm_agent/g2_20261101T095348Z) | `191b13c` | 09:53–11:25 | 1 | setup failure; **B2 retained for variability only** | The router was installed before the llm-agent O2 release that carries its NetworkPolicies. In the default-deny namespace its kubelet probes on 9003 timed out and it crash-looped; `--atomic` removed it before its logs were saved. Fixed in `db43c96`: policies first, no `--atomic` for the router, diagnostics kept |
| 2 | [`g2_20261101T113055Z`](../../evidence/llm_agent/g2_20261101T113055Z) | `db43c96` | 11:30–12:43 | 1 | gate failure; **B2 retained for variability only** | The router, O2 netpol, auth and smoke passed. During the O2 failover gate, llm-d and the direct primary were evicted at 12:25:03–06; 30 s later both were retried and the smoke's second turn got 503 at 12:25:36. Fixed in `0c89af3`: session eviction window 300 s, for B2 and O2 alike |
| 3 | [`g2_20261101T124444Z`](../../evidence/llm_agent/g2_20261101T124444Z) | `0c89af3` | 12:44–14:07 | **0** | **accepted matched benchmark** | All gates passed, B2 and O2 measured, cleanup verified |

- **Accepted measurements:** only attempt 3's B2 and O2 enter the verdict.
- **Retained baselines:** B2 from attempts 1 and 2 is used only as
  variability context (§6). It is never mixed into the matched comparison.
- **Fixes after the run** (`d2f2f02`), from reviewing the runner's remaining
  steps; none changes a measurement:
  - the EPP log parser for v1.0.0's `body` key;
  - log capture can no longer end the run under `set -e`;
  - an EPP re-check before the O2 benchmark;
  - a scrape retry.
- **Cleanup readback in every attempt:**
  - pool back to the 2 MLOps nodes;
  - no LLM release and no G2 object left in `ewallet-agents`;
  - exactly the labelled instances deleted;
  - MLOps healthy.

## 11. Limitations

| # | Limitation | Type |
|---|---|---|
| 1 | O2 regresses at concurrency 1 (−10.74 % throughput, +36.74 % TTFT p50), below all three B2 runs | observed |
| 2 | Concurrency 2 is a rule regression, but O2's throughput lies inside B2's three-run spread; one O2 run, no confidence interval | observed and uncertain |
| 3 | The same 12 prompts repeat at every level. This favours a prefix-aware router after level 1, so the gains at 4/8 are partly a property of this workload | design bias |
| 4 | The 300 s eviction window is a benchmark-session setting (B2 = O2) | configuration |
| 5 | Production keeps 30 s. With llm-d as a third failover target, if both primaries are down, each window's end costs one client 503 (attempt 2) | observed behaviour |
| 6 | O2 vLLM throttling (13 % lifetime, 28 % window on one pod) is not explained; the scraping cause is a hypothesis | unresolved |
| 7 | No real-world or production traffic validation | missing |
| 8 | No runtime reproduction after the final merge | missing |
| 9 | G2 is not integrated into canonical/main/I7 | pending |
| 10 | Both primaries unavailable in production: degraded mode serves from the fallback with periodic 503s (item 5); active health checks are not configured | open |
| 11 | Standalone router only; Gateway API / InferencePool mode not run (needs cluster-wide CRDs) | scope |
| 12 | The Envoy sidecar has no `readOnlyRootFilesystem` (the chart offers no proxy volume) | security residual |

## 12. Reproducibility

The runbook is [docs/llm_agent/g2_runbook.md](../../llm_agent/g2_runbook.md).

- **Offline:**
  - 28 G2 tests, including an end-to-end runner simulation with failure and
    SIGTERM paths;
  - the local preflight with `G2_DRY_RUN=yes`.
- **Recompute from the evidence, without the cluster:**

  ```bash
  python3 scripts/llm_agent/g2_compare.py <copy of g2_20261101T124444Z/g2>
  ```

  Run it on a copy; it rewrites `comparison.{json,md}` in the directory it is
  given.
- **Cloud:** owner approval, about 1.4 h on 3 temporary nodes. A new run
  should use HEAD (≥ `d2f2f02`); the accepted run used `0c89af3`.

## 13. Evidence manifest

| Commit | Content |
|---|---|
| `a961b38` | chart `llmd.mode=standalone`, G2 overlays, router values/pins, G1 operator fixes |
| `191b13c` | runner, benchmark, preflight, readback, compare, guard, simulator, tests |
| `db43c96` | fix after attempt 1 |
| `0c89af3` | fix after attempt 2. **Runtime SHA of the accepted run** |
| `d2f2f02` | post-run review fixes (no measurement change) |
| `74c6cb1` | evidence of attempts 1–3 |
| `99135d0` | first report |
| this commit | documentation closure (this report, runbook note) |

**Integrity:**

| Run | Files (with manifest) | `MANIFEST.sha256` sha256 |
|---|---|---|
| `g2_20261101T095348Z` | 90 | `c18cb428ec8e3401…` |
| `g2_20261101T113055Z` | 120 | `ffa5ebf0f3c2a0e2…` |
| `g2_20261101T124444Z` | 148 | `4fb9ca1c49c3a2c7…` |

Each manifest verifies with `sha256sum -c --quiet MANIFEST.sha256`. Trivy
0.75.0 found 0 secrets in each run. Every 64-hex string in the evidence is a
digest, a container ID or a Helm checksum.

**Accepted-run artifacts:**
- `g2/{b2,o2}/`: `raw_requests.jsonl`, `summary.json`, `serving.json`,
  `pods.json`, `workload.json`, `readback/`, `deploy/`, per-pod logs and `cpu.stat`;
- O2 only: `epp.log`, `envoy.log`, `epp-metrics-*.txt`;
- `g2/comparison.{json,md}`: **original runner output** at `0c89af3`, unmodified;
- `g2/comparison.recomputed.json`: an **additional analysis artifact**. It
  equals the original in every rule field and adds the EPP decision count
  from the `d2f2f02` log parser;
- `ops/{b2,o2}/`: gates;
- `state/resources.jsonl`: 237 guard samples;
- `capacity.json`, `mlops-health-{before,during,after}.json`, `cleanup.txt`.

## 14. Final acceptance matrix

| G2 acceptance gate | Status | Evidence |
|---|---|---|
| 1. B2 deployed | PASS | §4, `g2/b2/serving.json` |
| 2. Two C1 replicas active | PASS | both pods served at levels 2–8 in B2 and at every level in O2 |
| 3. C1 functional/security verification | PASS | §8 (first time for C1) |
| 4. O2 llm-d deployed | PASS | router release, `ready_endpoints = 2` |
| 5. EPP/endpoint selection participates in routing | PASS | 53 EPP decisions, 48/48 `backend=llm-d`. Standalone selector; no InferencePool object |
| 6. AuthN and NetworkPolicy preserved | PASS | 401s, 6/6 and 9/9 |
| 7. Matched raw evidence | PASS | `raw_requests.jsonl` and `summary.json` for both |
| 8. Metrics and routing decisions saved | PASS | per-pod counters, EPP metrics and log |
| 9. Fairness gate | PASS | `fairness_issues: []` |
| 10. Conclusion computed from data | PASS | `g2_compare.py` output `MIXED` |
| 11. MLOps not degraded | PASS | health before, during and after; 0 regressions |
| 12. Temporary resources cleaned up | PASS | `cleanup.txt` of all attempts |
| 13. Report complete | PASS | this document |
| 14. Evidence and checksums stored | PASS | §13 |
| 15. Source and evidence commits | PASS | §13 |
| 16. Runbook reproducible after integration | **NOT YET** | runbook exists; no post-merge reproduction |

**Navigation.** `docs/README.md` and the I7 registry are not changed on this
branch. The entries to add at integration are listed in the closure notes.
