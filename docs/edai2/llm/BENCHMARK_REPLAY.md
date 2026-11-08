# EXP-V1 / EXP-V2 benchmark replay on GKE

This page describes how the O1 and G2 CPU benchmarks are reproduced on GKE so that the owner can capture genuine screenshots of the runs and of agentgateway policy enforcement. It covers what was added to the existing framework, what was validated on GKE on 2026-11-09, and what is still pending.

Owner procedure: [BENCHMARK_EVIDENCE_RUNBOOK.md](../../runbooks/edai2/BENCHMARK_EVIDENCE_RUNBOOK.md). Comparison rules: [BENCHMARK_METHODOLOGY.md](BENCHMARK_METHODOLOGY.md).

## 1. What is what

Keep these apart when reading any number:

| Kind | Where | Status |
|---|---|---|
| Historical O1 (thread binding) | [vllm_cpu_o1_benchmark.md](vllm_cpu_o1_benchmark.md), evidence `o1_20261101T052939Z` | Frozen. One run per configuration. The evaluator said `NO_GAIN` (baseline-validity gate); the owner selected C1 by review |
| Historical G2 (llm-d routing) | [llmd_g2_routing_benchmark.md](llmd_g2_routing_benchmark.md), evidence `g2_20261101T124444Z` | Frozen. `MIXED`. The original `comparison.json` showed 0 EPP decisions (parser issue); `comparison.recomputed.json` shows 53 and changes no performance result |
| This replay's implementation | `scripts/experiments/vllm_bench.sh` and the `ops/experiments` modules listed in §3 | Tested locally (§7) |
| This replay's GKE validation | [§8](#8-gke-validation-2026-11-09), evidence `benchmark_reproduction_20261109/` | Smoke and drill only. These are not benchmark results |
| Owner's evidence session | runbook | **Pending** |
| Production LLM deployment | `llm-agent` Helm release (revision 42) | Not touched by any of this |
| Benchmark deployment | namespace `ewallet-bench`, node pool `bench-pool` | Temporary. Created and deleted per session; never a production promotion |

New results come only from new raw records. The historical numbers are context. They are not reused.

## 2. Experiments

### EXP-V1: vLLM CPU thread binding

| | |
|---|---|
| Research question | Does binding vLLM's two OpenMP threads to CPUs 0-1 raise output throughput on the same pod budget? |
| Hypothesis | `0-1` raises output tokens/s at concurrency 4 compared with `auto` |
| Hardware | one e2-standard-4 node (`bench-pool`), asia-southeast1-b |
| Model | `Qwen/Qwen3-0.6B` at revision `c1899de289a04d12100db370d81485cdf75e47ca`, baked into the image |
| Image | `vllm-cpu-model@sha256:f695b23efa8145ae5cb57a1873863879927ab4805e70e5eee1a71a2063edd07c` (verified in Artifact Registry on 2026-11-09) |
| Baseline / optimized | `VLLM_CPU_OMP_THREADS_BIND=auto` (`b0-prime`) / `0-1` (`c1-thread-bind`) |
| Controlled | image digest, model revision, 2 CPU / 6 GiB requests, max-num-seqs 4, prefix cache on, gateway token budget, node names, workload SHA-256 |
| Workload | `o1-workload-v1`: 3 cold-prefix requests, 2 warm-up requests, then 12 measured requests at concurrency 1, 2, 4 |
| Repetitions | 3 runs per arm, alternating A B, B A, A B |
| Primary metric | output tokens/s |

### EXP-V2: Kubernetes Service versus llm-d EPP

| | |
|---|---|
| Research question | With two identical replicas, does llm-d Endpoint Picker routing improve latency over a Kubernetes Service? |
| Hypothesis | EPP helps at higher concurrency without hurting low concurrency |
| Hardware | two e2-standard-4 nodes, one replica on each |
| Baseline (B2) | agentgateway → Service `vllm-primary` → two replicas |
| Optimized (O2) | agentgateway → Envoy + llm-d EPP v1.0.0 (standalone chart, pinned SHA-256) → the same two replicas |
| Controlled | as EXP-V1, plus replicas = 2 and thread binding `0-1` in both arms |
| Workload | the same `o1-workload-v1`, at concurrency 1, 2, 4, 8 |
| Primary metric | e2e p95 |

The only rendered difference is the gateway's `baseUrl` and its `x-ewallet-backend` label. `plan` prints the diff, and a test pins it.

## 3. What this replay adds

The existing framework (matched workload, provenance checks, alternating arms, raw records, comparator, ownership marks) is reused unchanged. The additions are:

| Concern | Change | Files |
|---|---|---|
| Laptop sleep or loss | `bench-pool` autoscales with min 0 / max = planned nodes. An in-cluster CronJob watchdog deletes the namespace's workloads at the time limit + 10 min, and the node then drains to zero without any action from the laptop. A systemd user timer runs `down` at the limit + 20 min as a second path | `ops/experiments/bench_watchdog.py`, `infra/kubernetes/experiments/bench-watchdog.yaml.tmpl` |
| Watchdog reach | A Role in `ewallet-bench` only: list/delete Deployments, StatefulSets and Jobs, and delete its own CronJob by name. The runner checks it with `kubectl auth can-i` and refuses to continue if it can do more | same |
| Cost and time | Worst-case plan (all nodes up for the whole limit, plus teardown, plus watchdog and scale-down delay, plus 25 % margin). The session refuses above USD 5 or a 240-minute window. The limit is wall-clock time | `ops/experiments/cost_plan.py` |
| Cleanup proof | After deletion: pool absent, namespace absent, and no VM or disk named `gke-ewallet-dev-bench-pool-*`. A failed read counts as failure. Cleanups are serialized with a lock | `vllm_bench.sh` |
| Observability | Isolated Prometheus (pod discovery in `ewallet-bench` only, one target per pod) and Grafana with one dashboard. CPU and memory come from `metrics.k8s.io` through a sidecar exporter | `bench-observability.yaml.tmpl`, `bench_dashboard.py`, `pod_usage_exporter.py` |
| Panel proof | After each run, every dashboard query runs against Prometheus for that run's window. The result records which panels had data, plus the scrape targets and Grafana's data source path | `prom_readback.py` |
| Policy evidence | A bounded probe after run 01 of each arm: valid, missing and wrong key, backend key as client key, route header and EPP counter, and a 16-request burst against the 12/s limit | `gateway_policy.py` |
| Owner capture | `EXP_CAPTURE_MODE=interactive` stops after every run and before cleanup. It prints the configuration read from the pods, the client summary, a Grafana link for exactly that window, the screenshot paths and the commands. `CONTINUE`, `ABORT`, or a timeout (stop and clean up) | `checkpoint.py`, `vllm_bench.sh` |
| Rehearsal | `EXP_PROFILE=smoke` runs one run per arm at fewer levels and always records itself as a smoke test (verdict `INCONCLUSIVE`). The `drill` action proves the watchdog path | `vllm_bench.sh` |

Defects found and fixed on the way. None of these had shown up before, because the stack had never run on GKE:

| Defect | Effect | Fix |
|---|---|---|
| Secrets were created with `<<<`, which adds a newline | Every `Bearer` header would have carried `\n`. The vLLM startup probe could not authenticate and the client key never matched | `printf '%s'` |
| `allow-dns` matched only kube-system pods | With NodeLocal DNSCache no lookup reaches a kube-dns pod. The gateway could not resolve `vllm-primary`: the first request timed out after 170 s, then every request got 503 `NoHealthyBackend` (EXP-V2 smoke attempt 1) | Allow `169.254.20.10/32` and the kube-dns ClusterIP, as production does |
| API server egress allowed only the endpoint IP | In-cluster clients dial the Service address | Both `/32`s |
| Exporter used `PORT` | The helper image sets `PORT=8080`, so the exporter listened on the wrong port | `EXPORTER_PORT` |
| Watchdog deleted its CronJob with background propagation | Its own Job and log were garbage-collected | `Orphan` for its own CronJob |

## 4. Observability

Prometheus scrapes, every 5 s:

| Job | Targets | Labels |
|---|---|---|
| `vllm` | each running vLLM pod, port 8000 | `pod`, `node`, `run_id` (e.g. `optimized-01`), `arm`, `experiment` |
| `agentgateway` | the gateway pod, port 15020 | same |
| `epp` | the llm-d EPP pod, port 9090 (O2 only) | `pod`, `node`, `experiment` |
| `pod-usage` | the exporter in the Prometheus pod | per `pod` and `container` |

The pod discovery means that the two EXP-V2 replicas are two targets, not one Service sample. Because each run restarts the pods, every run's series has its own pod name and `run_id`, so a counter reset never mixes runs.

The dashboard `EDAI-2 — LLM CPU Benchmark (EXP-V1 / EXP-V2)` (uid `edai2-llm-cpu-bench`) has three groups:

- **A. Serving.** Output and prompt tokens/s per pod; TTFT and e2e p50/p95 from vLLM histograms; running and waiting requests; gateway responses by status.
- **B. Resources.** CPU and memory working set per container, from metrics-server; mean queue time; prefix-cache hit ratio.
- **C. Routing.** Completed requests per replica and their share over the selected range; EPP ready endpoints, scheduling decisions per endpoint, errors and metric-poll errors, scheduler p95; scrape health and exporter age.

Every metric name in these queries appears in a real scrape of the pinned versions, and a test checks this. A missing series renders as "no data: series absent", never as 0.

The benchmark client remains the authoritative source. Grafana's percentiles are bucket interpolations over a 1-minute rate at a 5-second scrape, so with 12 requests per level they can differ from the client's exact percentiles. Requested CPU is never shown as usage.

Not collected: cAdvisor (it needs `nodes/metrics` cluster-wide) and vLLM process CPU (the engine runs in a child process).

## 5. Safety and cost

| Limit | Value | Enforced by |
|---|---|---|
| Nodes | 1 (EXP-V1), 2 (EXP-V2); never more than 2 | `--max-nodes`, cost plan |
| Machine | e2-standard-4, no GPU | pool flags |
| Time limit | default 210 min, at most 215 (setup 10 + limit + teardown 15 ≤ 240) | runner |
| Cost | worst case USD 0.91 (EXP-V1) and USD 1.82 (EXP-V2), ceiling USD 5 | `cost_plan.py` |
| Host loss | workloads deleted at limit + 10 min; node removed by the autoscaler about 15 min after it empties (measured in the drill); `down` at limit + 20 min from the laptop timer | watchdog, autoscaler, timer |

Prices are asia-southeast1 list prices read on 2026-11-09: about USD 0.165 per e2-standard-4 hour, plus pd-balanced disk and an external IP. These are estimates, not a billing readback.

## 6. Gateway policy evidence

The benchmark gateway configures exactly these policies, and the probe tests each one:

- `apiKey` strict with one client key;
- a separate vLLM backend key;
- `localRateLimit` of 12 requests/s and a per-run token budget;
- per-route timeouts;
- the `x-ewallet-backend` response header.

**Failover is NOT_EXECUTED here, on purpose.** The benchmark stack has no fallback backend, so a failed request stays failed and cannot flatter an arm. Failover was demonstrated in G1; see [LLM_PLATFORM.md §4](LLM_PLATFORM.md#4-runtime-verification). This benchmark does not test production failover.

## 7. Local validation

`tests/experiments` (268 tests) runs the real runner, renderer, preflight, readback and comparator against fake `kubectl`, `gcloud`, `helm`, `systemd-run` and benchmark clients. It covers:

- every stage and both arm transitions;
- refusals before any resource;
- the watchdog RBAC gate;
- the scale-to-zero flags;
- the laptop timer;
- secrets without a newline;
- the cost and time ceilings;
- interactive `CONTINUE`, `ABORT`, timeout and closed input, and checkpoints on the first run only;
- the smoke profile;
- leaked port-forwards;
- a leftover VM;
- the drill pass and failure paths;
- DNS egress;
- the EPP URL contract.

Additional checks:

- the Prometheus config passes `promtool check config` (v3.7.3);
- all 22 dashboard queries parse in Prometheus v3.7.3;
- the dashboard loads in Grafana 12.3.0 with a Viewer-readable data source.

## 8. GKE validation (2026-11-09)

Evidence: [`benchmark_reproduction_20261109/`](../../evidence/edai2/llm/benchmark_reproduction_20261109/README.md). Window 20:44Z–23:35Z (171 of 240 minutes), about 4.9 node-hours, estimated USD 0.88 at list price (not a billing readback). No GPU, no production change.

| Session | Nodes | Result |
|---|---|---|
| [Cleanup drill](../../evidence/edai2/llm/benchmark_reproduction_20261109/safety/drill/) | 1 | **PASS.** Watchdog deadline 20:49:26; Deployments and CronJob gone 20:51:30; node removed by the autoscaler 21:08:05. The laptop only watched. RBAC read-back: yes in `ewallet-bench` only |
| [EXP-V2 smoke, attempt 1](../../evidence/edai2/llm/benchmark_reproduction_20261109/exp-v2-smoke-attempt1/) | 2 | Failed on DNS egress (§3), found by this run. Cleanup confirmed |
| [EXP-V2 smoke, attempt 2](../../evidence/edai2/llm/benchmark_reproduction_20261109/exp-v2-smoke-attempt2/) | 2 | Interactive checkpoint answered over stdin; baseline passed. The optimized client got 404 on `/metrics/metrics` (§3). Cleanup confirmed |
| [EXP-V2 smoke, attempt 3](../../evidence/edai2/llm/benchmark_reproduction_20261109/exp-v2-smoke/) | 2 | Both arms passed. Comparison `INCONCLUSIVE` (1 run per arm, by design) |

What attempt 3 verified, per arm:

| Check | Kubernetes Service (B2) | llm-d EPP (O2) |
|---|---|---|
| Running image and model | digest `f695b23e…`, revision `c1899de2…`, read from the pods before the first request | same |
| Replicas | 2, one per node, bind `0-1` | same |
| Requests | 24/24 HTTP 200, backend header `primary` | 24/24 HTTP 200, backend header `llm-d` |
| Per-replica share (vLLM counters, c=1 / c=4) | 0 / 1.0, then 0.33 / 0.67 | 0.75 / 0.25 at both levels |
| EPP | — | 12 scheduling attempts per level, both endpoints, `success` |
| Prometheus targets | two vLLM pods, gateway, exporter: `up` | plus EPP: `up` |
| Dashboard queries with data | 17/22 (5 EPP-only empty) | 20/22 (2 EPP error counters empty: no errors) |
| Grafana | dashboard provisioned, data source path to Prometheus OK | same |
| Policy probe | PASS | PASS |

Policy probe details (the instructor's request):

| Case | B2 | O2 |
|---|---|---|
| valid client key | 200 | 200 |
| no key / wrong key / vLLM backend key | 401 / 401 / 401 | 401 / 401 / 401 |
| route | 3 × 200, `x-ewallet-backend: primary` | 3 × 200, `x-ewallet-backend: llm-d`, EPP `llm_d_epp_request_total` +3 |
| 16 concurrent requests against 12/s | 12 × 200, 4 × 429 `rate limit exceeded` (`x-ratelimit-limit: 12`) | 8 × 200, 4 × 429, 4 × 503 |

The four 503s in the llm-d burst came from the router path: the gateway records no reason of its own. They match the EPP's saturation filter, which G2 also recorded as `ServiceUnavailable`. This was not attributed further.

Start-up dominates the time budget. Each vLLM start took about 16 minutes:

- about 2 min image pull;
- 4 min engine init, including torch.compile and profiling;
- about 7 min for the startup probe's first completion, which compiles the remaining shapes.

A formal EXP-V1 session (6 starts) therefore needs about 190 minutes before any checkpoint waits. That is why the runbook sets `EXP_MAX_MINUTES=215` and `EXP_CHECKPOINT_RUNS=first`.

Not run on GKE: EXP-V1. It no longer fitted the window. Its path differs from the verified one only in the O1 client, one replica and thread binding, and the local tests cover it.

## 9. Remaining work

1. **Owner evidence session** ([runbook](../../runbooks/edai2/BENCHMARK_EVIDENCE_RUNBOOK.md)): EXP-V1 and then EXP-V2, interactive, 3 runs per arm, screenshots at the printed paths.
2. **Fill in the results from that session's `comparison/metrics.json`.** Until then the historical O1 and G2 figures in [LLM_PLATFORM.md §5](LLM_PLATFORM.md#5-performance-and-optimisation) stay labelled historical.
3. **Optional.** Persist the torch.compile cache across runs, to cut about 6 min per start. This changes the stack and needs its own review.
