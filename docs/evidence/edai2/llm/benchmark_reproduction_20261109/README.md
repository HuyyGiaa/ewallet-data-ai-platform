# Benchmark replay validation on GKE, 2026-11-09

Raw evidence from the validation campaign described in [BENCHMARK_REPLAY.md](../../../../edai2/llm/BENCHMARK_REPLAY.md). **Nothing here is an EXP-V1 or EXP-V2 result.** It contains a cleanup drill, EXP-V2 smoke tests (one run per arm, concurrency 1 and 4) and an EXP-V1 smoke test (one run per arm, concurrency 1), each with verdict `INCONCLUSIVE` by design, plus the analysis of the llm-d 503s. The owner's formal sessions and screenshots are still to come; the `screenshots/` folders are empty on purpose.

Branch `feature/llm-benchmark-evidence-replay`. Campaign window 20:44Z–23:35Z on 2026-11-08 (UTC), 171 of the 240 allowed minutes. Everything ran in namespace `ewallet-bench` on node pool `bench-pool` (e2-standard-4), and both were deleted afterwards.

| Directory | What | Source | Outcome |
|---|---|---|---|
| `safety/preflight/` | Read-only GKE state before the campaign: Helm releases, pools, nodes, quotas, API server, unhealthy pods | `db20155d` | 2 Kubeflow pods already failing (since 2026-11-07, unrelated) |
| `safety/drill/` | Watchdog drill: 1 node, 1 idle pod, watchdog deadline 20:49:26Z | `db20155d` | **PASS**. Workloads deleted 20:51:30, node removed by the autoscaler 21:08:05, no action from the laptop |
| `exp-v2-smoke-attempt1/` | EXP-V2 smoke, interactive | `32fbc4d2` | **FAILED**. DNS egress missed NodeLocal DNSCache: the first request timed out (504 at 170 s), then `NoHealthyBackend`. Cleanup confirmed |
| `exp-v2-smoke-attempt2/` | EXP-V2 smoke, interactive, DNS fixed | `6bbf7317` | Baseline **PASS** (checkpoint answered with `CONTINUE`). Optimized **FAILED**: the runner passed the EPP URL with `/metrics`, and the client appended it again (404). Cleanup confirmed |
| `exp-v2-smoke/` | EXP-V2 smoke, unattended, both fixes | `6f7a9ce7` | Both arms **PASS**. Comparison `INCONCLUSIVE` (1 run per arm). Cleanup confirmed |
| `exp-v1-smoke/` | EXP-V1 smoke, interactive (3 checkpoints answered `CONTINUE`), 2026-11-09 06:23–07:15Z | `fe07f1bb` | Both arms **PASS** on one node. Baseline (`auto`) 11/12: `long-1` ended at 161.5 s on the gateway's 160 s timeout, as in O1 B0'; the run was kept and the failure counted. Optimized (`0-1`) 12/12. Tool calls 2/2 per arm. Probe 503s attributed to vLLM admission control. Cleanup confirmed |
| `analysis/llmd-503/` | Why the llm-d policy burst got 4 × 503 | local kind, no cloud | vLLM `--max-num-queued-reqs 8` plus EPP prefix affinity; reproduced locally (8 × 200, 4 × 503) |
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

## Key readings (exp-v1-smoke)

| Check | Baseline `b0-prime` (`auto`) | Optimized `c1-thread-bind` (`0-1`) |
|---|---|---|
| Provenance | digest `f695b23e…`, revision `c1899de2…`, 1 replica, node `…-nb5n` | same node, bind `0-1` |
| Start to Ready | 15.6 min (fresh node, image pull) | 11.6 min (image cached) |
| Requests at c=1 | 11/12; `long-1` HTTP 200 with 0 tokens at 161 486 ms | 12/12 |
| Tool calls | 2/2 valid | 2/2 valid |
| Prometheus readback | 18/23 queries with data; the 5 empty ones are EPP-only | 18/23, same |
| Policy probe | PASS; burst 8 × 200, 4 × 429, 4 × 503; `attribution.json`: vLLM `max_num_queued_reqs` +4 on the one pod | PASS; same counts and attribution |

With one replica, all 12 admitted burst requests reach one vLLM pod, so the 8-request cap applies directly. This is the same mechanism the analysis found for the llm-d arm.

## Not executed

- **Formal sessions** (3 runs per arm) and **owner screenshots.**
- **Failover.** Not part of the benchmark stack, by design.

Every 64-hex string in these files was checked: each one is an image or chart digest, the workload SHA-256 or a container ID. No key, token or password was written.
