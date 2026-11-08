# Benchmark replay validation on GKE, 2026-11-09

Raw evidence from the validation campaign described in [BENCHMARK_REPLAY.md](../../../../edai2/llm/BENCHMARK_REPLAY.md). **Nothing here is an EXP-V1 or EXP-V2 result.** It contains a cleanup drill and EXP-V2 smoke tests (one run per arm, concurrency 1 and 4, verdict `INCONCLUSIVE` by design). The owner's formal sessions and screenshots are still to come; the `screenshots/` folders are empty on purpose.

Branch `feature/llm-benchmark-evidence-replay`. Campaign window 20:44Z–23:35Z on 2026-11-08 (UTC), 171 of the 240 allowed minutes. Everything ran in namespace `ewallet-bench` on node pool `bench-pool` (e2-standard-4), and both were deleted afterwards.

| Directory | What | Source | Outcome |
|---|---|---|---|
| `safety/preflight/` | Read-only GKE state before the campaign: Helm releases, pools, nodes, quotas, API server, unhealthy pods | `db20155d` | 2 Kubeflow pods already failing (since 2026-11-07, unrelated) |
| `safety/drill/` | Watchdog drill: 1 node, 1 idle pod, watchdog deadline 20:49:26Z | `db20155d` | **PASS**. Workloads deleted 20:51:30, node removed by the autoscaler 21:08:05, no action from the laptop |
| `exp-v2-smoke-attempt1/` | EXP-V2 smoke, interactive | `32fbc4d2` | **FAILED**. DNS egress missed NodeLocal DNSCache: the first request timed out (504 at 170 s), then `NoHealthyBackend`. Cleanup confirmed |
| `exp-v2-smoke-attempt2/` | EXP-V2 smoke, interactive, DNS fixed | `6bbf7317` | Baseline **PASS** (checkpoint answered with `CONTINUE`). Optimized **FAILED**: the runner passed the EPP URL with `/metrics`, and the client appended it again (404). Cleanup confirmed |
| `exp-v2-smoke/` | EXP-V2 smoke, unattended, both fixes | `6f7a9ce7` | Both arms **PASS**. Comparison `INCONCLUSIVE` (1 run per arm). Cleanup confirmed |
| `plan/exp-v1`, `plan/exp-v2` | Local renders, arm difference, cost plan | — | One-line (V1) and two-line (V2) arm difference |
| `safety/resource-inventory/` | GKE state after the campaign | — | Same 17 Helm releases and revisions; only `cpu-pool` (5 VMs); no bench namespace, disk or timer |

## Inside a smoke session directory

- `manifest.json`: source SHA (clean tree), image digests, settings, deviations.
- `session.log`, `terminal.log`: the runner's own log and stdout.
- `production-before.txt` / `production-after.txt`: identical in every session.
- `safety/`: cost plan, watchdog manifest, RBAC read-back (`watchdog-rbac.txt`), watchdog jobs and log, `cleanup.txt`.
- `<arm>/config/run-01.yaml`: the exact rendered stack.
- `<arm>/raw/run-01/`: `raw_requests.jsonl`, `summary.json`, `controls.json` (read from the running pods), EPP metrics before and after (O2).
- `<arm>/metrics/run-01/`: resource samples, window start and end, `prometheus-readback.json`.
- `<arm>/logs/run-01/`: client log, gateway log with credentials filtered, the checkpoint banner, inventory.
- `gateway-policy/<arm>/`: probe results, probe log, gateway policy config with keys removed, gateway log.
- `observability/`: rendered stack, dashboard JSON, panel queries, live metric names, `agentgateway_requests_total` label names.
- `comparison/`: `metrics.json`, `comparison.csv`, `comparison.md`.

## Key readings (exp-v2-smoke)

| Check | Baseline (Service) | Optimized (llm-d EPP) |
|---|---|---|
| Provenance | digest `f695b23e…`, revision `c1899de2…`, 2 replicas on 2 nodes, bind `0-1` | same |
| Requests | 24/24 succeeded | 24/24 succeeded |
| Prometheus readback | 17/22 queries with data; the 5 empty ones are EPP-only | 20/22; the 2 empty ones are EPP error counters (no errors occurred) |
| Scrape targets | 2 vLLM pods, gateway, exporter: all `up` | plus `epp`: all `up` |
| Requests per replica (vLLM counters) | c=1: 0 / 1.0; c=4: 0.33 / 0.67 | c=1 and c=4: 0.75 / 0.25 |
| EPP scheduling decisions | — | 12 per level, both endpoints, status `success` |
| Policy probe | PASS: valid 200; missing, wrong and backend key 401; burst 12×200, 4×429 | PASS: same auth results; EPP request counter +3 for 3 requests; burst 8×200, 4×429, 4×503 (upstream 503 from the router path) |

The numbers in `comparison/` come from one run per arm and two concurrency levels. They show that the pipeline works end to end. They are not a performance claim.

## Not executed

- **EXP-V1 on GKE.** It did not fit the remaining campaign window: each vLLM start-up takes about 16 min. The EXP-V1 path differs from the validated one only in the O1 client, one replica and thread binding. It is covered by the local tests and the plan render.
- **Formal sessions** (3 runs per arm) and **owner screenshots.**
- **Failover.** Not part of the benchmark stack, by design.

Every 64-hex string in these files was checked: each one is an image or chart digest, the workload SHA-256 or a container ID. No key, token or password was written.
