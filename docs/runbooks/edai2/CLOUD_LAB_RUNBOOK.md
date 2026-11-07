# On-demand GKE cloud lab

How to sleep and wake `ewallet-dev`. The cluster has been asleep since
2026-11-06T16:38:12Z with `cpu-pool` at zero nodes.

Tool: [`ops/gcp/cloud_lab.py`](../../../ops/gcp/cloud_lab.py). Profiles:
[`ops/gcp/cloud_lab_profiles.json`](../../../ops/gcp/cloud_lab_profiles.json).
Recovery manifest: [`docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z`](../../../docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z/MANIFEST.json).

Every wake needs explicit owner approval. Without `--execute` a wake command
only prints its plan. With it, the tool also needs
`--confirm-cluster ewallet-dev` and `CLOUD_LAB_APPROVED=yes`.

No wake path has been executed yet. The plans were dry-run against the sleeping
cluster; node counts below come from recorded requests, not from a real wake.

## Commands

```bash
M=docs/evidence/edai2/cloud_lab/sleep_20261106T163042Z
python3 ops/gcp/cloud_lab.py status
python3 ops/gcp/cloud_lab.py cost-status
python3 ops/gcp/cloud_lab.py plan-sleep
python3 ops/gcp/cloud_lab.py plan-wake --mode full --manifest $M
python3 ops/gcp/cloud_lab.py plan-wake --mode minimal --profile jenkins-smoke --manifest $M
python3 ops/gcp/cloud_lab.py verify --expect sleep --manifest $M
```

## Modes

| Mode | Profile | Nodes | Requests incl. system pods | Cost/h while awake |
| --- | --- | --- | --- | --- |
| 0 Sleep | none | 0 | none | $0.107 |
| 1 Minimal | `jenkins-smoke`, `sandbox`, `kserve-small` | 1 | about 2.2 CPU, 3.7 GiB | $0.285 |
| 1 Minimal | `mcp-fallback` | 1 | 1.6 CPU, 2.7 GiB | $0.285 |
| 1 Minimal | `one-agent` | 2 | 3.8 CPU, 8.9 GiB | $0.463 |
| 2 Experiment | `vllm-cpu`, `agent-sandbox` | 2 | up to 4.4 CPU, 10 GiB | $0.463 |
| 2 Experiment | `agent-keda` | 3 | 5.6 CPU, 13.8 GiB | $0.641 |
| 3 Full | none | 4 | 8.9 CPU, 22.7 GiB | $0.819 |

Each node gives 3.0 CPU and 11.4 GiB after DaemonSets. Counts keep 15% headroom.
Full needs 4 nodes: 3 would be 99% full on CPU requests. Costs exclude the
cluster fee, logging, metrics and egress.

GPU experiments are out of scope for this tool. They need a separate approved
node pool, quota and cost check.

## Wake full

```bash
python3 ops/gcp/cloud_lab.py wake-full --manifest $M        # read the plan
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-full --manifest $M \
  --execute --confirm-cluster ewallet-dev
python3 ops/gcp/cloud_lab.py verify --expect awake --manifest $M
```

The tool resizes to 4 nodes, waits for Ready, labels 3 nodes
`ewallet.io/g3-llm=true`, and restores recorded replicas. HPAs resume by
themselves once a target is above zero. No Helm command is needed: all releases
are unchanged.

## Wake one component

```bash
CLOUD_LAB_APPROVED=yes python3 ops/gcp/cloud_lab.py wake-minimal --profile jenkins-smoke \
  --manifest $M --execute --confirm-cluster ewallet-dev
```

Before adding nodes, the tool scales every application workload outside the
profile to zero. Otherwise the scheduler would fill the node from all pending
pods. System namespaces are left alone. `wake-full` later restores everything.

## Validation after wake

1. `verify --expect awake` reports no failures.
2. All pods reach Ready; `kubectl get pods -A | grep -v Running` shows only Completed.
3. `ewallet.io/g3-llm` is on 3 nodes and no `g3` pod is Pending.
4. Feast Redis reports `DBSIZE` 173382 and `aof_last_write_status:ok`.
5. Valkey: `kubectl -n ate-system exec valkey-cluster-0 -- valkey-cli cluster info` shows `cluster_state:ok`.
6. HPAs show `ScalingActive=True`; PDBs show `expectedPods` above zero.
7. `release_state.py drift` reports zero drift on `llm-agent` and `llm-agent-mcp`.
8. KFP, MLflow, Jenkins and the Prompt Registry open and list their old records.

## Known limitations

- **Valkey does not re-form (observed 2026-11-07).** `nodes.conf` stores pod
  IPs, there is no announce-hostname, and the init Job does not rerun. After the
  wake all six nodes reported `cluster_state:fail`. That made `ate-api-server`
  crash-loop, and `kagent-controller` crash-loops while it cannot dial `ate-api`.

  **Owner-approved recovery** (evidence in `docs/evidence/edai2/cloud_lab/wake_20261107T150640Z/valkey`):
  1. Check that the six node IDs are unique and unchanged.
  2. Check that every node holds the same slot map covering 16,384 slots.
  3. From `valkey-cluster-0`, run `valkey-cli CLUSTER MEET <new pod IP> 6379` for each of the other five pods.

  All six nodes reached `cluster_state:ok` within about 20 seconds, with all slots OK and the node IDs unchanged. One shard failed over to its replica automatically, with the same slot range. Synthetic read and write worked in every slot range. `ate-api-server` was Ready about 6 minutes later, and `kagent-controller` about 10 minutes later, through their own backoff; nothing was restarted by hand.

  **Residual of that first recovery:** `valkey-cluster-0` sent the MEETs but never received one, so it kept its old self-address.

  **Every pod move repeats the failure (G4.5, 2026-11-07).** The GKE node upgrade rescheduled all six pods. Afterwards every node advertised a stale address for *itself* and for its peers. `ate-api` (go-redis cluster client) dialled a stale address, `10.216.1.17`. The root cause combines three things:
  - stale peer addresses (A);
  - stale self-addresses on all six nodes (B);
  - `nodes.conf` on the PVC, with no `cluster-announce-ip` in `valkey-config` (C).

  Valkey sets its own address from the socket of an incoming `CLUSTER MEET` when `cluster-announce-ip` is empty. So the bounded sequence must deliver a MEET **to every node, including the sender**:
  1. Run the same prechecks: six known IDs, one slot map, 16,384 slots.
  2. From `valkey-cluster-1`, `CLUSTER MEET <current IP> 6379` to pods 0, 2, 3, 4 and 5.
  3. From `valkey-cluster-0`, `CLUSTER MEET <current IP of pod 1> 6379`.
  4. Verify:
     - every `myself` address equals its pod IP;
     - no `fail`, `handshake` or `noaddr` flags;
     - `valkey-cli --cluster check` reports OK;
     - a `-c` read and write in all three shards, then delete the keys.

  This took 6 MEETs, with no `CONFIG SET`, and reached `cluster_state:ok` within 20 seconds. Again one shard failed over inside its own pair. Evidence: `docs/evidence/edai2/cloud_lab/stabilize_20261107T173813Z/valkey`.

  **Durable fix (needs its own approval, not applied):** set `cluster-announce-ip` from the pod IP at start-up. Use a `POD_IP` downward-API env and `--cluster-announce-ip $(POD_IP)` in the `valkey-cluster` StatefulSet, or announce hostnames through a headless Service. Changing the StatefulSet restarts the pods one by one. Until then, every wake, node upgrade or repair needs the sequence above.

  **Placement.** There is no pod anti-affinity. After G4.5 each shard happens to span two nodes, but the scheduler gives no guarantee: before the upgrade all six pods sat on one node.

  Never use `CLUSTER RESET`, `FLUSHALL`, a reshard, or edit `nodes.conf`.
- **Observability history is gone.** Prometheus, Loki and Tempo used `emptyDir`.
  Grafana dashboards come from ConfigMaps and survive. Repository evidence is unaffected.
- **Third-party images.** 19 images use tags, and many come from Docker Hub,
  GHCR and Quay. A pull limit can delay a wake, and a moved tag can change
  content. Resolved digests are in `pods-image-digests.json`.
- **Owned workloads in partial wakes.** `fraud-detector-predictor` belongs to
  KServe and `kagent-pilot-deployment` to a WorkerPool. Their controllers may
  override a hold-down. kagent was observed not to for 3 minutes on 2026-11-06.
- **No disk backups.** There are zero snapshots and every PV has reclaim policy
  `Delete`. Never delete a PVC or namespace while relying on these disks.
- **Do not run `terraform apply` while asleep.** The module declares autoscaling
  and the live pool has none, so an apply could add nodes and cost.
- **The control plane keeps upgrading.** It is on the REGULAR channel, so the
  version may have moved at wake.
