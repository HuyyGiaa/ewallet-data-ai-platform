# EXP-V1 / EXP-V2 evidence matrix

Use this file to check each screenshot before submission. Every row links:

- a rubric requirement;
- the experiment it belongs to;
- the owner screenshot ID, defined in the [runbook](../../runbooks/edai2/BENCHMARK_EVIDENCE_RUNBOOK.md);
- the raw artifact that backs the screenshot independently;
- the status of that artifact in the unattended formal rehearsal of 2026-11-09;
- the documentation section.

**Status values:**
- **Rehearsal:** `PASS`, `PARTIAL`, `NOT_EXECUTED`, or the reported verdict. This is what the unattended formal rehearsal produced.
- **Screenshot:** `PENDING` until the owner has captured it. Only the owner captures screenshots. Nothing in this repository generates them.

`R` = `docs/evidence/edai2/llm/benchmark_rehearsal_20261109`. `O` = the owner's `$CAMPAIGN` directory.

Every number is in the [rehearsal README](../../evidence/edai2/llm/benchmark_rehearsal_20261109/README.md). Screenshot paths are relative to `O`.

## Preflight

| Rubric | Experiment | Screenshot | Raw artifact (rehearsal) | Rehearsal | Screenshot | Doc |
|---|---|---|---|---|---|---|
| 6 | both | `PRE-1` plan and cost | `R/exp-v2/safety/cost-plan.json` (worst case 1.85, fits) | PASS | PENDING | runbook §2 |
| 6 | both | `PRE-2` arm difference | `R/exp-v1/setup.yaml`, `R/exp-v2/setup.yaml` | PASS | PENDING | runbook §2 |

## EXP-V1: thread binding `auto` versus `0-1`

| Rubric | Experiment | Screenshot | Raw artifact (rehearsal) | Rehearsal | Screenshot | Doc |
|---|---|---|---|---|---|---|
| 3, 4 | EXP-V1 baseline | `V1-01` provenance | `R/exp-v1/baseline/raw/run-01/controls.json`, `manifest.json` | PASS (1 node, digest `f695b23e`) | PENDING | runbook §3 |
| 6 | EXP-V1 baseline | `V1-02` config | `R/exp-v1/baseline/config/run-01.yaml` | PASS | PENDING | runbook §3 |
| 6 | EXP-V1 baseline | `V1-03` running | `R/exp-v1/baseline/raw/run-01/raw_requests.jsonl` | PASS | PENDING | runbook §3 |
| 6 | EXP-V1 baseline | `V1-04` result | `R/exp-v1/baseline/raw/run-01/summary.json` | PASS (0/36 failed) | PENDING | runbook §3 |
| 6, 50 | EXP-V1 baseline | `V1-05` Grafana | `R/exp-v1/observability/metric-readback/baseline-run-01.json` | PASS | PENDING | runbook §3 |
| 50 | EXP-V1 baseline | `V1-05b` targets | `R/exp-v1/baseline/metrics/run-01/prometheus-readback.json` | PASS (3/3 up, all 6 runs) | PENDING | runbook §3 |
| 6 | EXP-V1 optimized | `V1-06` config | `R/exp-v1/optimized/config/run-01.yaml` | PASS | PENDING | runbook §3 |
| 6 | EXP-V1 optimized | `V1-07` running | `R/exp-v1/optimized/raw/run-01/raw_requests.jsonl` | PASS | PENDING | runbook §3 |
| 6, 50 | EXP-V1 optimized | `V1-08` result and Grafana | `R/exp-v1/optimized/raw/run-01/summary.json`, `metric-readback/optimized-run-01.json` | PASS | PENDING | runbook §3 |
| 6 | EXP-V1 | `V1-09` comparison | `R/exp-v1/comparison/metrics.json` | `IMPROVED` (tok/s +87/+73/+76 % at c=1/2/4) | PENDING | runbook §3, [README §2](../../evidence/edai2/llm/benchmark_rehearsal_20261109/README.md#2-exp-v1-vllm_cpu_omp_threads_bind-auto-versus-0-1) |
| 3, 6 | EXP-V1 | `V1-10` manifest | `R/exp-v1/manifest.json` | PASS (`deviations: []`) | PENDING | runbook §3 |
| 6, 50 | EXP-V1 | `V1-11` session in Grafana | `R/exp-v1/observability/metric-readback/` (6 files) | PASS | PENDING | runbook §3 |
| 6 | EXP-V1 | `V1-12` cleanup | `R/exp-v1/safety/cleanup.txt`, `R/chain-gate-exp-v1.json` | PASS | PENDING | runbook §3 |

## EXP-V2: Kubernetes Service versus llm-d EPP

| Rubric | Experiment | Screenshot | Raw artifact (rehearsal) | Rehearsal | Screenshot | Doc |
|---|---|---|---|---|---|---|
| 3, 6 | EXP-V2 B2 | `V2-01` two replicas | `R/exp-v2/baseline/raw/run-01/controls.json` | PASS (2 nodes, 1 pod each) | PENDING | runbook §5 |
| 6 | EXP-V2 B2 | `V2-02` route | `R/exp-v2/baseline/config/run-01.yaml` | PASS | PENDING | runbook §5 |
| 6 | EXP-V2 B2 | `V2-03` running | `R/exp-v2/baseline/raw/run-01/raw_requests.jsonl` | PASS | PENDING | runbook §5 |
| 6, 50 | EXP-V2 B2 | `V2-04` result and Grafana | `R/exp-v2/baseline/raw/run-01/summary.json`, `metric-readback/baseline-run-01.json` | PASS (0/48 failed; 4/4 targets up) | PENDING | runbook §5 |
| 6 | EXP-V2 B2 | `V2-05` per-replica counters | `summary.json` → `levels.*.distribution` | PASS | PENDING | runbook §5 |
| 6 | EXP-V2 O2 | `V2-06` route | `R/exp-v2/optimized/config/run-01.yaml` | PASS | PENDING | runbook §5 |
| 6 | EXP-V2 O2 | `V2-07` EPP endpoints and log | `R/exp-v2/gateway-policy/optimized/epp.log` | PASS (2 endpoints remain after filter) | PENDING | runbook §5 |
| 6 | EXP-V2 O2 | `V2-08` running | `R/exp-v2/optimized/raw/run-01/raw_requests.jsonl` | PASS | PENDING | runbook §5 |
| 6, 50 | EXP-V2 O2 | `V2-09` result, Grafana, distribution | `R/exp-v2/optimized/raw/run-01/summary.json`, `metric-readback/optimized-run-01.json` | PASS (0/48 failed; 5/5 targets up) | PENDING | runbook §5 |
| 6 | EXP-V2 | `V2-10` comparison | `R/exp-v2/comparison/metrics.json` | `NO_SIGNIFICANT_CHANGE` (differences within run-to-run spread) | PENDING | runbook §5, [README §3](../../evidence/edai2/llm/benchmark_rehearsal_20261109/README.md#3-exp-v2-kubernetes-service-b2-versus-llm-d-epp-o2-2-replicas-0-1) |
| 6 | EXP-V2 | `V2-11` cleanup | `R/exp-v2/safety/cleanup.txt`, `R/chain-gate-exp-v2.json` | PASS | PENDING | runbook §5 |

## Gateway policy

| Rubric | Experiment | Screenshot | Raw artifact (rehearsal) | Rehearsal | Screenshot | Doc |
|---|---|---|---|---|---|---|
| 5 | EXP-V2 both arms | `POL-1` valid key → 200 | `R/exp-v2/gateway-policy/<arm>/results.json` `valid_client_key` | PASS (also EXP-V1) | PENDING | runbook §6 |
| 5 | EXP-V2 both arms | `POL-2` missing key → 401 | same, `missing_key` | PASS | PENDING | runbook §6 |
| 5 | EXP-V2 both arms | `POL-3` invalid key, backend key → 401 | same, `wrong_key`, `backend_key_as_client_key` | PASS | PENDING | runbook §6 |
| 5 | EXP-V2 both arms | `POL-4` rate limit → 429 | same, `rate_limit_burst`; `gateway.log` | PASS (4×429 in every arm) | PENDING | runbook §6 |
| 5, 6 | EXP-V2 both arms | `POL-5` routing | same, `routing_backend_header` | PASS (`primary` / `llm-d`) | PENDING | runbook §6 |
| 5 | EXP-V2 O2 | `POL-6` admission (503) | `R/analysis/probe-attribution/exp-v2-optimized.json`, `counters/*.prom` | ATTRIBUTED after re-analysis (runner wrote UNATTRIBUTED; README §4). B2: NO_503 | PENDING | runbook §6 |
| 5 | EXP-V2 both arms | `POL-7` policy config | `R/exp-v2/gateway-policy/<arm>/gateway-policy-config.yaml` | PASS | PENDING | runbook §6 |
| 5 | — | failover | — | NOT_EXECUTED (no fallback in the benchmark stack, on purpose; G1 holds failover) | not applicable | runbook §6 |
