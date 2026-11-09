# EXP-V1 / EXP-V2 evidence matrix (single-run submission)

Use this file to check each screenshot before submission.

**What each row gives:**
- the screenshot ID and its kind;
- the rubric row, the experiment and the arm;
- when to capture it, and the command or page to capture;
- what must be visible;
- the raw artifact that verifies the screenshot independently;
- the suggested file name;
- its status.

The commands are given in full in the [runbook](../../runbooks/edai2/BENCHMARK_EVIDENCE_RUNBOOK.md), in the section listed for each table.

**Methodology.** The submission uses the single-run methodology: `EXP_PROFILE=single`, one run per arm, every level ([methodology](BENCHMARK_METHODOLOGY.md#single-run-comparison-selected-for-the-edai-2-submission)).
- Status: `SINGLE_RUN_RESULTS=PENDING`, `OWNER_SCREENSHOTS=PENDING`.
- Every screenshot below is `PENDING` until the owner has captured it.
- Only the owner captures screenshots. Nothing in this repository generates them.

**Kinds:**
- **REQUIRED:** needed to show a rubric requirement (deployment, model configuration, benchmark results, optimization, gateway security and policy, monitoring, llm-d routing).
- **SUPPORTING:** diagnostic or technical detail, which can be left out of the submission.

**Paths.** `O` = the owner's `$CAMPAIGN` (`docs/evidence/edai2/llm/owner_single_run_<date>`). `V1` = `O/exp-v1`, `V2` = `O/exp-v2`.

**Rubric rows** (EDAI-2, [RUBRIC_EVIDENCE_INDEX.md](../RUBRIC_EVIDENCE_INDEX.md)):

| Row | Requirement |
|---|---|
| 3 | deploy the LLM inference platform |
| 4 | custom model |
| 5 | agentgateway security and policy |
| 6 | benchmark and optimize |
| 50 | LLM telemetry |

## Coverage of the rubric

| Requirement | REQUIRED screenshots |
|---|---|
| Deployment (3) | S1-01, S1-02, S2-01, S1-12 |
| Model configuration (3, 4) | S1-01 (image digest, model revision), S1-02 |
| Benchmark results (6) | S1-03, S1-04, S1-09, S2-04, S2-09 |
| Optimization (6) | S1-07 against S1-02, S1-11, S2-07, S2-11 |
| Gateway security and policy (5) | POL-1, POL-2, POL-3, POL-4, POL-5, POL-7 |
| Monitoring (50) | S1-05, S1-10, S2-05, S2-09 |
| llm-d routing (6) | S2-07, S2-08, S2-09, POL-5 (O2) |
| Cleanup and production safety | S1-14, S2-13 |

## Preflight (runbook §2)

| ID | Kind | Rubric | Experiment / arm | When | Command | Must show | Raw artifact | File | Status |
|---|---|---|---|---|---|---|---|---|---|
| PRE-1 | SUPPORTING | 6 | both | before EXP-V1 | `vllm_bench.sh exp-v1 plan` / `exp-v2 plan` with `EXP_PROFILE=single` | `methodology=single-run`, `runs_per_arm=1`, full levels, `fits: true` | `V1/safety/cost-plan.json`, `V2/safety/cost-plan.json` | `O/screenshots/PRE-1_plan.png` | PENDING |
| PRE-2 | SUPPORTING | 6 | both | before EXP-V1 | `cat /tmp/plan-v*/plan/arm-difference.diff` | one variable per experiment | `V1/setup.yaml`, `V2/setup.yaml` | `O/screenshots/PRE-2_arm_difference.png` | PENDING |

## EXP-V1: thread binding `auto` (B0) versus `0-1` (C1) (runbook §4)

| ID | Kind | Rubric | Arm | When | Command | Must show | Raw artifact | File | Status |
|---|---|---|---|---|---|---|---|---|---|
| S1-01 | REQUIRED | 3, 4 | B0 | checkpoint 1 | `ev_provenance V1/baseline/raw/run-01` | `bench-pool` node, `vllm-primary` Running, digest `f695b23e…`, revision `c1899de2…` | `V1/baseline/raw/run-01/controls.json` | `V1/baseline/screenshots/S1-01_pods_nodes.png` | PENDING |
| S1-02 | REQUIRED | 3, 6 | B0 | checkpoint 1 | `ev_config; ev_route` | `THREADS_BIND=auto`, vLLM args, resources, route, 160 s timeout | `V1/baseline/config/run-01.yaml` | `V1/baseline/screenshots/S1-02_config.png` | PENDING |
| S1-03 | REQUIRED | 6 | B0 | baseline window | progress view + Grafana | requests in flight at c=1, 2, 4 | `V1/baseline/raw/run-01/raw_requests.jsonl` | `V1/baseline/screenshots/S1-03_running.png` | PENDING |
| S1-04 | REQUIRED | 6 | B0 | checkpoint 1 | runner banner | `SINGLE-RUN`, `c=1, 2, 4` lines | `V1/baseline/raw/run-01/summary.json` | `V1/baseline/screenshots/S1-04_result.png` | PENDING |
| S1-05 | REQUIRED | 6, 50 | B0 | checkpoint 1 | Grafana link from the banner | tokens/s, TTFT, E2E, queue, admission rejections, CPU, memory for `baseline-01` | `V1/baseline/metrics/run-01/prometheus-readback.json` | `V1/baseline/screenshots/S1-05_grafana.png` | PENDING |
| S1-06 | SUPPORTING | 50 | B0 | checkpoint 1 | `ev_targets` | all targets `up` | `prometheus-readback.json` → `targets` | `V1/baseline/screenshots/S1-06_targets.png` | PENDING |
| S1-07 | REQUIRED | 6 | C1 | checkpoint 2 | `ev_config` | `THREADS_BIND=0-1`, rest as S1-02 | `V1/optimized/config/run-01.yaml` | `V1/optimized/screenshots/S1-07_config.png` | PENDING |
| S1-08 | SUPPORTING | 6 | C1 | optimized window | progress view + Grafana | requests in flight | `V1/optimized/raw/run-01/raw_requests.jsonl` | `V1/optimized/screenshots/S1-08_running.png` | PENDING |
| S1-09 | REQUIRED | 6 | C1 | checkpoint 2 | runner banner | `Thread binding: 0-1`, same node, `c=1, 2, 4` lines | `V1/optimized/raw/run-01/summary.json` | `V1/optimized/screenshots/S1-09_result.png` | PENDING |
| S1-10 | REQUIRED | 6, 50 | C1 | checkpoint 2 | Grafana link from the banner | as S1-05 for `optimized-01` | `V1/optimized/metrics/run-01/prometheus-readback.json` | `V1/optimized/screenshots/S1-10_grafana.png` | PENDING |
| S1-11 | REQUIRED | 6 | both | checkpoint 3 | comparison banner / `comparison.md` | `single-run`, `VALID`, observed outcome, failures, metrics per level | `V1/comparison/metrics.json`, `comparison.csv` | `V1/comparison/screenshots/S1-11_comparison.png` | PENDING |
| S1-12 | REQUIRED | 3, 6 | both | checkpoint 3 | `json.tool V1/manifest.json` (filtered) | `profile: single`, `methodology: single-run`, `runs_per_arm: 1`, `source_sha`, digests | `V1/manifest.json` | `V1/comparison/screenshots/S1-12_manifest.png` | PENDING |
| S1-13 | SUPPORTING | 50 | both | checkpoint 3 | Grafana, whole session | both runs | `V1/observability/metric-readback/` | `V1/comparison/screenshots/S1-13_grafana_session.png` | PENDING |
| S1-14 | REQUIRED | 6 | — | after `runner exit 0` | `ev_cleanup V1` | cleanup confirmed, production unchanged | `V1/safety/cleanup.txt`, `production-*.txt` | `O/screenshots/S1-14_cleanup.png` | PENDING |

## EXP-V2: Kubernetes Service (B2) versus llm-d EPP (O2), 2 replicas (runbook §6)

| ID | Kind | Rubric | Arm | When | Command | Must show | Raw artifact | File | Status |
|---|---|---|---|---|---|---|---|---|---|
| S2-01 | REQUIRED | 3, 6 | B2 | checkpoint 1 | `ev_provenance V2/baseline/raw/run-01` | 2 nodes, 2 pods (1 per node), digest, revision | `V2/baseline/raw/run-01/controls.json` | `V2/baseline/screenshots/S2-01_pods_nodes.png` | PENDING |
| S2-02 | REQUIRED | 6 | B2 | checkpoint 1 | `ev_route; ev_config` | route via Service, `x-ewallet-backend: primary`, `0-1` | `V2/baseline/config/run-01.yaml` | `V2/baseline/screenshots/S2-02_route.png` | PENDING |
| S2-03 | SUPPORTING | 6 | B2 | B2 window | progress view + Grafana | requests up to c=8 | `V2/baseline/raw/run-01/raw_requests.jsonl` | `V2/baseline/screenshots/S2-03_running.png` | PENDING |
| S2-04 | REQUIRED | 6 | B2 | checkpoint 1 | runner banner | `SINGLE-RUN`, `c=1, 2, 4, 8` lines | `V2/baseline/raw/run-01/summary.json` | `V2/baseline/screenshots/S2-04_result.png` | PENDING |
| S2-05 | REQUIRED | 6, 50 | B2 | checkpoint 1 | Grafana link from the banner | per-replica panels, latency, CPU | `V2/baseline/metrics/run-01/prometheus-readback.json` | `V2/baseline/screenshots/S2-05_grafana.png` | PENDING |
| S2-06 | SUPPORTING | 6 | B2 | checkpoint 1 | `ev_replicas` | requests per pod | `summary.json` → `distribution` | `V2/baseline/screenshots/S2-06_replicas.png` | PENDING |
| S2-07 | REQUIRED | 6 | O2 | checkpoint 2 | `ev_route; ev_k get pods -l app.kubernetes.io/name=llmd-router-epp -o wide` | route via `llmd-router-epp`, `x-ewallet-backend: llm-d`, router pod Running | `V2/optimized/config/run-01.yaml`, `logs/run-01/router.log` | `V2/optimized/screenshots/S2-07_route.png` | PENDING |
| S2-08 | REQUIRED | 6 | O2 | checkpoint 2 | `ev_epp`; EPP log | 2 ready endpoints, decisions for both pods | `V2/optimized/raw/run-01/epp-metrics-*.txt`, `gateway-policy/optimized/epp.log` | `V2/optimized/screenshots/S2-08_epp.png` | PENDING |
| S2-09 | REQUIRED | 6, 50 | O2 | checkpoint 2 | banner, Grafana link, `ev_replicas` | `c=1, 2, 4, 8` lines, both pods served, EPP panels | `V2/optimized/raw/run-01/summary.json`, `prometheus-readback.json` | `V2/optimized/screenshots/S2-09_result.png`, `S2-09_grafana.png` | PENDING |
| S2-10 | SUPPORTING | 6 | O2 | O2 window | progress view + Grafana | `backend llm-d` rows | `V2/optimized/raw/run-01/raw_requests.jsonl` | `V2/optimized/screenshots/S2-10_running.png` | PENDING |
| S2-11 | REQUIRED | 6 | both | checkpoint 3 | comparison banner / `comparison.md` | `single-run`, `VALID`, observed outcome (`e2e_p95_ms`), failures, per-replica shares | `V2/comparison/metrics.json` | `V2/comparison/screenshots/S2-11_comparison.png` | PENDING |
| S2-12 | SUPPORTING | 3, 6 | both | checkpoint 3 | `json.tool V2/manifest.json` (filtered) | `profile: single`, `levels: 1,2,4,8`, `nodes: 2` | `V2/manifest.json` | `V2/comparison/screenshots/S2-12_manifest.png` | PENDING |
| S2-13 | REQUIRED | 6 | — | after `runner exit 0` | `ev_cleanup V2` | cleanup confirmed, production unchanged | `V2/safety/cleanup.txt` | `O/screenshots/S2-13_cleanup.png` | PENDING |

## Gateway policy (runbook §7, at the EXP-V2 checkpoints)

| ID | Kind | Rubric | Arm | When | Command | Must show | Raw artifact | File | Status |
|---|---|---|---|---|---|---|---|---|---|
| POL-1 | REQUIRED | 5 | B2 | checkpoint 1 | `ev_policy_valid` | `HTTP/1.1 200`, backend header, rate-limit headers | `V2/gateway-policy/baseline/results.json` `valid_client_key` | `V2/baseline/screenshots/POL-1_valid.png` | PENDING |
| POL-2 | REQUIRED | 5 | B2 | checkpoint 1 | `ev_policy_missing` | 401 | `missing_key` | `V2/baseline/screenshots/POL-2_missing.png` | PENDING |
| POL-3 | REQUIRED | 5 | B2 | checkpoint 1 | `ev_policy_wrong; ev_policy_backend_key` | 401 twice | `wrong_key`, `backend_key_as_client_key` | `V2/baseline/screenshots/POL-3_invalid.png` | PENDING |
| POL-4 | REQUIRED | 5 | B2 | checkpoint 1 | `ev_policy_burst 16` | 200s and 429s | `rate_limit_burst`, `gateway.log` | `V2/baseline/screenshots/POL-4_rate_limit.png` | PENDING |
| POL-5 | REQUIRED | 5, 6 | B2 and O2 | checkpoints 1 and 2 | `ev_policy_route` (+ `ev_epp` in O2) | `primary` / `llm-d` backend header; EPP requests +3 | `routing_backend_header` | `V2/<arm>/screenshots/POL-5_route.png` | PENDING |
| POL-6 | SUPPORTING | 5 | O2 | checkpoint 2, if the burst returned 503 | `cat V2/gateway-policy/optimized/attribution.json; ev_replicas` | `ATTRIBUTED`, vLLM queue cap relayed by the EPP | `attribution.json`, `counters/*.prom` | `V2/optimized/screenshots/POL-6_admission.png` | PENDING |
| POL-7 | REQUIRED | 5 | B2 | checkpoint 1 | `cat V2/gateway-policy/baseline/gateway-policy-config.yaml` | apiKey auth, 12/s rate limit, token budget, 160 s timeout | the same file | `V2/baseline/screenshots/POL-7_policy.png` | PENDING |
| Failover | — | 5 | — | — | — | NOT_EXECUTED: there is no fallback in the benchmark stack, on purpose. G1 holds the failover evidence | — | — | not applicable |

**Totals:** 36 IDs, plus the failover row. 26 are REQUIRED: 11 for EXP-V1, 9 for EXP-V2 and 6 for policy. POL-5 is captured once per arm.

## Historical: multi-run rehearsal of 2026-11-09

The unattended rehearsal ran with 3 runs per arm. EXP-V1 was `IMPROVED` and EXP-V2 was `NO_SIGNIFICANT_CHANGE`, and the same kinds of raw artifacts were written and passed the chain gate in both sessions. It shows what correct output looks like.

It is kept as engineering evidence in [benchmark_rehearsal_20261109](../../evidence/edai2/llm/benchmark_rehearsal_20261109/README.md). It is not the submission result, and its numbers are not single-run results. The screenshot IDs of that period (`V1-xx`, `V2-xx`) are replaced by `S1-xx`, `S2-xx`.
