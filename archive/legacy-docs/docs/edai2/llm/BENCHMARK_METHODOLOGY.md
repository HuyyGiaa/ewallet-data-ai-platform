# Benchmark methodology

This page defines how EDAI-2 compares a baseline with an optimization, and the experiments that use the method.

**Selected for the EDAI-2 submission: the single-run methodology** (`EXP_PROFILE=single`, [§ Single-run comparison](#single-run-comparison-selected-for-the-edai-2-submission)). The owner runs it personally and captures the screenshots.

`SINGLE_RUN_RESULTS=COMPLETE` · `OWNER_SCREENSHOTS=INTEGRATED`. Results: [BENCHMARK_RESULTS.md](../../../../../docs/edai2/llm/BENCHMARK_RESULTS.md).

**Earlier results, kept as engineering evidence:**
- **Multi-run formal rehearsal**, 2026-11-09, 3 runs per arm: [benchmark_rehearsal_20261109](../../../../../docs/evidence/edai2/llm/benchmark_rehearsal_20261109/README.md).
- **O1 and G2**, `HISTORICAL`: one run per configuration each.

These earlier results are not reused as the single-run results.

## Profiles

| `EXP_PROFILE` | Runs per arm | Levels (V1 / V2) | Order | Comparator | Use |
|---|---|---|---|---|---|
| `single` | exactly 1 | 1,2,4 / 1,2,4,8 (full) | baseline, then optimized | [`single_run.py`](../../../../../ops/experiments/single_run.py): observed differences | **EDAI-2 submission** |
| `formal` (default) | `EXP_RUNS`, default 3 (2–5) | 1,2,4 / 1,2,4,8 (full) | AB, BA, AB | [`compare.py`](../../../../../ops/experiments/compare.py): rules below | the 2026-11-09 rehearsal |
| `smoke` | exactly 1 | 1 / 1,4 (reduced) | baseline, then optimized | `compare.py`, which returns `INCONCLUSIVE` | workflow check, never a result |

All three profiles use the same image, model, workload, request counts, gateway policy (160 s timeout), fresh pods per run, observability, policy probe, watchdog and cleanup. Only the number of runs, the levels (smoke) and the comparator differ.

The profile is recorded in three places, so single-run and multi-run evidence cannot be mixed up:
- `manifest.json` → `settings.profile` and `settings.methodology` (`single-run`, `multi-run` or `smoke`);
- every checkpoint banner, for example `EXP-V1 SINGLE-RUN / BASELINE run 01`;
- the comparison: `methodology: single-run`, its own verdict names, and a `SINGLE-RUN` title.

`ops.experiments.cli compare` selects the comparator from `settings.profile` in the session's manifest, not from a command-line flag.

## Rules (multi-run comparison)

Every multi-run comparison (`formal`, `smoke`) follows these rules, enforced in [`ops/experiments/compare.py`](../../../../../ops/experiments/compare.py).

| Rule | Effect |
|---|---|
| Both arms need evidence | Otherwise `NOT_EXECUTED` |
| Every control must match, read from live pods | Otherwise `INVALID_COMPARISON`, and no percentages are reported |
| Raw rows must match the summary | Otherwise the level is blocked |
| Failed requests stay in the counts | An arm that fails more is `REGRESSED`, however fast it was |
| A blocked level blocks the verdict | `INCONCLUSIVE` |
| One run is not a result | Below `min_runs`, `INCONCLUSIVE` |
| A change must clear the threshold and the run-to-run spread | Otherwise `NO_SIGNIFICANT_CHANGE` |
| A good level cannot hide a bad one | Better at one level and worse at another is `INCONCLUSIVE` |

With three runs per arm, the spread check compares ranges. That catches obvious noise, but it is not a significance test, so the reports give medians and ranges and make no statistical claim.

In the reports, a positive improvement always means better, whichever way the metric points. The arm values in a multi-run report are **medians** of the runs, with their min–max range.

## Single-run comparison (selected for the EDAI-2 submission)

**Why it is used.** The EDAI-2 rubric asks for a benchmark and an optimization. It does not ask for repeated measurements. The owner chose one run per arm at every original level and with every original request, so the whole benchmark can be replayed and captured personally in one sitting.

**What one pair cannot show.** One run per arm has no run-to-run spread. The comparison therefore reports what was observed, and nothing more:
- no confidence intervals;
- no significance test;
- no claim of a stable improvement.

**Rules**, enforced in [`ops/experiments/single_run.py`](../../../../../ops/experiments/single_run.py). Execution validity and the observed outcome are reported separately.

| Rule | Effect |
|---|---|
| Exactly one run per arm, both with evidence | Otherwise `INVALID` (or `NOT_EXECUTED` when an arm has no run) |
| Every control matches, read from live pods | Otherwise `INVALID`, with the differing controls listed |
| Every level measured in both arms; raw rows match the summary; the same levels in both arms | Otherwise `INVALID` |
| Failed requests and timeouts stay in the counts | An arm that fails more requests at any level cannot be an observed improvement |
| Observed change of the primary metric at each level, against a fixed threshold (the spec's `significant_percent`, 10 %) | This is a reporting rule, not a statistical test |
| Baseline 0, or a value missing in one arm | No percentage, with the reason; never counted as 0 % or as a win |

**Observed outcome** (only when the pair is valid):

| Verdict | Meaning |
|---|---|
| `OBSERVED_IMPROVEMENT` | the primary metric is better by at least the threshold at one or more levels, worse at none, and there are no extra failures |
| `OBSERVED_REGRESSION` | the primary metric is worse by at least the threshold, or the optimized arm has extra failures, and it is better at no level |
| `OBSERVED_MIXED` | better at some levels, and worse at others or with extra failures |
| `NO_CLEAR_OBSERVED_CHANGE` | every level is within the threshold |

**Reported metrics.** Every level reports:
- the spec metrics (below);
- ITL p50, error rate, timeout rate and valid tool calls;
- tokens/s per allocated CPU and tokens/s per CPU core actually used (output tokens/s ÷ mean vLLM CPU cores);
- for EXP-V2 only, each arm's per-replica request share and the EPP counters, shown but not compared.

**How to read a result.** A single-run verdict is an observation of one pair, and it must be written up as one. A valid result does not require the optimized arm to win. A run is not repeated because the optimized arm lost.

## Fairness measures

- **One difference.** Both arms render from one template and one arm table in [`render_bench.py`](../../../../../scripts/experiments/render_bench.py). The `plan` step prints the difference.
- **Alternating order.** Multi-run sessions go A B, B A, A B, so slow drift does not favour an arm. A single-run session is A then B, so drift between the two runs cannot be separated from the effect. This is one of its stated limits.
- **Clean start.** vLLM and the gateway restart before each run, which clears the prefix cache and refills the token budget.
- **Same workload.** Both arms use the pinned `o1-workload-v1`, and its SHA-256 is a control. Cold and warm-up requests are reported separately from measured ones.
- **No fallback.** The stack has one backend, so a failed request stays failed.
- **Isolation.** Each session runs in `ewallet-bench` on its own tainted pool, away from production load.
- **Same machines.** `node_names` is a control, so a node replaced mid-session invalidates the comparison.
- **Provenance.** The running image digest and model revision must match the pins before any request is sent.
- **Complete records.** The raw request count must match the summary, or the level is refused.

## Differences from O1 and G2

The new stack matches the historical vLLM settings from commit `a194189`, with the same image, model and gateway timeouts. It differs in three ways, so its numbers should be compared only with each other:

| Item | O1/G2 | New stack |
|---|---|---|
| Gateway replicas | 2 | 1 |
| vLLM fallback | Present | Absent |
| Istio ambient | Enrolled | Not enrolled |

## Experiments

### EXP-V1: vLLM CPU thread binding

| | Baseline | Optimized |
|---|---|---|
| `VLLM_CPU_OMP_THREADS_BIND` | `auto` | `0-1` |
| Label | `b0-prime` | `c1-thread-bind` |

Everything else matches: image `vllm-cpu-model@sha256:f695b23e…`, Qwen3-0.6B at revision `c1899de2…`, 2 CPU / 6 GiB requested, limit 3 CPU / 10 GiB, 2 GiB KV cache, 4 sequences, prefix cache on, one e2-standard-4 node. Concurrency is 1, 2 and 4, with 12 measured requests per level. There is 1 run per arm with `single`, and 3 with `formal`.

**Primary metric:** output tokens per second. **Hypothesis:** binding improves throughput at the same pod budget.

### EXP-V2: Kubernetes Service versus llm-d EPP

| | Baseline | Optimized |
|---|---|---|
| Route | gateway → Service → vLLM | gateway → router (Envoy + EPP) → vLLM |
| Label | `b2` | `o2` |

Both arms use two `0-1` replicas, one per node. Concurrency is 1, 2, 4 and 8. EPP scorers are as in G2: queue 2, KV 2, prefix 3.

**Primary metric:** e2e p95. **Hypothesis:** routing helps at higher concurrency without hurting low concurrency. The router's own CPU is reported separately, because it is a cost of the optimized arm.

### EXP-V3A and EXP-V3B

See the [LLM A/B design](../../../../../docs/edai2/platform/LLM_AB_DESIGN.md). Both need GPU quota.

### Agent experiments

| Experiment | Baseline | Optimized | Primary metric | Runs in |
|---|---|---|---|---|
| EXP-C5-KEDA | CPU HPA | KEDA on tasks in flight | e2e p95 | Live platform |
| EXP-C5-MCP | 1 replica, no retry | 2 replicas, read-only retry | Availability | `ewallet-mcp-exp` |
| EXP-C5-SANDBOX | BYO Deployment | SandboxAgent on gVisor | e2e p95, no expected winner | `ewallet-sandbox` |

They have two runs per arm and smaller bounds. KEDA load runs inside a Job and goes through a Service, because a port-forward would pin all traffic to one pod. EXP-C5-MCP and EXP-C5-SANDBOX are isolated, so their failure injection can't reach production.

## Observability, capture and unattended safety (2026-11-09)

Sessions now bring their own Prometheus and Grafana inside `ewallet-bench`, can stop for owner screenshots (`EXP_CAPTURE_MODE=interactive`), and are protected against host loss by an in-cluster watchdog and a scale-to-zero pool. The serving settings and the comparison rules above are unchanged.

Two consequences for comparability:

- the monitoring pods share the benchmark nodes in both arms (about 0.16 CPU requested);
- every run pays about 16 minutes of vLLM start-up: about 2 min image pull, then 4 min of engine init including torch.compile, then a first completion that compiles again.

See [BENCHMARK_REPLAY.md](BENCHMARK_REPLAY.md).

## Evidence layout

```text
<experiment>/
  manifest.json  hypothesis.md  verdict.md
  baseline/  config/ raw/run-NN/ metrics/ logs/ screenshots/
  optimized/ config/ raw/run-NN/ metrics/ logs/ screenshots/
  comparison/ metrics.json comparison.csv comparison.md
```

The tooling never writes empty placeholder evidence, and an experiment that has not run has no directory. Screenshots are taken by a person during the run.
