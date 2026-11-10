# EXP-V1 / EXP-V2 owner evidence session (single-run)

This runbook is for the owner's own benchmark and screenshot session, the evidence used in the EDAI-2 submission. It runs the two CPU benchmarks on GKE in the isolated `ewallet-bench` namespace:

- **one run per arm, at every original concurrency level and with every original request;**
- inside `tmux`, with three checkpoints per experiment where you capture the screens yourself;
- then it cleans up.

Nothing here generates screenshots, and nothing touches production.

- Methodology and why one run per arm is used: [BENCHMARK_METHODOLOGY.md](../../edai2/llm/BENCHMARK_METHODOLOGY.md#single-run-comparison-selected-for-the-edai-2-submission).
- Which screenshot backs which rubric row: [BENCHMARK_EVIDENCE_MATRIX.md](../../edai2/llm/BENCHMARK_EVIDENCE_MATRIX.md).
- Design and validation history: [BENCHMARK_REPLAY.md](../../edai2/llm/BENCHMARK_REPLAY.md).

Status: the owner ran this procedure on 2026-11-09/10 (`SINGLE_RUN_RESULTS=COMPLETE`, `OWNER_SCREENSHOTS=INTEGRATED`): [results](../../edai2/llm/BENCHMARK_RESULTS.md), [evidence matrix](../../edai2/llm/BENCHMARK_EVIDENCE_MATRIX.md). The steps below stay as the replay procedure. As executed, some screens were combined or captured with equivalent commands; the matrix records what each file shows.

## 0. Overview

### 0.1 Three kinds of benchmark run: do not mix them up

| | **Single-run (this runbook, official)** | Multi-run rehearsal (historical) | Smoke (workflow check) |
|---|---|---|---|
| Profile | `EXP_PROFILE=single` | `EXP_PROFILE=formal` (default) | `EXP_PROFILE=smoke` |
| Runs per arm | 1 | 3 | 1 |
| Levels V1 / V2 | 1,2,4 / 1,2,4,8 | 1,2,4 / 1,2,4,8 | 1 / 1,4 |
| Comparison | observed differences, `OBSERVED_*` verdicts, no statistics | medians, run-to-run spread, `IMPROVED` / `NO_SIGNIFICANT_CHANGE` … | always `INCONCLUSIVE` |
| How it starts | `scripts/experiments/owner_single_run.sh` | `vllm_bench.sh` with `EXP_RUNS=3` | `vllm_bench.sh` with `EXP_PROFILE=smoke` |
| Evidence | `docs/evidence/edai2/llm/owner_single_run_<date>/` | [benchmark_rehearsal_20261109](../../evidence/edai2/llm/benchmark_rehearsal_20261109/README.md) | `benchmark_reproduction_20261109/` |
| Used in the submission | **yes** | no, kept as engineering evidence | no |

**Why one run per arm is enough here.** The EDAI-2 rubric asks for a deployed inference platform, a benchmark and an optimization. It does not ask for repeated measurements. One full run per arm, at every level and with every request, shows the deployment, the measurement, the optimization and the monitoring.

**What one run cannot show.** One run per arm has no run-to-run spread. The result is an observed difference between two runs:
- no confidence interval and no significance test;
- the order is fixed (baseline, then optimized), so slow drift between the two runs cannot be told apart from the effect.

Write it up as an observation. Do not call it a proven or stable improvement.

**You cannot start a three-run session by mistake.** `owner_single_run.sh` always sets `EXP_PROFILE=single EXP_RUNS=1`, whatever your shell holds. Every banner says `SINGLE-RUN`, and the first log line says `profile=single methodology=single-run … runs=1`.

| Item | Value |
|---|---|
| Worktree | `~/Develop/ewallet-data-ai-platform/.worktrees/claude-benchmark-evidence-replay` |
| Branch | `feature/llm-benchmark-evidence-replay`. `git status --short` must print nothing before EXP-V1. The manifest records `source_sha` |
| Cluster access | `~/.kube/ewallet-n15-dns-config` (the DNS endpoint; the public IP endpoint times out from the laptop) |
| Sessions | Two, one after the other, never at the same time: EXP-V1 (1 node), then EXP-V2 (2 nodes) |
| What protects the cluster | §11 |

### 0.2 Time and cost

Estimates from the 2026-11-09 rehearsal timings. One run per arm keeps every level, so each arm takes as long as one rehearsal run.

| Session | Setup | Baseline run | Optimized run | Compare + cleanup | Unattended total | With your 3 checkpoints |
|---|---|---|---|---|---|---|
| EXP-V1 | 2.5 min | ≈31 min (vLLM Ready ≈14 min with the first image pull, window ≈16 min `auto`) | ≈21 min (Ready ≈11 min, window ≈8.5 min `0-1`) | ≈3 min | **≈57 min** | ≈70–75 min |
| EXP-V2 | 2.5 min | ≈25.5 min (Ready ≈12 min, window ≈12 min) | ≈23 min (Ready ≈12 min, window ≈10 min) | ≈3.5 min | **≈55 min** | ≈70–75 min |

Plan for about **2.5 hours** for both sessions, including the pause between them.

**Time limit.** Each session keeps the runner's limit `EXP_MAX_MINUTES=215`. That leaves room for checkpoints of up to 20 min each (`EXP_CHECKPOINT_MINUTES`), and nothing needs to be raised.

**Cost.**
- `plan` prints the worst case for the full 215-minute window: USD 0.93 (EXP-V1) and USD 1.85 (EXP-V2).
- The expected cost is far lower, about USD 0.2 and USD 0.4: e2-standard-4 at USD 0.1778 per node-hour.
- These are list-price estimates, not billing.
- The session refuses to start above USD 5.

### 0.3 Windows

Everything runs in one tmux session called `bench`, with three windows. Grafana opens in your browser.

| Window | What runs there | Keys |
|---|---|---|
| tmux window **0 `run`** | the runner; it prints the checkpoints and waits for `CONTINUE` | `Ctrl-b 0` |
| tmux window **1 `show`** | the `ev_*` display commands for screenshots | `Ctrl-b 1` |
| tmux window **2 `live`** | the live progress view | `Ctrl-b 2` |
| **Browser** | Grafana `http://127.0.0.1:13030`, Prometheus `http://127.0.0.1:13090`. The runner opens both port-forwards on this laptop; Grafana is anonymous and read-only | — |

## 1. Before you start

1. **Power.** Plug in the charger. Keep the laptop open and awake for the whole session.
2. **Do not log out, shut down or restart** while a session runs.
   - This host has `Linger=no`, so logging out stops your systemd user manager, and with it the laptop cleanup timer.
   - logind has `KillUserProcesses=no`, so tmux may survive a logout, but this was not tested.
   - The launcher holds a sleep/idle/lid-switch inhibit lock, but a logout or shutdown overrides it.
   - If the laptop is lost anyway, the in-cluster watchdog still removes the workloads (§11). You lose the session, not the cluster's safety.
3. **Do not close the terminal by killing tmux** (`tmux kill-server`, or `exit` in window 0 while the runner runs). Closing the terminal window or VS Code is fine: tmux keeps running, and you reattach (§3).
4. **Keep the watchdog.** Never delete the `bench-watchdog` CronJob or the laptop `ewallet-bench-down-*` timer while a session runs.

## 2. Environment and preflight

**Check that tmux is installed**, once:

```bash
command -v tmux && tmux -V || echo "install tmux first: sudo apt install tmux"
```

**Write the environment file once.** `CAMPAIGN` gets today's date now and stays fixed, even if the session crosses midnight:

```bash
cat > ~/.ewallet-bench-env <<EOF
cd ~/Develop/ewallet-data-ai-platform/.worktrees/claude-benchmark-evidence-replay
export KUBECONFIG=~/.kube/ewallet-n15-dns-config
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export VLLM_IMAGE=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/vllm-cpu-model@sha256:f695b23efa8145ae5cb57a1873863879927ab4805e70e5eee1a71a2063edd07c
export CAMPAIGN=docs/evidence/edai2/llm/owner_single_run_$(date -u +%Y%m%d)
source scripts/experiments/owner_evidence.sh
EOF
```

**Open the tmux session and its windows:**

```bash
tmux new -s bench -n run                 # window 0; you are now inside tmux
source ~/.ewallet-bench-env
tmux new-window -t bench -n show         # window 1
tmux new-window -t bench -n live         # window 2
tmux select-window -t bench:show
```

In windows 1 and 2, run `source ~/.ewallet-bench-env` first. Each window has its own shell.

**Preflight** (window 1 `show`):

```bash
git status --short                                                   # nothing
git log --oneline -1                                                 # the commit you will cite
echo $CAMPAIGN; ls $CAMPAIGN 2>/dev/null                            # must not exist yet
kubectl --context $KUBE_CONTEXT get nodes -l ewallet.io/bench=true   # "No resources found"
kubectl --context $KUBE_CONTEXT get ns ewallet-bench                 # NotFound
systemctl --user list-timers --all | grep ewallet-bench              # nothing
for p in $(pgrep -x kubectl); do tr '\0' ' ' </proc/$p/cmdline; echo; done | grep port-forward   # nothing
EXP_PROFILE=single EXP_RUNS=1 EXP_MAX_MINUTES=215 scripts/experiments/vllm_bench.sh exp-v1 plan /tmp/plan-v1; cat /tmp/plan-v1/plan/arm-difference.diff
EXP_PROFILE=single EXP_RUNS=1 EXP_MAX_MINUTES=215 scripts/experiments/vllm_bench.sh exp-v2 plan /tmp/plan-v2; cat /tmp/plan-v2/plan/arm-difference.diff
mkdir -p $CAMPAIGN/screenshots
```

| ID | Kind | Capture | Must show |
|---|---|---|---|
| `PRE-1` → `$CAMPAIGN/screenshots/PRE-1_plan.png` | SUPPORTING | both `plan` outputs | `profile=single methodology=single-run`, `runs_per_arm=1`, `levels=1,2,4` / `levels=1,2,4,8`, `fits: true`, `worst_case_usd` 0.93 / 1.85, `problems: []` |
| `PRE-2` → `$CAMPAIGN/screenshots/PRE-2_arm_difference.png` | SUPPORTING | both diffs | EXP-V1: only `VLLM_CPU_OMP_THREADS_BIND` `auto`→`0-1`. EXP-V2: only the gateway `baseUrl` and `x-ewallet-backend` |

## 3. tmux in five commands

| To | Do |
|---|---|
| switch window | `Ctrl-b 0`, `Ctrl-b 1`, `Ctrl-b 2` |
| leave tmux, keep everything running | `Ctrl-b d` (detach). The runner keeps running, and closing the terminal or VS Code is now safe |
| come back | open any terminal: `tmux attach -t bench` |
| see whether the session exists | `tmux ls` |
| scroll back in a window | `Ctrl-b [`, then arrows or PgUp; `q` to leave scroll mode |

Type `CONTINUE` or `ABORT` in window 0 (`run`), and press Enter. A checkpoint waits at most 20 minutes. If you are detached, attach first.

## 4. EXP-V1 (thread binding): one run per arm

**Window 0 `run`** starts the session:

```bash
EXP_APPROVED=yes scripts/experiments/owner_single_run.sh exp-v1 $CAMPAIGN
```

**Check the first lines right away:** `owner_single_run: exp-v1 single-run …` and `start exp-v1 session profile=single methodology=single-run … runs=1`. If you see anything else, press `Ctrl-C` (§10).

**Window 2 `live`** shows progress while requests are processed:

```bash
until [ -d $CAMPAIGN/exp-v1 ]; do sleep 5; done; python3 -m ops.experiments.progress $CAMPAIGN/exp-v1
```

**Flow.** Baseline `auto` (c=1, 2, 4) → **checkpoint 1** → optimized `0-1` (c=1, 2, 4) → **checkpoint 2** → comparison → **checkpoint 3** → cleanup.
- Each arm starts with fresh pods: about 14 minutes for the first vLLM start, then the measured window.
- The automatic gateway policy probe runs after each window, outside it.
- At each checkpoint, window 0 prints a banner titled `EXP-V1 SINGLE-RUN / BASELINE run 01` (or `OPTIMIZED`, or `COMPARISON`). The banner gives the Grafana link for exactly that run's window.
- Capture the screens below, then type `CONTINUE` in window 0.

**Failed requests do not stop the session.** The `auto` arm can lose `long-1` to the gateway's 160 s timeout. In that case window 0 logs `measured; N of M requests failed and stay in the comparison`, and the comparison counts the failure.

`R=$CAMPAIGN/exp-v1`. Save each PNG at the path given in its row.

| ID | Kind | Rubric | When | Where and command | Must show | Raw artifact |
|---|---|---|---|---|---|---|
| `S1-01` provenance → `$R/baseline/screenshots/S1-01_pods_nodes.png` | REQUIRED | 3, 4 | checkpoint 1 | window 1: `ev_provenance $R/baseline/raw/run-01` | one `bench-pool` node; `vllm-primary` Running; `imageID …@sha256:f695b23e…`; `model_revision c1899de2…`; `run=baseline-01` | `baseline/raw/run-01/controls.json` |
| `S1-02` baseline config → `$R/baseline/screenshots/S1-02_config.png` | REQUIRED | 3, 6 | checkpoint 1 | window 1: `ev_config; ev_route` | `VLLM_CPU_OMP_THREADS_BIND=auto`, `OMP_NUM_THREADS=2`, `--max-num-seqs 4`, `--max-num-queued-reqs 8`, 2 CPU / 6Gi requested; route `vllm-primary…:8000`, `backendRequestTimeout: 160s` | `baseline/config/run-01.yaml` |
| `S1-03` requests in flight → `$R/baseline/screenshots/S1-03_running.png` | REQUIRED | 6 | during the baseline window, after window 0 prints `run 01 baseline measuring` (≈15 min after start) | window 2 next to Grafana (last 15 min, `run` = `baseline-01`) | rows with `c=`, `http 200`, TTFT/E2E; Grafana series rising | `baseline/raw/run-01/raw_requests.jsonl` |
| `S1-04` baseline result → `$R/baseline/screenshots/S1-04_result.png` | REQUIRED | 6 | checkpoint 1 | window 0: the banner | `SINGLE-RUN`, `Thread binding: auto`, digest, `Window (UTC)`, lines `c=1, 2, 4` with tok/s, TTFT, E2E p95, errors | `baseline/raw/run-01/summary.json` |
| `S1-05` baseline Grafana → `$R/baseline/screenshots/S1-05_grafana.png` | REQUIRED | 6, 50 | checkpoint 1 | browser: the banner's link | row A (tokens/s, TTFT, E2E, running/waiting, admission rejections) and row B (CPU, memory) for `baseline-01`, over the printed window | `baseline/metrics/run-01/prometheus-readback.json` |
| `S1-06` scrape targets → `$R/baseline/screenshots/S1-06_targets.png` | SUPPORTING | 50 | checkpoint 1 | window 1: `ev_targets` (or `http://127.0.0.1:13090/targets`) | `vllm`, `agentgateway`, `pod-usage` all `up` | `prometheus-readback.json` → `targets` |
| `S1-07` optimized config → `$R/optimized/screenshots/S1-07_config.png` | REQUIRED | 6 | checkpoint 2 | window 1: `ev_config` | `VLLM_CPU_OMP_THREADS_BIND=0-1`; everything else as in S1-02 | `optimized/config/run-01.yaml` |
| `S1-08` optimized in flight → `$R/optimized/screenshots/S1-08_running.png` | SUPPORTING | 6 | during the optimized window | window 2 + Grafana `run` = `optimized-01` | as S1-03 | `optimized/raw/run-01/raw_requests.jsonl` |
| `S1-09` optimized result → `$R/optimized/screenshots/S1-09_result.png` | REQUIRED | 6 | checkpoint 2 | window 0: the banner | `Thread binding: 0-1`, same node as S1-01, lines `c=1, 2, 4` | `optimized/raw/run-01/summary.json` |
| `S1-10` optimized Grafana → `$R/optimized/screenshots/S1-10_grafana.png` | REQUIRED | 6, 50 | checkpoint 2 | browser: the banner's link | as S1-05 for `optimized-01` | `optimized/metrics/run-01/prometheus-readback.json` |
| `S1-11` comparison → `$R/comparison/screenshots/S1-11_comparison.png` | REQUIRED | 6 | checkpoint 3 | window 0: the `EXP-V1 (SINGLE-RUN) — COMPARISON CHECKPOINT` block; scroll with `Ctrl-b [`, or run `less $R/comparison/comparison.md` in window 1 | `Methodology: single-run`, `Execution validity: VALID`, the observed outcome and its reasons, the failure table, the metric rows (tok/s, TTFT, E2E, CPU, tokens per CPU) for c=1, 2, 4 | `comparison/metrics.json` |
| `S1-12` manifest → `$R/comparison/screenshots/S1-12_manifest.png` | REQUIRED | 3, 6 | checkpoint 3 | window 1: `python3 -m json.tool $R/manifest.json \| grep -E 'source_sha\|profile\|methodology\|runs_per_arm\|levels\|vllm_image\|deviations' ` | `profile: single`, `methodology: single-run`, `runs_per_arm: 1`, `levels: 1,2,4`, `source_sha` (= PRE `git log -1`), image digest, `deviations` | `manifest.json` |
| `S1-13` session in Grafana → `$R/comparison/screenshots/S1-13_grafana_session.png` | SUPPORTING | 50 | checkpoint 3 | browser: the link in the comparison banner (`run` = All) | the two runs side by side | `observability/metric-readback/*.json` |
| `S1-14` cleanup → `$CAMPAIGN/screenshots/S1-14_cleanup.png` | REQUIRED | 6 | after window 0 prints `session end status=0` and `runner exit 0` | window 1: `ev_cleanup $R` | `pool and namespace confirmed absent; no bench VMs or disks`; only `cpu-pool`; `bench VMs: 0`, `bench disks: 0`; `NotFound`; `production unchanged`; `armed bench timers: 0` | `safety/cleanup.txt`, `production-*.txt` |

The single-run comparison table also lists ITL, error and timeout rates, valid tool calls and tokens/s per CPU core. Report the numbers from `$R/comparison/metrics.json`, never from Grafana.

## 5. Between the sessions

Start EXP-V2 only when all of these hold:

- S1-14 shows `status=0`, cleanup confirmed and production unchanged.
- Window 2 has ended. The live view stops by itself when the manifest is finished.
- `kubectl --context $KUBE_CONTEXT get nodes -l ewallet.io/bench=true` prints nothing.

Do not commit or change anything in between. The EXP-V1 evidence inside `$CAMPAIGN` is expected, and the launcher allows it. Nothing stays running in the cloud between the two sessions.

## 6. EXP-V2 (Service vs llm-d EPP): one run per arm

**Window 0:**

```bash
EXP_APPROVED=yes scripts/experiments/owner_single_run.sh exp-v2 $CAMPAIGN
```

**Window 2:**

```bash
until [ -d $CAMPAIGN/exp-v2 ]; do sleep 5; done; python3 -m ops.experiments.progress $CAMPAIGN/exp-v2
```

**Flow.** B2 Kubernetes Service (c=1, 2, 4, 8) → **checkpoint 1** → O2 llm-d EPP (c=1, 2, 4, 8) → **checkpoint 2** → comparison → **checkpoint 3** → cleanup.
- Both arms run two `0-1` replicas, one per node.
- The router (Envoy + EPP) is installed only for O2.
- EPP scorers and vLLM admission limits are the G2 values and are not changed.

`R=$CAMPAIGN/exp-v2`. The gateway policy screens (§7) are captured at these same checkpoints.

| ID | Kind | Rubric | When | Where and command | Must show | Raw artifact |
|---|---|---|---|---|---|---|
| `S2-01` two replicas → `$R/baseline/screenshots/S2-01_pods_nodes.png` | REQUIRED | 3, 6 | checkpoint 1 | window 1: `ev_provenance $R/baseline/raw/run-01` | two `bench-pool` nodes; two `vllm-primary` pods, one per node; digest `f695b23e…`; revision `c1899de2…` | `baseline/raw/run-01/controls.json` |
| `S2-02` B2 route → `$R/baseline/screenshots/S2-02_route.png` | REQUIRED | 6 | checkpoint 1 | window 1: `ev_route; ev_config` | `baseUrl` → `vllm-primary.ewallet-bench.svc…:8000`, `x-ewallet-backend: primary`; `VLLM_CPU_OMP_THREADS_BIND=0-1` | `baseline/config/run-01.yaml` |
| `S2-03` B2 in flight → `$R/baseline/screenshots/S2-03_running.png` | SUPPORTING | 6 | during the B2 window | window 2 + Grafana `run` = `baseline-01` | rows with `backend primary`, levels up to `c=8` | `baseline/raw/run-01/raw_requests.jsonl` |
| `S2-04` B2 result → `$R/baseline/screenshots/S2-04_result.png` | REQUIRED | 6 | checkpoint 1 | window 0: the banner | `SINGLE-RUN`, `replicas: 2`, lines `c=1, 2, 4, 8` | `baseline/raw/run-01/summary.json` |
| `S2-05` B2 Grafana per replica → `$R/baseline/screenshots/S2-05_grafana.png` | REQUIRED | 6, 50 | checkpoint 1 | browser: the banner's link | "Completed requests per replica" and "Share of completed requests per replica" with two pods; latency and CPU rows | `baseline/metrics/run-01/prometheus-readback.json` |
| `S2-06` B2 replica counters → `$R/baseline/screenshots/S2-06_replicas.png` | SUPPORTING | 6 | checkpoint 1 | window 1: `ev_replicas` | `vllm:request_success_total` per pod | `summary.json` → `levels.*.distribution` |
| `S2-07` O2 route and router → `$R/optimized/screenshots/S2-07_route.png` | REQUIRED | 6 | checkpoint 2 | window 1: `ev_route; ev_k get pods -l app.kubernetes.io/name=llmd-router-epp -o wide` | `baseUrl` → `llmd-router-epp.ewallet-bench.svc…:8081`, `x-ewallet-backend: llm-d`; router pod 2/2 Running | `optimized/config/run-01.yaml`, `optimized/logs/run-01/router.log` |
| `S2-08` EPP endpoints and decisions → `$R/optimized/screenshots/S2-08_epp.png` | REQUIRED | 6 | checkpoint 2 | window 1: `ev_epp`; then `ev_k logs deploy/llmd-router-epp -c epp --tail=40 \| grep -i endpoint` | `llm_d_epp_ready_endpoints 2`; scheduler attempts for both endpoints; log lines naming both pod IPs | `optimized/raw/run-01/epp-metrics-before.txt` / `-after.txt`, `gateway-policy/optimized/epp.log` |
| `S2-09` O2 result, Grafana, distribution → `$R/optimized/screenshots/S2-09_result.png`, `S2-09_grafana.png` | REQUIRED | 6, 50 | checkpoint 2 | window 0 banner; browser link; window 1 `ev_replicas` | lines `c=1, 2, 4, 8`; both pods with completed requests; EPP panels with data | `optimized/raw/run-01/summary.json`, `prometheus-readback.json` |
| `S2-10` O2 in flight → `$R/optimized/screenshots/S2-10_running.png` | SUPPORTING | 6 | during the O2 window | window 2 + Grafana `run` = `optimized-01` | rows with `backend llm-d` | `optimized/raw/run-01/raw_requests.jsonl` |
| `S2-11` comparison → `$R/comparison/screenshots/S2-11_comparison.png` | REQUIRED | 6 | checkpoint 3 | window 0 block, or `less $R/comparison/comparison.md` | `single-run`, `VALID`, the observed outcome (decision metric `e2e_p95_ms`), the failure table for c=1, 2, 4, 8, the per-replica share table | `comparison/metrics.json` |
| `S2-12` manifest → `$R/comparison/screenshots/S2-12_manifest.png` | SUPPORTING | 3, 6 | checkpoint 3 | window 1: as S1-12 with `$R` | `profile: single`, `levels: 1,2,4,8`, `nodes: 2` | `manifest.json` |
| `S2-13` cleanup → `$CAMPAIGN/screenshots/S2-13_cleanup.png` | REQUIRED | 6 | after `runner exit 0` | window 1: `ev_cleanup $R` | as S1-14 | `safety/cleanup.txt` |

## 7. Gateway policy evidence (EXP-V2 checkpoints)

**What the runner already does.** After each arm's measured window, the runner runs an automatic probe and writes these files to `$R/gateway-policy/<arm>/`:
- `results.json`, `probe.log`, `attribution.json`;
- `gateway.log`;
- in O2, also `epp.log` and `envoy.log`.

**When to run the manual sequence.** At these checkpoints the measured window has ended, and the next arm restarts every pod, so these requests cannot enter any benchmark window.

| Checkpoint | Run |
|---|---|
| EXP-V2 **checkpoint 1** (B2) | POL-1 to POL-5, and POL-7 |
| EXP-V2 **checkpoint 2** (O2) | POL-5 again, and POL-6 if the burst returned 503 |

**How keys are handled.** The helpers read each key from its Secret into a private header file (mode 600) and delete it afterwards. No key appears on screen or in the process list. Do not run them with `set -x`.

| ID | Kind | Case | Window 1 command | Must show | Raw artifact |
|---|---|---|---|---|---|
| `POL-1` → `$R/baseline/screenshots/POL-1_valid.png` | REQUIRED | authenticated request | `ev_policy_valid` | `HTTP/1.1 200`, `x-ewallet-backend: primary`, `x-ratelimit-*` headers | `gateway-policy/baseline/results.json` `valid_client_key` |
| `POL-2` → `…/POL-2_missing.png` | REQUIRED | no API key | `ev_policy_missing` | `missing key -> HTTP 401` | `missing_key` |
| `POL-3` → `…/POL-3_invalid.png` | REQUIRED | invalid key; the vLLM backend key used as a client key | `ev_policy_wrong; ev_policy_backend_key` | `HTTP 401` twice | `wrong_key`, `backend_key_as_client_key` |
| `POL-4` → `…/POL-4_rate_limit.png` | REQUIRED | rate limit, 12 requests/s | `ev_policy_burst 16` | a count of `200` and a count of `429` | `rate_limit_burst`, `gateway.log` (`http.status=429`) |
| `POL-5` → `$R/baseline/screenshots/POL-5_route.png`, `$R/optimized/screenshots/POL-5_route.png` | REQUIRED | routing | `ev_policy_route` (in O2, `ev_epp` before and after) | three lines `HTTP/1.1 200 … x-ewallet-backend: primary` (B2) or `llm-d` (O2); in O2, `llm_d_epp_request_total` +3 | `routing_backend_header` |
| `POL-6` → `$R/optimized/screenshots/POL-6_admission.png` | SUPPORTING | admission rejection (only if the O2 burst returned 503) | `cat $R/gateway-policy/optimized/attribution.json; ev_replicas` | `"verdict": "ATTRIBUTED"`, `vLLM admission control (max_num_queued_reqs) … relayed by the llm-d EPP as ModelServerError` | `attribution.json`, `counters/*.prom` |
| `POL-7` → `$R/baseline/screenshots/POL-7_policy.png` | REQUIRED | the policy itself | `cat $R/gateway-policy/baseline/gateway-policy-config.yaml` | `apiKey` auth (keys removed), `localRateLimit` 12/s and the token budget, `backendRequestTimeout 160s` | the same file |

**Why POL-6 happens.**
- The burst repeats one prompt. With the EPP's prefix affinity it can reach a single pod. vLLM `--max-num-queued-reqs 8` then answers the requests beyond 8 in flight with 503, and the EPP counts the same 503s as `ModelServerError`.
- In the rehearsal, B2 spread the burst over both pods and returned no 503.
- The measured workload never has more than 8 requests in flight.

**Failover: NOT_EXECUTED.** The benchmark stack has no fallback backend, on purpose: a fallback would hide failed requests. Failover was shown in G1 ([BENCHMARK_REPLAY.md §6](../../edai2/llm/BENCHMARK_REPLAY.md#6-gateway-policy-evidence)).

## 8. Watching Grafana and Prometheus

- **Grafana:** `http://127.0.0.1:13030`, dashboard "EDAI-2 — LLM CPU Benchmark (EXP-V1 / EXP-V2)". It is available while a session runs; the runner opens the port-forward. Every checkpoint banner prints the link for that run's exact window (`from`, `to`, `var-run_id`).
- **Selectors:** keep the `experiment` and `run` selectors visible in screenshots.
- **Prometheus targets:** `http://127.0.0.1:13090/targets`, or `ev_targets`.
- **After cleanup** both stop, because they live in `ewallet-bench`. Per-run readbacks stay in `$R/<arm>/metrics/run-01/prometheus-readback.json` and `$R/observability/metric-readback/`.

## 9. Postflight (after both sessions)

```bash
tail -1 $CAMPAIGN/exp-v1.terminal.log $CAMPAIGN/exp-v2.terminal.log          # runner exit 0 (both)
python3 -c "import json,sys; [print(p, json.load(open(p))['methodology'], json.load(open(p))['valid'], json.load(open(p))['verdict']) for p in sys.argv[1:]]" $CAMPAIGN/exp-v*/comparison/metrics.json
ev_cleanup $CAMPAIGN/exp-v1; ev_cleanup $CAMPAIGN/exp-v2
(cd $CAMPAIGN && find . -type f ! -name sha256sums.txt -print0 | sort -z | xargs -0 sha256sum > sha256sums.txt)
```

1. Both comparisons must say `single-run True <verdict>`. Any `OBSERVED_*` or `NO_CLEAR_OBSERVED_CHANGE` is a valid result. `INVALID` means the pair is not comparable; read `validity_problems`.
2. Check every row of [BENCHMARK_EVIDENCE_MATRIX.md](../../edai2/llm/BENCHMARK_EVIDENCE_MATRIX.md): each REQUIRED screenshot exists and shows what its row requires.
3. Take numbers only from `comparison/metrics.json`. Describe them as observed differences of one run per arm.
4. Commit the campaign directory yourself. Do not run `tmux kill-server` before both `runner exit` lines are written.

## 10. Stopping safely

| Situation | Do |
|---|---|
| At a checkpoint and you want to stop | type `ABORT` in window 0. The runner records the deviation and cleans up |
| A run is measuring and you must stop | window 0: `Ctrl-C` once, then wait for `session end status=…` and `runner exit …`. Cleanup takes 2–5 min. Do not press it again, and do not close tmux meanwhile |
| You detached and the terminal or VS Code closed | `tmux attach -t bench`; the session is still there |
| tmux or the laptop died | in a new terminal: `source ~/.ewallet-bench-env; scripts/experiments/vllm_bench.sh exp-v1 down /tmp/down-$(date +%s)` (use `exp-v2` for that session). It removes only the objects that carry the ownership marks. Then run `ev_cleanup $CAMPAIGN/exp-v1` |

**A session cannot be resumed.** The comparison needs both arms of one session, on the same nodes, under one source commit. After an interrupted session:
1. Confirm cleanup.
2. Rename the directory to `exp-v1-attempt1`, and keep it.
3. Start again into a new `$CAMPAIGN/exp-v1`.

Never combine screenshots or runs from an interrupted attempt with a new session. Never repeat a session because the optimized arm lost.

## 11. What protects the cluster while you capture

- **Checkpoint timeout.** Each checkpoint waits at most `EXP_CHECKPOINT_MINUTES` (default 20; you may export up to 45 before starting), and never past the time limit. When the wait ends, the runner stops and cleans up.
- **Host loss.**
  - If the laptop dies, sleeps or logs out, an in-cluster watchdog (a Role limited to `ewallet-bench`) deletes the benchmark workloads at the time limit + 10 min. The autoscaled `bench-pool` (min 0) then drains to zero nodes.
  - A systemd user timer on the laptop also runs `down` at the limit + 20 min, when the laptop is up.
- **Cost ceiling.** The cost plan refuses a session whose worst case exceeds USD 5 or whose window exceeds 240 min.
- **Production.** Helm releases and the `cpu-pool` nodes are read before and after. Any difference fails the session (`PRODUCTION STATE CHANGED`).

## 12. Options and the other profiles

| Variable | Set by `owner_single_run.sh` | Meaning |
|---|---|---|
| `EXP_PROFILE` | `single` | `formal`: multi-run (3 per arm); `smoke`: reduced levels, never a result |
| `EXP_RUNS` | `1` | `single` and `smoke` accept only 1; `formal` accepts 2–5 |
| `EXP_CAPTURE_MODE` | `interactive` | `unattended` never stops |
| `EXP_CHECKPOINT_RUNS` | `first` | stop after run 01 of each arm and at the comparison (with one run per arm, the same as `all`) |
| `EXP_MAX_MINUTES` | `215` | hard time limit (max 215) |
| `EXP_CHECKPOINT_MINUTES` | not set (20) | longest wait per checkpoint, 1–45 |
| `EXP_MAX_USD`, `EXP_LAPTOP_TIMER`, `EXP_POLICY_PROBE` | not set | 5, `auto`, `on` |

**Historical multi-run rehearsal (not for the submission).** It used this command:

```bash
EXP_APPROVED=yes EXP_RUNS=3 EXP_CAPTURE_MODE=unattended EXP_MAX_MINUTES=215 \
  scripts/experiments/vllm_bench.sh exp-v1 session <dir>
```

The chain is described in [BENCHMARK_REPLAY.md](../../edai2/llm/BENCHMARK_REPLAY.md) and the [rehearsal README](../../evidence/edai2/llm/benchmark_rehearsal_20261109/README.md).

**Smoke test:** `EXP_PROFILE=smoke`. **Cleanup drill:** `scripts/experiments/vllm_bench.sh exp-v1 drill <dir>`.
