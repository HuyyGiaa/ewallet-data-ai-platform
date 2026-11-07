# G5-R: Valkey survives pod moves

**Status: deployed 2026-11-07.** Helm release `substrate` revision 2, source commit `3952288`. Evidence: `docs/evidence/edai2/cloud_lab/g5r_20261107T194523Z`.

## Root cause

| Fact | Evidence |
|---|---|
| Valkey 8.0.12 in cluster mode keeps `nodes.conf` on its PVC | G4.5 readback |
| `valkey.conf` had no `cluster-announce-ip`; the container ran `valkey-server /etc/valkey/valkey.conf` | `valkey-config`, StatefulSet |
| After pods move, every node announced its old IP for itself and held old IPs for its peers | G4.5 `summary.txt` |
| `ate-api-server` (go-redis cluster client) dialled stale addresses and crash-looped; `kagent-controller` followed | G4.5 logs |

The two gaps:
1. **Own address.** A node learns its own IP only from an incoming `CLUSTER MEET`.
2. **Peer addresses.** After pods move, peers are known only by their old IPs.

## Change (source of truth)

`valkey-cluster` belongs to the Helm release `substrate` (chart 0.0.9, digest-pinned). The chart has no knob for this, so the repository's post-renderer makes the change. A server-side dry run first confirmed that the live release equals the repository render.

[substrate_resource_post_renderer.py](../../../infra/kubernetes/kagent-pilot/scripts/substrate_resource_post_renderer.py):

| Part | Change |
|---|---|
| `valkey` container | `POD_IP` from the Downward API; `--cluster-announce-ip $(POD_IP)` |
| `valkey` container | Readiness `valkey-cli -h 127.0.0.1 ping`. This is process liveness for Service endpoints and DNS. **It is not cluster health**, and a `cluster_state` check here would hide every pod from DNS after a full restart. |
| Sidecar `cluster-address-reconciler` | Every 15 s, for each peer: resolve its current IP through the headless Service and ask for its node ID. Skip any ID not already in the table. Do nothing if the table already holds exactly `<id> <ip>:6379@`. Otherwise `CLUSTER MEET`, at most 5 times per IP, then log `give up` |
| Sidecar security and resources | uid 999, non-root, read-only root filesystem, no privilege escalation, all capabilities dropped; 5m/16Mi requested, 50m/32Mi limit |
| Fail-closed guards | Rendering fails if upstream changes the Valkey command, args or env, or if a reconciler is already present |
| `ateapi-ca` | Undoes a chart bug that added one leading newline to `ca.crt` on every upgrade, so the upgrade changes only the StatefulSet |

The script contains no reset, flush, slot, forget or `nodes.conf` command; a unit test enforces this. Another test simulates the loop with fake `valkey-cli` and `getent`. It confirms:
- only a moved, known peer gets MEET;
- a current peer and an unknown ID are left alone;
- retries stop at the limit.

Tests: [test_kagent_bounded_pilot.py](../../../tests/unit/test_kagent_bounded_pilot.py).

## Rollout performed

1. **Preflight.**
   - GKE: no operation running, 5 nodes Ready.
   - Cluster: `ok`, the six known IDs, 16,384 slots, three pairs, six PVCs Bound.
   - Helm: one revision, 1.
2. **Server-side dry run** (`helm upgrade --dry-run=server`, with `lookup` active): 45 objects, 0 added or removed. **Only `StatefulSet/valkey-cluster` changes.** The first dry run also changed `ConfigMap/ateapi-ca`; the post-renderer fix above removed that before deploying.
3. **Hold:** `partition=6`, then `helm upgrade` to revision 2. No pod restarted.
4. **One pod at a time:** partition 5 → 0. After each pod the gate required:
   - Ready 2/2 on the new revision;
   - its own address equals its pod IP;
   - the same six IDs;
   - all nodes `ok` with 16,384 slots;
   - no fail flags;
   - each pair keeps its slot range;
   - replication links up;
   - a `-c` write, read and delete in all three shards.

   Every step passed within 50–52 s.

| Pod | Old IP → new IP | Result |
|---|---|---|
| 5 | 10.216.0.20 → 10.216.4.18 (moved jt0h → cbl0) | PASS |
| 4 | 10.216.2.11 → 10.216.2.15 | PASS |
| 3 | 10.216.0.19 → 10.216.0.26 | PASS |
| 2 (primary) | 10.216.1.24 → 10.216.1.25 | PASS; failed over to its replica, epoch 9 |
| 1 (primary) | 10.216.2.9 → 10.216.2.16 | PASS; failover, epoch 10 |
| 0 (primary) | 10.216.5.31 → 10.216.5.32 | PASS; failover, epoch 11 |

## Recovery tests

| Test | Result |
|---|---|
| Recreate `valkey-cluster-2` (replica) | New IP 10.216.1.26, announced correctly, rejoined on its own; healthy in 50 s |
| Recreate `valkey-cluster-3` (then primary) | New IP 10.216.0.27; failed over and rejoined on its own; healthy in 52 s |
| Final state | Six IDs unchanged; 16,384 slots; `--cluster check` OK; each pair keeps its range; key counts and existing keys (`atespace:*`, `actor:*`, `worker:*`) as before |
| Clients | `ate-api-server` and `kagent-controller`: no restart and no Valkey error during the rollout |
| A2A | HTTP 200, `completed`, 1.46 s; `feature_lookup` and `fraud_scoring` both `ok`; model v1, probability 0.0639895560144887 |

No manual `CLUSTER MEET` was needed at any point. Primaries are now `1724…`, `34207…` and `81d1…`: automatic in-pair failovers, with the same slot ranges.

## Rollback

`helm rollback substrate 1 -n ate-system`, revision 1 being the one read from the cluster. Pace the pods with `partition` as above. Pods restarted by the rollback no longer announce their IP, so run the manual 6-MEET recovery from [CLOUD_LAB_RUNBOOK.md](CLOUD_LAB_RUNBOOK.md#known-limitations) once. Data and node IDs stay on the PVCs either way.

If a step fails, stop changing the partition and leave the cluster as it is for diagnosis.

## Remaining limitations

| Limitation | Note |
|---|---|
| All six pods moving at once, as on a GKE wake, was **not tested live**, on the owner's instruction | That case relies on the reconciler's MEET path. It is covered by the unit simulation only; it never ran live because no stale peer appeared in single-pod moves. Watch the first wake; if `give up` appears, use the manual recovery |
| PING readiness is not cluster health | Use `cluster info` and `--cluster check` for health |
| The sidecar has no `seccompProfile` | Shows as a `restricted` PodSecurity warning (`ate-system` enforces `privileged`). Add `RuntimeDefault` in the next substrate revision |
| No pod anti-affinity | Pairs happen to sit on different nodes today. HA placement is a separate item |
| A lost PVC gives a new node ID | Deliberately not re-joined automatically |
