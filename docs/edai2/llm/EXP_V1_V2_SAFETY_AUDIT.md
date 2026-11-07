# EXP-V1 and EXP-V2 safety audit

This audit reviews `scripts/experiments/vllm_bench.sh` before the owner runs it. **Neither experiment has run.** Findings were checked against the live cluster's read-only state, and every fix has a test in `tests/experiments/test_vllm_bench_session.py`.

The review found defects that would have failed or misled a real session. All are fixed:

| Defect | Effect if left | Fix |
|---|---|---|
| `--no-enable-autoupgrade` | Pool creation fails: the cluster is on the REGULAR channel | Removed; a replaced node is caught instead |
| Node service account not set | Nodes use the default account and may not pull the image | Same account, scopes and hardening as `cpu-pool` |
| A gcloud failure read as "pool absent" | Cleanup reports success while a node keeps billing | Failed reads are errors, and absence must be confirmed |
| A kubectl failure read as "namespace absent" | Same | Same |
| `down` deleted any pool named `bench-pool` | Could delete a pool it didn't create | Deletes only with the ownership mark |
| Rollout wait could pass the time limit | Up to 30 extra minutes | Waits use the remaining time |
| Leftover port-forward on a benchmark port | Benchmark measures the wrong target | Session stops if a port is in use |
| Provenance recorded but not enforced | A wrong image would be found only at comparison | Checked before each run's first request |
| A summary could drop raw rows unseen | A failure could vanish silently | Raw rows are counted against the summary |

## The ten review points

| # | Question | Verdict | Evidence |
|---|---|---|---|
| 1 | Resource creation reviewed | PASS | Creates only `bench-pool`, `ewallet-bench` and, for EXP-V2, `llmd-router` inside it |
| 2 | Cleanup paths reviewed | PASS | Runs on success, failure and signals; reads back that both are gone |
| 3 | Production Helm releases safe | PASS | The only `helm uninstall` names `llmd-router` in `ewallet-bench`; a static test covers every driver |
| 4 | CPU pools can't be resized | PASS | No resize in any driver; a static test fails if one appears |
| 5 | Isolated names | PASS | Fixed names; the session refuses to start if either exists |
| 6 | Ownership verified | PASS | Namespace label and pool node label, both checked before deletion |
| 7 | Image and model provenance | PASS | Image must be in the project registry and exist; the running digest and model revision must match |
| 8 | Bounded execution and cleanup | PASS, one gap | Time limit bounds every wait. **If the host dies, nothing cleans up**: run `down` |
| 9 | Comparable conditions | PASS | One template, alternating arms, fresh restarts, controls read from pods, same nodes required |
| 10 | Failed requests kept | PASS | A test runs the real client with failures and follows them to the comparison |

Point 3 and 4 are also protected at run time: all Helm releases plus the `cpu-pool` nodes and size are compared before and after, and a difference fails the session.

## Capacity

Each e2-standard-4 node has 3.92 CPU allocatable, and DaemonSets request 0.918 CPU and about 1.5 GiB, measured on the live nodes. That leaves about 3.0 CPU.

| Experiment | Busiest node requests | Left |
|---|---|---|
| EXP-V1 | 2.1 CPU (vLLM and gateway) | 0.9 CPU |
| EXP-V2 | 2.2 CPU (vLLM and router) | 0.8 CPU |

The quota has room: `CPUS_ALL_REGIONS` 16 of 32, in-use IPs 4 of 8, SSD 273 of 500 GB.

## Remaining risks

- **Auto-upgrade can't be disabled.** A node replaced mid-session changes `node_names`, and the comparison becomes `INVALID_COMPARISON`. That wastes the session but can't mislead.
- **e2 CPU families vary.** Both arms share the same nodes within a session, so the comparison holds, but numbers may not match another session.
- **Host loss.** If the laptop sleeps or dies, the pool keeps billing about $0.18 per node-hour until `down` runs. Keep the machine awake.
- **Resource panels.** Prometheus doesn't scrape `ewallet-bench`, so use Cloud Monitoring.

## Using historical results

O1 and G2 are context only. Their stack differs (two gateways, fallback, ambient mesh), and the driver can't read them into a comparison. Do not mix them with new runs.
