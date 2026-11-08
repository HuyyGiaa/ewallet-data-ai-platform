# Benchmark methodology

This page defines how EDAI-2 compares a baseline with an optimization, and the experiments that use the method. No formal EXP-V1 or EXP-V2 session has run yet; the 2026-11-09 GKE runs were smoke tests ([BENCHMARK_REPLAY.md](BENCHMARK_REPLAY.md)). The O1 and G2 results are `HISTORICAL`: each was one run per configuration, so they motivate these experiments but do not settle them.

## Rules

Every comparison follows the same rules, enforced in [`ops/experiments/compare.py`](../../../ops/experiments/compare.py).

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

In the reports, a positive improvement always means better, whichever way the metric points.

## Fairness measures

- **One difference.** Both arms render from one template and one arm table in [`render_bench.py`](../../../scripts/experiments/render_bench.py). The `plan` step prints the difference.
- **Alternating order.** Runs go A B, B A, A B, so slow drift does not favour an arm.
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

Everything else matches: image `vllm-cpu-model@sha256:f695b23e…`, Qwen3-0.6B at revision `c1899de2…`, 2 CPU / 6 GiB requested, limit 3 CPU / 10 GiB, 2 GiB KV cache, 4 sequences, prefix cache on, one e2-standard-4 node. Concurrency is 1, 2 and 4, with 12 measured requests per level and 3 runs per arm.

**Primary metric:** output tokens per second. **Hypothesis:** binding improves throughput at the same pod budget.

### EXP-V2: Kubernetes Service versus llm-d EPP

| | Baseline | Optimized |
|---|---|---|
| Route | gateway → Service → vLLM | gateway → router (Envoy + EPP) → vLLM |
| Label | `b2` | `o2` |

Both arms use two `0-1` replicas, one per node. Concurrency is 1, 2, 4 and 8. EPP scorers are as in G2: queue 2, KV 2, prefix 3.

**Primary metric:** e2e p95. **Hypothesis:** routing helps at higher concurrency without hurting low concurrency. The router's own CPU is reported separately, because it is a cost of the optimized arm.

### EXP-V3A and EXP-V3B

See the [LLM A/B design](LLM_AB_DESIGN.md). Both need GPU quota.

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
