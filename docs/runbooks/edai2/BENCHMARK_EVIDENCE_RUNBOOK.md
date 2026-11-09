# EXP-V1 / EXP-V2 owner evidence replay

This runbook is for the owner's own screenshot session. It replays the two CPU benchmarks on GKE in the isolated `ewallet-bench` namespace, stops at three checkpoints per experiment so that you can capture the screens yourself, and cleans up. Nothing here generates screenshots, and nothing touches production.

- Design and validation history: [BENCHMARK_REPLAY.md](../../edai2/llm/BENCHMARK_REPLAY.md).
- Comparison rules: [BENCHMARK_METHODOLOGY.md](../../edai2/llm/BENCHMARK_METHODOLOGY.md).
- Which screenshot backs which rubric row: [BENCHMARK_EVIDENCE_MATRIX.md](../../edai2/llm/BENCHMARK_EVIDENCE_MATRIX.md).
- Full formal rehearsals of both experiments, run unattended on 2026-11-09: [benchmark_rehearsal_20261109](../../evidence/edai2/llm/benchmark_rehearsal_20261109/README.md). Use them to see what correct output looks like.

## 0. Overview

| Item | Value |
|---|---|
| Worktree | `~/Develop/ewallet-data-ai-platform/.worktrees/claude-benchmark-evidence-replay` |
| Branch | `feature/llm-benchmark-evidence-replay`; `git status --short` must print nothing (the manifest records `source_sha` and `source_dirty`) |
| Cluster access | `~/.kube/ewallet-n15-dns-config` (DNS endpoint; the public IP endpoint times out from the laptop) |
| Sessions | Two bounded sessions, one after the other, never at the same time: EXP-V1 (1 node), then EXP-V2 (2 nodes) |
| Time | See §0.1. Every run starts vLLM fresh. |
| Cost | `plan` prints the worst case: USD 0.93 (EXP-V1) and USD 1.85 (EXP-V2). The session refuses to start above USD 5 |
| What protects the cluster | §8 |

### 0.1 Time budget

| Session | Unattended rehearsal (measured 2026-11-09) | Your replay, with 3 checkpoints | Limit |
|---|---|---|---|
| EXP-V1 | see the rehearsal README | rehearsal + about 15 min of checkpoint pauses | `EXP_MAX_MINUTES=215` |
| EXP-V2 | see the rehearsal README | rehearsal + about 15 min of checkpoint pauses | `EXP_MAX_MINUTES=215` |

The time a checkpoint waits for you counts against the 215-minute limit. Capture the screenshots and type `CONTINUE` within about 5 minutes per checkpoint. If a session reaches its limit, it stops, keeps its evidence, cleans up, and reports `INCONCLUSIVE`. In that case run the session again (§9); do not raise the limit.

### 0.2 Terminals

| Window | Runs | Section |
|---|---|---|
| **T1** | the runner. It prints the checkpoints and waits for `CONTINUE` | §3, §5 |
| **T2** | the display commands (`ev_*`) for screenshots | §3–§6 |
| **T3** | the live view of the running benchmark | §3, §5 |
| **Browser** | Grafana `http://127.0.0.1:13030` and Prometheus `http://127.0.0.1:13090`. The runner opens both port-forwards on the laptop. Grafana is anonymous and read-only. Nothing is exposed outside the laptop | §3–§5 |

## 1. Environment (T1, T2 and T3)

Paste this into each of the three terminals. It sets the same variables everywhere and creates one campaign directory:

```bash
cd ~/Develop/ewallet-data-ai-platform/.worktrees/claude-benchmark-evidence-replay
export KUBECONFIG=~/.kube/ewallet-n15-dns-config
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export VLLM_IMAGE=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/vllm-cpu-model@sha256:f695b23efa8145ae5cb57a1873863879927ab4805e70e5eee1a71a2063edd07c
export CAMPAIGN=docs/evidence/edai2/llm/owner_evidence_$(date -u +%Y%m%d)
source scripts/experiments/owner_evidence.sh     # ev_* display helpers; they only read
```

`CAMPAIGN` must be the same in all three terminals. If you open them on different days, set it by hand instead of using `date`.

## 2. Preflight (T2)

```bash
git status --short                                                   # nothing
git log --oneline -1                                                 # the commit you will cite
kubectl --context $KUBE_CONTEXT get nodes -l ewallet.io/bench=true   # "No resources found"
kubectl --context $KUBE_CONTEXT get ns ewallet-bench                 # NotFound
systemctl --user list-timers --all | grep ewallet-bench              # nothing
for p in $(pgrep -x kubectl); do tr '\0' ' ' </proc/$p/cmdline; echo; done | grep port-forward   # nothing
EXP_MAX_MINUTES=215 scripts/experiments/vllm_bench.sh exp-v1 plan /tmp/plan-v1; cat /tmp/plan-v1/plan/arm-difference.diff
EXP_MAX_MINUTES=215 scripts/experiments/vllm_bench.sh exp-v2 plan /tmp/plan-v2; cat /tmp/plan-v2/plan/arm-difference.diff
```

| ID | Capture | Must show |
|---|---|---|
| `PRE-1` → `$CAMPAIGN/screenshots/PRE-1_plan_cost.png` | both `plan` outputs | `fits: true`, `worst_case_usd` 0.93 and 1.85, `problems: []` |
| `PRE-2` → `$CAMPAIGN/screenshots/PRE-2_arm_difference.png` | both diffs | EXP-V1: only `VLLM_CPU_OMP_THREADS_BIND` `auto`→`0-1`. EXP-V2: only the gateway `baseUrl` and `x-ewallet-backend` |

Run `mkdir -p $CAMPAIGN/screenshots` first. Do **not** create `$CAMPAIGN/exp-v1` yourself: the runner refuses a directory that already exists.

## 3. EXP-V1 replay

**T1** starts the session:

```bash
EXP_APPROVED=yes EXP_CAPTURE_MODE=interactive EXP_CHECKPOINT_RUNS=first EXP_MAX_MINUTES=215 \
  scripts/experiments/vllm_bench.sh exp-v1 session $CAMPAIGN/exp-v1 2>&1 | tee $CAMPAIGN/exp-v1.terminal.log
```

**T3** starts the live view (start it right after T1; it waits for the directory):

```bash
until [ -d $CAMPAIGN/exp-v1 ]; do sleep 5; done; python3 -m ops.experiments.progress $CAMPAIGN/exp-v1
```

- **Run order:** baseline-01, optimized-01, optimized-02, baseline-02, baseline-03, optimized-03 (AB, BA, AB). Every run restarts vLLM and the gateway.
- **Checkpoints:** with `EXP_CHECKPOINT_RUNS=first`, T1 stops after baseline-01, after optimized-01, and at the comparison. Runs 02 and 03 continue on their own.
- **Failed requests:** a request that fails does not stop the session. The `auto` arm often loses `long-1` to the gateway's 160 s timeout. T1 then logs `measured; N of M requests failed and stay in the comparison`.
- **Grafana:** at a checkpoint, the banner prints a link for exactly that run's window (`from`, `to` and `var-run_id`). Leave the `experiment` and `run` selectors visible in the screenshot. For a run without a checkpoint (02, 03), pick it in the `run` selector and use its window from `$CAMPAIGN/exp-v1/<arm>/metrics/run-NN/window-start.txt` and `window-end.txt`.

Below, `$R` is the raw directory of the run being shown, for example `R=$CAMPAIGN/exp-v1/baseline/raw/run-01`.

| ID | Rubric row | When | Where and command | Must show | Hide | Corroborated by |
|---|---|---|---|---|---|---|
| `V1-01` provenance → `exp-v1/baseline/screenshots/baseline-01-4_pods_nodes.png` | 3, 4 | baseline-01 checkpoint | T2: `ev_provenance $CAMPAIGN/exp-v1/baseline/raw/run-01` | one `bench-pool` node; `vllm-primary` Running on it; `imageID …@sha256:f695b23e…`; `model_revision c1899de2…`; `run=baseline-01` | nothing | `baseline/raw/run-01/controls.json`, `manifest.json` |
| `V1-02` baseline config → `exp-v1/baseline/screenshots/baseline-01-1_config.png` | 6 | baseline-01 checkpoint | T2: `ev_config; ev_route` | `VLLM_CPU_OMP_THREADS_BIND=auto`, `OMP_NUM_THREADS=2`, `--max-num-seqs 4`, `--max-num-queued-reqs 8`, requests 2 CPU / 6Gi, limits 3 / 10Gi; route `vllm-primary…:8000`, `backendRequestTimeout: 160s` | nothing (keys are filtered out) | `baseline/config/run-01.yaml` |
| `V1-03` baseline running → `exp-v1/baseline/screenshots/baseline-01-0_running.png` | 6 | during baseline-01, after T1 prints `run 01 baseline measuring` (≈15 min after start) | T3 live view next to Grafana (time range "Last 15 minutes", `run` = `baseline-01`) | rows appearing with `c=1`, `http 200`, TTFT/E2E values; Grafana series rising | nothing | `baseline/raw/run-01/raw_requests.jsonl` |
| `V1-04` baseline result → `exp-v1/baseline/screenshots/baseline-01-2_terminal.png` | 6 | baseline-01 checkpoint | T1: the checkpoint block | `Thread binding: auto`, digest, `Window (UTC)`, one line per level `c=1, 2, 4` with tok/s, TTFT, E2E p95, `errors` | nothing | `baseline/raw/run-01/summary.json` |
| `V1-05` baseline Grafana → `exp-v1/baseline/screenshots/baseline-01-3_grafana.png` | 6, 50 | baseline-01 checkpoint | Browser: the printed link | rows A (tokens/s, TTFT, E2E, running/waiting, admission rejections = 0) and B (CPU, memory) for `baseline-01` only; time axis equal to the printed window | nothing | `baseline/metrics/run-01/prometheus-readback.json` |
| `V1-05b` targets → `exp-v1/baseline/screenshots/baseline-01-6_prometheus_targets.png` | 50 | baseline-01 checkpoint | T2: `ev_targets` (or browser `http://127.0.0.1:13090/targets`) | `vllm`, `agentgateway`, `pod-usage` all `up`; `run=baseline-01` on the vLLM target | nothing | `prometheus-readback.json` → `targets` |
| `V1-06` optimized config → `exp-v1/optimized/screenshots/optimized-01-1_config.png` | 6 | optimized-01 checkpoint | T2: `ev_config` | `VLLM_CPU_OMP_THREADS_BIND=0-1`; everything else as in V1-02 | nothing | `optimized/config/run-01.yaml`; `diff` with the baseline config shows one line |
| `V1-07` optimized running → `exp-v1/optimized/screenshots/optimized-01-0_running.png` | 6 | during optimized-01 | T3 + Grafana `run` = `optimized-01` | as V1-03 | nothing | `optimized/raw/run-01/raw_requests.jsonl` |
| `V1-08` optimized result and Grafana → `exp-v1/optimized/screenshots/optimized-01-2_terminal.png`, `optimized-01-3_grafana.png` | 6, 50 | optimized-01 checkpoint | T1 block; browser link | as V1-04/V1-05 with `Thread binding: 0-1`, same node | nothing | `optimized/raw/run-01/summary.json`, `prometheus-readback.json` |
| `V1-09` comparison → `exp-v1/comparison/screenshots/comparison-1_verdict.png` | 6 | comparison checkpoint | T1: the COMPARISON block (`cat $CAMPAIGN/exp-v1/comparison/comparison.md` shows the same) | `Runs: 3 baseline, 3 optimized`, the verdict and its reasons, the failure table (failed / requests per level and arm), the metric table | nothing | `comparison/metrics.json`, `comparison.csv` |
| `V1-10` manifest → `exp-v1/comparison/screenshots/comparison-2_manifest.png` | 3, 6 | comparison checkpoint | T2: `python3 -m json.tool $CAMPAIGN/exp-v1/manifest.json \| head -60` | `source_sha`, `source_dirty: false`, image digests, `deviations` (lists every run with failed requests) | nothing | `manifest.json` |
| `V1-11` whole session in Grafana → `exp-v1/comparison/screenshots/comparison-3_grafana_session.png` | 6, 50 | comparison checkpoint | Browser: Grafana, `run` = All, time range = session start to now | six runs, each with its own `run_id` series | nothing | `observability/metric-readback/*.json` |
| `V1-12` cleanup → `$CAMPAIGN/screenshots/V1-12_cleanup.png` | 6 | after T1 prints `session end status=0` | T2: `ev_cleanup $CAMPAIGN/exp-v1` | `status=0`; `pool and namespace confirmed absent; no bench VMs or disks`; only `cpu-pool`; `bench VMs: 0`, `bench disks: 0`; `NotFound`; `production unchanged`; `armed bench timers: 0` | nothing | `safety/cleanup.txt`, `production-before.txt` / `production-after.txt` |

After each checkpoint's screenshots, type `CONTINUE` in T1. `ABORT` stops the session and cleans up.

## 4. Between the sessions

Start EXP-V2 only when all of these hold:

- V1-12 is complete: `status=0`, cleanup confirmed, production unchanged.
- T3 has ended. The live view stops by itself when the manifest is finished.
- `kubectl --context $KUBE_CONTEXT get nodes -l ewallet.io/bench=true` prints nothing.

Nothing stays running between the two sessions.

## 5. EXP-V2 replay

**T1:**

```bash
EXP_APPROVED=yes EXP_CAPTURE_MODE=interactive EXP_CHECKPOINT_RUNS=first EXP_MAX_MINUTES=215 \
  scripts/experiments/vllm_bench.sh exp-v2 session $CAMPAIGN/exp-v2 2>&1 | tee $CAMPAIGN/exp-v2.terminal.log
```

**T3:**

```bash
until [ -d $CAMPAIGN/exp-v2 ]; do sleep 5; done; python3 -m ops.experiments.progress $CAMPAIGN/exp-v2
```

- **Arms:** B2 sends traffic through the Kubernetes Service; O2 sends it through the llm-d router (Envoy + EPP).
- **Replicas:** both arms run two vLLM replicas, one per node, with `0-1` thread binding.
- **Levels:** 1, 2, 4 and 8.
- **Router install:** the router is installed for each O2 run and removed for each B2 run.

| ID | Rubric row | When | Where and command | Must show | Hide | Corroborated by |
|---|---|---|---|---|---|---|
| `V2-01` environment and two replicas → `exp-v2/baseline/screenshots/baseline-01-4_pods_nodes.png` | 3, 6 | baseline-01 checkpoint | T2: `ev_provenance $CAMPAIGN/exp-v2/baseline/raw/run-01` | two `bench-pool` nodes; two `vllm-primary` pods, one per node; digest `f695b23e…`; revision `c1899de2…`; `replicas 2` | nothing | `baseline/raw/run-01/controls.json` |
| `V2-02` B2 routing → `exp-v2/baseline/screenshots/baseline-01-8_route.png` | 6 | baseline-01 checkpoint | T2: `ev_route; ev_config` | `baseUrl` → `vllm-primary.ewallet-bench.svc…:8000`, `x-ewallet-backend: primary`; `VLLM_CPU_OMP_THREADS_BIND=0-1`, `replicas=2` | nothing | `baseline/config/run-01.yaml`, `baseline/logs/run-01/gateway-config.yaml` |
| `V2-03` B2 live benchmark → `exp-v2/baseline/screenshots/baseline-01-0_running.png` | 6 | during baseline-01 | T3 + Grafana `run` = `baseline-01`, last 15 min | rows with `backend primary`, levels progressing to `c=8` | nothing | `baseline/raw/run-01/raw_requests.jsonl` |
| `V2-04` B2 result and Grafana → `exp-v2/baseline/screenshots/baseline-01-2_terminal.png`, `baseline-01-3_grafana.png` | 6, 50 | baseline-01 checkpoint | T1 block; browser link | four level lines (1, 2, 4, 8); panels "Completed requests per replica" and "Share of completed requests per replica" with two pods | nothing | `summary.json` (`distribution.per_pod`), `prometheus-readback.json` |
| `V2-05` B2 per-replica counters → `exp-v2/baseline/screenshots/baseline-01-5_replicas.png` | 6 | baseline-01 checkpoint | T2: `ev_replicas` | `vllm:request_success_total` for each of the two pods | nothing | `summary.json` → `levels.*.distribution` |
| `V2-06` O2 configuration → `exp-v2/optimized/screenshots/optimized-01-8_route.png` | 6 | optimized-01 checkpoint | T2: `ev_route; ev_k get pods -l app.kubernetes.io/name=llmd-router-epp -o wide` | `baseUrl` → `llmd-router-epp.ewallet-bench.svc…:8081`, `x-ewallet-backend: llm-d`; the router pod (2/2: `epp` + `envoy-proxy`) Running | nothing | `optimized/config/run-01.yaml`, `optimized/logs/run-01/router.log` |
| `V2-07` EPP endpoints and decisions → `exp-v2/optimized/screenshots/optimized-01-6_epp.png`, `optimized-01-7_epp_log.png` | 6 | optimized-01 checkpoint | T2: `ev_epp`; browser row C | `llm_d_epp_ready_endpoints 2`; `llm_d_epp_scheduler_attempts_total` for **both** endpoints with `status="success"`; log lines `Request handled -> <pod IP>:8000` for both IPs | nothing | `optimized/raw/run-01/epp-metrics-before.txt` / `-after.txt`, `prometheus-readback.json` |
| `V2-08` O2 live benchmark → `exp-v2/optimized/screenshots/optimized-01-0_running.png` | 6 | during optimized-01 | T3 + Grafana `run` = `optimized-01` | rows with `backend llm-d` | nothing | `optimized/raw/run-01/raw_requests.jsonl` |
| `V2-09` O2 result, Grafana, distribution → `exp-v2/optimized/screenshots/optimized-01-2_terminal.png`, `optimized-01-3_grafana.png`, `optimized-01-5_replicas.png` | 6, 50 | optimized-01 checkpoint | T1 block; browser link; T2 `ev_replicas` | four level lines; both pods with completed requests; EPP panels with data | nothing | `summary.json`, `prometheus-readback.json` |
| `V2-10` comparison → `exp-v2/comparison/screenshots/comparison-1_verdict.png`, `comparison-2_manifest.png`, `comparison-3_grafana_session.png` | 6 | comparison checkpoint | as V1-09 to V1-11 | `Runs: 3 baseline, 3 optimized`; verdict; failure table for levels 1, 2, 4, 8 | nothing | `comparison/metrics.json`, `manifest.json` |
| `V2-11` cleanup → `$CAMPAIGN/screenshots/V2-11_cleanup.png` | 6 | after `session end status=0` | T2: `ev_cleanup $CAMPAIGN/exp-v2` | as V1-12 | nothing | `exp-v2/safety/cleanup.txt` |

## 6. Gateway policy evidence

**What the runner already does.** After the measured window of run-01 in each arm, the runner runs an automatic probe and writes these files to `<session>/gateway-policy/<arm>/`:
- `results.json`, `probe.log`, `attribution.json`;
- `gateway.log`;
- in the EXP-V2 llm-d arm, also `epp.log` and `envoy.log`.

**When to run the manual sequence.** Run it in T2 during the **EXP-V2 baseline-01 checkpoint** (route `primary`), and again at the **optimized-01 checkpoint** (route `llm-d`). At a checkpoint the measured window has ended, and the next run restarts every pod, so these requests cannot enter any benchmark window. Each case sends one-token requests through the runner's port-forward (`127.0.0.1:13300`).

**How keys are handled.** The helpers read each key from its Secret into a private header file (mode 600) and delete it afterwards, so no key appears in the terminal or the process list. Do not run them with `set -x`.

| ID | Case | T2 command | Must show | Raw artifact |
|---|---|---|---|---|
| `POL-1` → `exp-v2/<arm>/screenshots/<arm>-01-P1_valid.png` | authenticated request | `ev_policy_valid` | `HTTP/1.1 200`, `x-ewallet-backend: primary` (or `llm-d`), `x-ratelimit-*` headers | `gateway-policy/<arm>/results.json` case `valid_client_key` |
| `POL-2` → `…-P2_missing.png` | no API key | `ev_policy_missing` | `missing key -> HTTP 401` | case `missing_key` |
| `POL-3` → `…-P3_invalid.png` | invalid key, and the vLLM backend key used as a client key | `ev_policy_wrong; ev_policy_backend_key` | `HTTP 401` twice | cases `wrong_key`, `backend_key_as_client_key` |
| `POL-4` → `…-P4_rate_limit.png` | rate limit 12 requests/s | `ev_policy_burst 16` | a count of `200` and a count of `429` (in the llm-d arm possibly also `503`, see POL-6) | case `rate_limit_burst`, `gateway.log` (`http.status=429`, `listener=llm`) |
| `POL-5` → `…-P5_route.png` | routing | `ev_policy_route` | three lines `HTTP/1.1 200 … x-ewallet-backend: primary` (B2) or `llm-d` (O2); in O2, `ev_epp` before and after shows `llm_d_epp_request_total` +3 | case `routing_backend_header` (`epp_request_total_delta`) |
| `POL-6` → `…-P6_admission.png` | admission rejection, when the burst returned 503 | `cat $CAMPAIGN/exp-v2/gateway-policy/<arm>/attribution.json; ev_replicas` | `"verdict": "ATTRIBUTED"`, component `vLLM admission control (max_num_queued_reqs)`; `vllm:admission_rejections_total{reason="max_num_queued_reqs"}` > 0 on one pod | `attribution.json`, `counters/*.prom`, [analysis/llmd-503](../../evidence/edai2/llm/benchmark_reproduction_20261109/analysis/llmd-503/README.md) |
| `POL-7` → `…-P7_policy_config.png` | the policy itself | `cat $CAMPAIGN/exp-v2/gateway-policy/<arm>/gateway-policy-config.yaml` | `apiKey` auth (keys removed), `localRateLimit` 12/s and the token budget, `backendRequestTimeout 160s` | the same file |

Why POL-6 happens: the burst repeats one prompt. With one replica (EXP-V1), or with the EPP's prefix affinity (EXP-V2 O2), it reaches one pod, and vLLM `--max-num-queued-reqs 8` answers the 9th to 12th requests with 503. The measured workload never has more than 8 requests in flight, so it cannot trigger this.

**Failover: NOT_EXECUTED.** The benchmark stack has no fallback backend, on purpose: a fallback would hide failed requests. Failover was shown in G1; see [BENCHMARK_REPLAY.md §6](../../edai2/llm/BENCHMARK_REPLAY.md#6-gateway-policy-evidence).

## 7. After both sessions

1. Write the evidence checksums: `(cd $CAMPAIGN && find . -type f ! -name sha256sums.txt -print0 | sort -z | xargs -0 sha256sum > sha256sums.txt)`.
2. Check every row of [BENCHMARK_EVIDENCE_MATRIX.md](../../edai2/llm/BENCHMARK_EVIDENCE_MATRIX.md): each screenshot file exists and shows what its row requires.
3. Take numbers only from `comparison/metrics.json`, never from Grafana.
4. Link screenshots from the reports only once the files exist.

## 8. What protects the cluster while you capture

- **Checkpoint timeout.** Each checkpoint waits at most `EXP_CHECKPOINT_MINUTES` (default 20), and never past the time limit. When the wait ends, the runner stops and cleans up. `ABORT` or Ctrl-C does the same.
- **Host loss.** If the laptop sleeps or dies, an in-cluster watchdog (Role limited to `ewallet-bench`) deletes the benchmark workloads at the time limit + 10 min. The autoscaled `bench-pool` (min 0) then drains to zero nodes. A systemd user timer on the laptop also runs `down` at the limit + 20 min.
- **Cost ceiling.** The cost plan refuses to start a session whose worst case exceeds USD 5 or whose window exceeds 240 min.

## 9. Recovery after an interrupted session

A session **cannot be resumed**. The comparison needs all runs of one session, on the same nodes, under one source commit, and the runner never mixes sessions.

1. If T1 is still running, type `ABORT` or press Ctrl-C. The runner cleans up.
2. If T1 or the laptop died, run `scripts/experiments/vllm_bench.sh exp-v1 down /tmp/down-$(date +%s)`. Use `exp-v2` if that was the experiment. It removes only objects that carry the ownership marks.
3. Run `ev_cleanup $CAMPAIGN/exp-v1`, or check §3 V1-12 by hand. If cleanup is not confirmed, stop and fix that first.
4. Keep the interrupted directory as a failed attempt: rename it to `exp-v1-attempt1`, and do not delete it.
5. Start a new session into a new directory (`$CAMPAIGN/exp-v1`). Screenshots from the interrupted attempt cannot be combined with the new runs.

## 10. Options

| Variable | Default | Meaning |
|---|---|---|
| `EXP_CAPTURE_MODE` | `unattended` | `interactive` stops at checkpoints (owner replay) |
| `EXP_CHECKPOINT_RUNS` | `all` | `first`: stop only after run 01 of each arm and at the comparison (use this) |
| `EXP_CHECKPOINT_MINUTES` | 20 | longest wait per checkpoint (1–45) |
| `EXP_RUNS` | 3 | runs per arm (2–5); below 3 the verdict is `INCONCLUSIVE` |
| `EXP_PROFILE` | `formal` | `smoke`: one run per arm, fewer levels; never a result |
| `EXP_MAX_MINUTES` | 210 | time limit; at most 215, so setup + limit + teardown fits 4 h |
| `EXP_MAX_USD` | 5 | worst-case ceiling |
| `EXP_LAPTOP_TIMER` | `auto` | `off` disables the second cleanup path |
| `EXP_POLICY_PROBE` | `on` | `off` skips the automatic gateway probe |

`scripts/experiments/vllm_bench.sh exp-v1 drill <dir>` repeats the cleanup drill: one node, one idle pod, and a watchdog with a 4-minute deadline. It passes when the cluster has removed the node by itself.
