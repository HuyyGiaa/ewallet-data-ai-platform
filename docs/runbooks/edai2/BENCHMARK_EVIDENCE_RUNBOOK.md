# EXP-V1 / EXP-V2 owner evidence session

This runbook is for the owner's own screenshot session. It reruns the CPU benchmarks on GKE in the isolated `ewallet-bench` namespace and stops after every run so that you can capture the screens yourself. Nothing here generates screenshots, and nothing touches production.

Design, validation results and limits: [BENCHMARK_REPLAY.md](../../edai2/llm/BENCHMARK_REPLAY.md). Rules of the comparison: [BENCHMARK_METHODOLOGY.md](../../edai2/llm/BENCHMARK_METHODOLOGY.md).

## 0. What you need

| Item | Value |
|---|---|
| Worktree | `.worktrees/claude-benchmark-evidence-replay` |
| Branch | `feature/llm-benchmark-evidence-replay` |
| Source | the commit you check out; `git status` must be clean (the manifest records `source_sha` and `source_dirty`) |
| Cluster access | `~/.kube/ewallet-n15-dns-config` (DNS endpoint; the public IP endpoint times out from the laptop) |
| Time | EXP-V1 about 3 h 10 min, EXP-V2 about 2 h 50 min, run one after the other. Each run starts vLLM fresh, and that alone takes about 16 min (measured on 2026-11-09) |
| Cost | worst case USD 0.91 (EXP-V1) and USD 1.82 (EXP-V2), printed by `plan` |

Open three terminals in the worktree, plus a browser:

| Terminal | Use |
|---|---|
| T1 | the runner; it prints the checkpoints and waits for `CONTINUE` |
| T2 | the `kubectl` commands each checkpoint prints (configuration, pods, logs) |
| T3 | optional: `watch kubectl get pods,nodes -o wide` |
| Browser | Grafana at `http://127.0.0.1:13030` (the runner opens the port-forward; anonymous read-only) |

Set the same environment in T1 and T2:

```bash
cd ~/Develop/ewallet-data-ai-platform/.worktrees/claude-benchmark-evidence-replay
export KUBECONFIG=~/.kube/ewallet-n15-dns-config
export KUBE_CONTEXT=gke_ewallet-data-ai-platform-hg26_asia-southeast1-b_ewallet-dev
export VLLM_IMAGE=asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/ewallet-platform/vllm-cpu-model@sha256:f695b23efa8145ae5cb57a1873863879927ab4805e70e5eee1a71a2063edd07c
CAMPAIGN=docs/evidence/edai2/llm/benchmark_reproduction_$(date -u +%Y%m%dT%H%MZ)
alias k="kubectl --context $KUBE_CONTEXT -n ewallet-bench"
```

Before you start:

```bash
git status --short                      # must print nothing
kubectl --context $KUBE_CONTEXT get nodes -l ewallet.io/bench=true   # must print "No resources found"
for p in $(pgrep -x kubectl); do tr '\0' ' ' </proc/$p/cmdline; echo; done   # no leftover port-forward on 130xx/133xx
```

## 1. Look at the plan (local, touches no cluster)

```bash
scripts/experiments/vllm_bench.sh exp-v1 plan /tmp/plan-v1
scripts/experiments/vllm_bench.sh exp-v2 plan /tmp/plan-v2
cat /tmp/plan-v1/plan/arm-difference.diff        # EXP-V1: only VLLM_CPU_OMP_THREADS_BIND differs
cat /tmp/plan-v2/plan/arm-difference.diff        # EXP-V2: only baseUrl and x-ewallet-backend differ
```

Screenshot `0-1_plan_and_cost.png`: both `plan` outputs with the cost plan, and `0-2_arm_difference.png`: both diffs. Save them to `$CAMPAIGN/screenshots/`.

## 2. What protects the cluster while you capture

- Each checkpoint waits at most `EXP_CHECKPOINT_MINUTES` (default 20). Then the runner stops and cleans up.
- `ABORT` at a checkpoint stops the session and cleans up. Ctrl-C does the same.
- If the laptop sleeps or dies, an in-cluster watchdog deletes the benchmark workloads at the time limit + 10 min. The autoscaled `bench-pool` then drops to zero nodes. A systemd user timer also runs `down` at the limit + 20 min.
- The evidence directory survives in `$CAMPAIGN/exp-v1` (or `exp-v2`). After a lost session, run `scripts/experiments/vllm_bench.sh exp-v1 down /tmp/down` and then check §6.

## 3. EXP-V1: thread binding (one node)

T1:

```bash
EXP_APPROVED=yes EXP_CAPTURE_MODE=interactive EXP_CHECKPOINT_RUNS=first EXP_MAX_MINUTES=215 \
  scripts/experiments/vllm_bench.sh exp-v1 session $CAMPAIGN/exp-v1 2>&1 | tee $CAMPAIGN-exp-v1.terminal.log
```

Run order is baseline (`auto`), optimized (`0-1`), optimized, baseline, baseline, optimized. Each run restarts vLLM and the gateway.

With `EXP_CHECKPOINT_RUNS=first`, the runner stops after `baseline-01` and `optimized-01` and at the comparison. Runs 02 and 03 continue on their own, because each extra stop costs time against the 215-minute limit. Grafana keeps every run, so you can still capture runs 02 and 03 at the comparison checkpoint by choosing them in the `run` selector. To stop after every run, use `EXP_CHECKPOINT_RUNS=all` with `EXP_RUNS=3`, and keep each stop short.

At each checkpoint the runner prints a block like this:

```text
================================================================
EXP-V1 / BASELINE run 01 — OWNER EVIDENCE CHECKPOINT
================================================================
Experiment / Model / Arm / Thread binding / Route / Image digest / Nodes / Run ID / Window
Benchmark completed (client summary, authoritative): one line per concurrency
Raw output / Prometheus / Grafana: <link to exactly this run's window>
Capture now ... screenshots -> <evidence>/<arm>/screenshots/<run-id>-<name>.png
```

At every checkpoint, capture the following. Use the exact path the block prints. A validated example of the block is in `docs/evidence/edai2/llm/benchmark_reproduction_20261109/exp-v2-smoke-attempt2/baseline/logs/run-01/checkpoint.txt`.

| Name | What | How |
|---|---|---|
| `<run-id>-1_config.png` | Deployment env and gateway config | T2: the two commands under "1 deployment configuration" |
| `<run-id>-2_terminal.png` | Benchmark result | T1: the checkpoint block |
| `<run-id>-3_grafana.png` | Grafana panels A, B and C for this window | Browser: the printed link (it has `from`, `to` and `var-run_id`) |
| `<run-id>-4_pods_nodes.png` | Pods and the bench node | T2: the command under "4 pods and nodes" |

Two of these are required at least once per arm:

- `baseline-01-5_policy.png` and `optimized-01-5_policy.png`: run 01 of each arm also ran the gateway policy probe. Run `cat <evidence>/gateway-policy/<arm>/probe.log` (see §5).
- `baseline-01-6_prometheus_targets.png`: `http://127.0.0.1:13090/targets` shows one target per pod.

Then type `CONTINUE` in T1.

After the last run comes a COMPARISON CHECKPOINT. Capture `comparison.md` (`comparison-1_verdict.png`), `manifest.json` (`comparison-2_manifest.png`: source SHA, image digest, deviations) and the whole-session Grafana link (`comparison-3_grafana_session.png`). Save them under `$CAMPAIGN/exp-v1/comparison/screenshots/`. Then type `CONTINUE` and the runner cleans up.

## 4. EXP-V2: Service versus llm-d (two nodes)

Start only after EXP-V1 has printed `session end status=0` (§6 shows how to check).

```bash
EXP_APPROVED=yes EXP_CAPTURE_MODE=interactive EXP_CHECKPOINT_RUNS=first EXP_MAX_MINUTES=215 \
  scripts/experiments/vllm_bench.sh exp-v2 session $CAMPAIGN/exp-v2 2>&1 | tee $CAMPAIGN-exp-v2.terminal.log
```

Capture the same four screenshots per run. EXP-V2 needs a few more:

| Name | When | How |
|---|---|---|
| `<run-id>-5_replicas.png` | every run | Grafana panels "Completed requests per replica" and "Share of completed requests per replica" (two pods, two series) |
| `optimized-<nn>-6_epp.png` | optimized runs | Grafana row C: EPP ready endpoints = 2, scheduling decisions per endpoint |
| `optimized-<nn>-7_epp_log.png` | optimized runs | T2: `k logs deploy/llmd-router-epp -c epp --tail=40 \| grep -i endpoint` |
| `baseline-01-8_route.png` | baseline | T2: `k get configmap agentgateway-llm-config -o jsonpath='{.data.config\.yaml}' \| grep -E 'baseUrl\|x-ewallet-backend'` shows `vllm-primary...:8000` |
| `optimized-01-8_route.png` | optimized | the same command shows `llmd-router-epp...:8081` |

## 5. Gateway policy evidence

The runner runs the probe after the measured window of run 01 in each arm and writes `<evidence>/gateway-policy/<arm>/results.json`, `probe.log` and `gateway.log`. The probe sends at most 23 one-token requests.

| Case | Expected |
|---|---|
| `valid_client_key` | 200 |
| `missing_key`, `wrong_key` | 401 or 403 |
| `backend_key_as_client_key` | 401 or 403 (the vLLM key is a separate Secret) |
| `routing_backend_header` | `x-ewallet-backend: primary` (Service) or `llm-d`; in the llm-d arm the EPP request counter must grow |
| `rate_limit_burst` | 16 concurrent requests against the 12 requests/s limit: some 200 and some 429. In the llm-d arm, a few requests that pass the limit may get 503 from the router (EPP saturation); that is recorded, not hidden |

To show it live while a checkpoint is waiting (the gateway port-forward is still open), run this in T2. The keys go straight from the Secret into the probe's environment and are never printed:

```bash
EWALLET_GATEWAY_KEY="$(k get secret agentgateway-client-key -o jsonpath='{.data.api-key}' | base64 -d)" \
EWALLET_BACKEND_KEY="$(k get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d)" \
  python3 -m ops.experiments.gateway_policy --gateway http://127.0.0.1:13300 --expect-backend primary \
  --output /tmp/policy-live.json
k logs deploy/agentgateway --since=2m | grep -viE 'authorization|bearer|api[-_]key' | tail -20
```

In the EXP-V2 optimized arm, use `--expect-backend llm-d --epp-metrics http://127.0.0.1:13302/metrics`. Screenshots go to `$CAMPAIGN/gateway-policy/screenshots/`: `1_policy_config.png` (the `gateway-policy-config.yaml` file, with keys removed), `2_probe_results.png` and `3_gateway_log.png`.

Failover is not part of this stack, on purpose: a fallback backend would let a failed request pass as success and spoil the comparison. Failover evidence stays with G1; see [BENCHMARK_REPLAY.md §6](../../edai2/llm/BENCHMARK_REPLAY.md#6-gateway-policy-evidence).

## 6. Cleanup check

After each session:

```bash
tail -3 $CAMPAIGN/exp-v1/session.log                     # "session end status=0"
cat $CAMPAIGN/exp-v1/safety/cleanup.txt                   # pool and namespace confirmed absent; no bench VMs or disks
gcloud container node-pools list --cluster ewallet-dev --zone asia-southeast1-b --project ewallet-data-ai-platform-hg26
kubectl --context $KUBE_CONTEXT get ns ewallet-bench      # NotFound
gcloud compute instances list --project ewallet-data-ai-platform-hg26 --filter='name~bench-pool'   # empty
diff $CAMPAIGN/exp-v1/production-before.txt $CAMPAIGN/exp-v1/production-after.txt && echo production unchanged
systemctl --user list-timers --all | grep ewallet-bench   # nothing left armed
```

Screenshot: `$CAMPAIGN/screenshots/6-1_cleanup.png`.

If `CLEANUP FAILED` appears, run `scripts/experiments/vllm_bench.sh exp-v1 down /tmp/down` and repeat the checks. Removal requires the ownership marks (`ewallet.io/experiment=true` on the namespace, `ewallet.io/bench=true` on the pool). It never touches anything else.

## 7. After the session

1. `sha256sum` the evidence: `(cd $CAMPAIGN && find . -type f ! -name sha256sums.txt -print0 | sort -z | xargs -0 sha256sum > sha256sums.txt)`.
2. Fill in the "New run" rows of [BENCHMARK_REPLAY.md](../../edai2/llm/BENCHMARK_REPLAY.md) from `comparison/metrics.json`. Do not take numbers from Grafana.
3. Link the screenshots from the reports only once the files exist.

## 8. Options

| Variable | Default | Meaning |
|---|---|---|
| `EXP_CAPTURE_MODE` | `unattended` | `interactive` stops at each checkpoint |
| `EXP_CHECKPOINT_MINUTES` | 20 | longest wait per checkpoint (1-45) |
| `EXP_CHECKPOINT_RUNS` | `all` | `first`: stop only after run 01 of each arm and at the comparison (recommended for formal runs) |
| `EXP_RUNS` | 3 | runs per arm (2-5); below 3 the verdict is `INCONCLUSIVE` |
| `EXP_PROFILE` | `formal` | `smoke`: one run per arm, fewer levels, always reported as a smoke test |
| `EXP_MAX_MINUTES` | 210 | time limit (at most 215, so setup + limit + teardown fits 4 h) |
| `EXP_MAX_USD` | 5 | worst-case ceiling; the session refuses to start above it |
| `EXP_LAPTOP_TIMER` | `auto` | `off` disables the second cleanup path |
| `EXP_POLICY_PROBE` | `on` | `off` skips the gateway probe |

`scripts/experiments/vllm_bench.sh exp-v1 drill <dir>` repeats the cleanup drill: one node, one idle pod, a watchdog with a 4-minute deadline. It passes when the cluster has removed the node by itself.
