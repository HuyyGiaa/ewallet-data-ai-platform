# Formal rehearsal of EXP-V1 and EXP-V2 (GKE, 2026-11-09)

> **Status: historical engineering evidence (multi-run, 3 runs per arm).** The EDAI-2 submission uses the single-run methodology (`EXP_PROFILE=single`, [BENCHMARK_METHODOLOGY.md](../../../../edai2/llm/BENCHMARK_METHODOLOGY.md)). The numbers on this page are not single-run results, and they are not reused as single-run results. Raw files in this directory are unchanged. Later on 2026-11-09 only this README's wording was corrected: the arm values are medians, not means, and EXP-V2's decision metric is e2e p95.

Both formal benchmark sessions were run once, unattended, on GKE, before the owner's own screenshot replay. The goal was to show that the full workflow completes under its limits and to record what correct output looks like.

- **What this is:** real GKE runs; every number below comes from this directory.
- **What this is not:** owner evidence. Nobody captured screenshots here, and no screenshot was generated. The owner replays both sessions with the [runbook](../../../../runbooks/edai2/BENCHMARK_EVIDENCE_RUNBOOK.md).
- Historical O1/G2 figures are not reused here.

## 1. Sessions

| Session | Source commit | Started (UTC) | Ended (UTC) | Duration | Nodes | Runs | Gate |
|---|---|---|---|---|---|---|---|
| EXP-V1 attempt 1 | `ba762ec9` | 07:50:17 | 09:26:53 (killed) | ~97 min | 1 | 3 of 6 complete | not gated; preserved in `exp-v1-attempt1/` |
| EXP-V1 | `3250424d` | 09:31:47 | 12:08:04 | 156.3 min | 1 | 6 of 6 | PASS (`chain-gate-exp-v1.json`) |
| EXP-V2 | `10b017cb` | 12:08:58 | 14:32:26 | 143.5 min | 2 | 6 of 6 | PASS (`chain-gate-exp-v2.json`) |

- **Run order:** both sessions ran baseline-01, optimized-01, optimized-02, baseline-02, baseline-03, optimized-03 (AB, BA, AB).
- **Mode:** `EXP_CAPTURE_MODE=unattended`, `EXP_MAX_MINUTES=215`, gateway `backendRequestTimeout 160s`.
- **How sessions were chained:** a systemd user unit, [bench_chain.sh](../../../../../scripts/experiments/bench_chain.sh), waited for EXP-V1 to end. It started EXP-V2 only after these checks passed:
  - the evidence gate, [chain_gate.py](../../../../../ops/experiments/chain_gate.py);
  - live cloud reads showing the benchmark was gone and production unchanged;
  - quota;
  - the EXP-V2 cost plan.

  Its log is `chain.log`.
- **Attempt 1:** EXP-V1 attempt 1 was started as a child of the editor session and died when the editor closed. Its resources were removed with `down` (`exp-v1-attempt1-down.log`). Its runs are **not** combined with the formal session.

## 2. EXP-V1: `VLLM_CPU_OMP_THREADS_BIND` `auto` versus `0-1`

**Verdict:** `IMPROVED` (`exp-v1/comparison/comparison.md`). The arm values below are medians of the 3 runs, as the multi-run comparator reports them.
- No request failed: 0 of 36 per level, in each arm.
- `deviations` is empty.
- The `0-1` arm uses more CPU. At c=1, the median over the runs of each run's mean vLLM CPU was 2.24 cores against 1.23.

| Level | Baseline tok/s (median) | Optimized tok/s (median) | Change | TTFT p50 baseline → optimized (median) | E2E p50 baseline → optimized (median) |
|---|---:|---:|---:|---|---|
| 1 | 1.492 | 2.786 | +86.7 % | 7.71 s → 4.19 s | 14.60 s → 8.52 s |
| 2 | 4.101 | 7.108 | +73.3 % | 8.45 s → 4.54 s | 28.97 s → 16.46 s |
| 4 | 4.257 | 7.507 | +76.4 % | 11.57 s → 6.40 s | 50.22 s → 28.73 s |

| Run | Window (UTC) | c=1 tok/s | c=2 tok/s | c=4 tok/s | c=1 TTFT p50 s | c=2 TTFT p50 s | c=4 TTFT p50 s | Failed | Targets up |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| baseline-01 | 09:48–10:04 | 1.51 | 4.10 | 4.26 | 7.6 | 8.4 | 11.3 | 0/36 | 3/3 |
| optimized-01 | 10:16–10:24 | 2.79 | 6.64 | 7.50 | 4.1 | 4.5 | 6.4 | 0/36 | 3/3 |
| optimized-02 | 10:37–10:45 | 2.77 | 7.11 | 7.51 | 4.2 | 4.5 | 6.3 | 0/36 | 3/3 |
| baseline-02 | 10:59–11:15 | 1.49 | 4.11 | 4.26 | 7.7 | 8.3 | 11.6 | 0/36 | 3/3 |
| baseline-03 | 11:28–11:44 | 1.47 | 3.95 | 4.19 | 7.8 | 8.4 | 14.2 | 0/36 | 3/3 |
| optimized-03 | 11:56–12:05 | 2.81 | 7.13 | 7.67 | 4.2 | 4.5 | 7.6 | 0/36 | 3/3 |

**Run timings:**
- Start to Ready: 11.0–14.1 min per run; the first run includes the image pull.
- Measured window: about 16 min for `auto` and about 8.5 min for `0-1`.

## 3. EXP-V2: Kubernetes Service (B2) versus llm-d EPP (O2), 2 replicas, `0-1`

**Verdict:** `NO_SIGNIFICANT_CHANGE` (`exp-v2/comparison/comparison.md`): no level changed beyond the threshold and the run-to-run spread.
- No request failed: 0 of 36 per level, in each arm.
- `deviations` is empty.

The decision metric for EXP-V2 is `e2e_p95_ms`. Its arm medians improve by +33.9 % at c=4 and +14.0 % at c=8. The tok/s medians improve by +50.5 % at c=4 and +16.3 % at c=8. In every case the per-run values of the two arms overlap:
- baseline c=4 ranged from 6.92 to 10.88 tok/s, and its e2e p95 from 48.1 to 102.9 s;
- optimized c=4 ranged from 9.17 to 12.83 tok/s, and its e2e p95 from 43.5 to 69.0 s.

The comparator therefore reports no significant change. This is the reported result. It was not re-run to look for a win.

| Level | Baseline tok/s (median) | Optimized tok/s (median) | Change | Within spread | TTFT p50 baseline → optimized (median) |
|---|---:|---:|---:|---|---|
| 1 | 2.737 | 2.654 | −3.0 % | yes | 4.19 s → 4.88 s |
| 2 | 6.983 | 7.609 | +9.0 % | yes | 4.18 s → 4.44 s |
| 4 | 7.988 | 12.025 | +50.5 % | yes | 5.02 s → 4.62 s |
| 8 | 10.773 | 12.527 | +16.3 % | yes | 12.90 s → 11.27 s |

| Run | Window (UTC) | c=1 tok/s | c=2 tok/s | c=4 tok/s | c=8 tok/s | c=1 TTFT p50 s | c=2 TTFT p50 s | c=4 TTFT p50 s | c=8 TTFT p50 s | Failed | Targets up |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| baseline-01 | 12:23–12:35 | 2.81 | 2.93 | 7.99 | 12.25 | 4.2 | 8.5 | 7.4 | 8.5 | 0/48 | 4/4 |
| optimized-01 | 12:48–12:58 | 2.65 | 7.65 | 12.03 | 12.53 | 4.9 | 4.5 | 4.6 | 9.4 | 0/48 | 5/5 |
| optimized-02 | 13:11–13:21 | 2.72 | 7.61 | 12.83 | 14.01 | 4.2 | 4.2 | 4.8 | 11.3 | 0/48 | 5/5 |
| baseline-02 | 13:33–13:44 | 2.74 | 6.98 | 6.92 | 8.62 | 4.2 | 4.2 | 5.0 | 22.6 | 0/48 | 4/4 |
| baseline-03 | 13:55–14:06 | 2.43 | 7.51 | 10.88 | 10.77 | 4.8 | 3.8 | 4.9 | 12.9 | 0/48 | 4/4 |
| optimized-03 | 14:18–14:29 | 2.59 | 7.49 | 9.17 | 8.85 | 5.5 | 4.4 | 4.6 | 12.7 | 0/48 | 5/5 |

**Run timings:**
- Start to Ready: 11.0–11.8 min per run.
- Measured window: 10–12.5 min.
- The O2 arm has one extra scrape target (the EPP).

## 4. Gateway policy probe

The probe runs once per arm, after run-01's measured window.

| Case | V1 baseline | V1 optimized | V2 B2 | V2 O2 |
|---|---|---|---|---|
| valid key | 200 | 200 | 200 | 200 |
| missing key | 401 | 401 | 401 | 401 |
| invalid key | 401 | 401 | 401 | 401 |
| backend key used as client key | 401 | 401 | 401 | 401 |
| routing header | PASS | PASS | PASS (`primary`) | PASS (`llm-d`) |
| burst of 16 | 8×200, 4×429, 4×503 | 8×200, 4×429, 4×503 | 12×200, 4×429 | 8×200, 4×429, 4×503 |
| 503 attribution | vLLM queue cap | vLLM queue cap | no 503 | vLLM queue cap, relayed by the EPP |
| failover | NOT_EXECUTED | NOT_EXECUTED | NOT_EXECUTED | NOT_EXECUTED |

- **Why failover is NOT_EXECUTED:** the benchmark stack has no fallback backend, on purpose.
- **Where B2 and O2 differ in the burst.** The 12 burst requests that passed the rate limit were spread differently between the two pods.

  | Arm | Pod 1 | Pod 2 | 503s |
  |---|---:|---:|---:|
  | B2 (Service) | 11 | 5 | 0 |
  | O2 (EPP) | 12 | 0 | 4 |

  In O2 the 4 requests above vLLM's `--max-num-queued-reqs 8` were rejected with 503. The burst repeats one prompt, and the EPP's `prefix-cache-scorer` (weight 3) prefers the pod that already holds that prefix.

  This is one burst per arm, not a measured result. The measured workload never has more than 8 requests in flight, and no measured request returned 503.
- **Correction to the O2 attribution.** The runner wrote `exp-v2/gateway-policy/optimized/attribution.json` = `UNATTRIBUTED`. The counters behind it showed two things:
  - vLLM `admission_rejections_total{reason="max_num_queued_reqs"}` +4 on pod `d8tz6`;
  - EPP `llm_d_epp_request_error_total{error_code="ModelServerError"}` +4.

  These are the same 4 requests. The EPP counts the backend's 503 that it relays. The old rule required zero EPP errors, so it reported UNATTRIBUTED. The rule now accepts EPP errors that are only `ModelServerError` and equal the vLLM rejections, and it has a test.

  The four probes were re-analysed from the stored `counters/` snapshots into `analysis/probe-attribution/`. The original files written by the runner are unchanged.

## 5. Observability readback

- Every run has `metrics/run-NN/prometheus-readback.json`. All scrape targets were `up` in all 12 runs: 3/3 in EXP-V1, 4/4 for B2 and 5/5 for O2.
- `observability/metric-readback/*.json` holds the per-run PromQL readback.
- `observability/dashboard/` holds the provisioned dashboard `llm-cpu-benchmark.json`, including the admission-rejection panel.
- Grafana itself was not captured. That is the owner's screenshot.

## 6. Safety, cleanup and cost

- **Cleanup.** Each session's `safety/cleanup.txt` reads `pool and namespace confirmed absent; no bench VMs or disks`.
- **Laptop timer.** The laptop `down` timer was disarmed by each session.
- **Watchdog.** The in-cluster watchdog never had to act.
- **Gate checks.** The chain re-read the cloud after each session (`chain.log`):
  - only `cpu-pool`;
  - namespace `ewallet-bench` NotFound;
  - no bench VMs or disks;
  - no bench timer or port-forward;
  - `helm list -A` identical to `production-before.txt`.
- **Independent check, 15:32Z.** Read again by hand after the chain finished: same results, with `llm-agent` still at revision 42.
- **Production.** `production-before.txt` equals `production-after.txt` in both sessions. No production Helm, Agent, ModelConfig or MLOps object was changed. No GPU, public load balancer or cluster-wide RBAC was used.
- **Quota.** `CPUS_ALL_REGIONS` was at 20 of 32 before EXP-V2.

**Cost.** These are estimates from list prices (e2-standard-4 at USD 0.1778 per node-hour in asia-southeast1). They are **not a billing readback**.

| Item | Node time | Estimate |
|---|---|---|
| EXP-V1 smoke (earlier, `benchmark_reproduction_20261109`) | — | ~0.16 |
| EXP-V1 attempt 1 | 1 node × ~97 min | ~0.31 |
| EXP-V1 | 1 node × ~155 min | ~0.46 |
| EXP-V2 | 2 nodes × ~142 min | ~0.84 |
| **Campaign** | | **~1.8**, under the USD 5 ceiling; the planned worst case was 3.09 |

## 7. Files

| Path | Content |
|---|---|
| `exp-v1/`, `exp-v2/` | the full session directories: `manifest.json`, `session.log`, `setup.yaml`, per-arm `config/`, `raw/` and `metrics/`, `comparison/`, `gateway-policy/`, `observability/`, `safety/`, `production-*.txt` |
| `exp-v1.terminal.log`, `exp-v2.terminal.log` | the runner's terminal output and the `runner exit` line |
| `chain.log`, `chain-gate-exp-v1.json`, `chain-gate-exp-v2.json` | the chain and its two gate reports |
| `exp-v1-attempt1/`, `exp-v1-attempt1*.log` | the interrupted attempt and its cleanup |
| `analysis/probe-attribution/` | the re-analysed probe attribution (§4) |
| `sha256sums.txt` | checksums of every file here |

`source_dirty` is `true` in both manifests, because each session writes its evidence inside the worktree. The source files themselves were unchanged: the chain refuses to start EXP-V2 with any change outside this directory.
