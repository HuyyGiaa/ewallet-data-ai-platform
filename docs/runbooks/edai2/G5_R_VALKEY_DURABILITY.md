# G5-R: Valkey survives pod moves (plan, not applied)

**Status: required, planned, awaiting owner approval.** Nothing in this page has been applied. G5-C (agent KEDA) waits for G5-R to be implemented and validated.

Until G5-R is applied, every GKE wake, node upgrade or node repair needs the manual 6-MEET recovery in [CLOUD_LAB_RUNBOOK.md](CLOUD_LAB_RUNBOOK.md#known-limitations). That recovery is not a fix.

## Root cause (confirmed in G4.5)

| Fact | Evidence |
|---|---|
| Valkey 8.0.12, cluster mode, `nodes.conf` on a PVC | `docs/evidence/edai2/cloud_lab/stabilize_20261107T173813Z/valkey/before/` |
| `valkey.conf` has no `cluster-announce-ip`; the container runs `valkey-server /etc/valkey/valkey.conf` | `valkey-config` ConfigMap, StatefulSet readback |
| After pods move, every node announces its **old** IP for itself and holds old IPs for its peers | `summary.txt`: all six `myself` addresses stale |
| `ate-api-server` (go-redis cluster client) dials a stale address and crash-loops; `kagent-controller` crash-loops because it cannot reach `ate-api` | `before/ate-api-server.log`, `before/kagent-controller.log` |

Two separate gaps cause this:
1. **Self-address.** A node learns its own IP only from an incoming `CLUSTER MEET`.
2. **Peer addresses.** When several pods move at once, which happens on every wake and node upgrade, every peer address in every `nodes.conf` is stale. No node can reach another, so gossip cannot repair it.

Announcing the pod IP fixes only gap 1. The fix must also rediscover peers.

## Source of truth

`valkey-cluster` belongs to the Helm release `substrate` (chart `substrate-0.0.9`, digest-pinned in [versions.env](../../../infra/kubernetes/kagent-pilot/versions.env)). It is rendered by [render.sh](../../../infra/kubernetes/kagent-pilot/scripts/render.sh) with [values/substrate.yaml](../../../infra/kubernetes/kagent-pilot/values/substrate.yaml).

The chart has no knob for Valkey arguments, environment or sidecars. The repository already owns a post-renderer for chart gaps, [substrate_resource_post_renderer.py](../../../infra/kubernetes/kagent-pilot/scripts/substrate_resource_post_renderer.py), which pins images and resources. G5-R extends that file. Nothing is patched by hand on live pods.

## Proposed change

The exact diffs are [g5r/post_renderer.diff](g5r/post_renderer.diff) (source) and [g5r/valkey-statefulset.diff](g5r/valkey-statefulset.diff) (the rendered StatefulSet). The second was rendered offline from the pinned chart. In that render no other object changes, apart from certificates the chart regenerates in every offline render (see the dry-run step).

| Part | Change | Why |
|---|---|---|
| `valkey` container | `POD_IP` from the Downward API; command becomes `valkey-server /etc/valkey/valkey.conf --cluster-announce-ip $(POD_IP)` | The node announces its real address from the first second, even before any peer contacts it |
| `valkey` container | Readiness probe `valkey-cli -h 127.0.0.1 ping` | The pod's DNS record appears only when the process answers. It is deliberately **not** `cluster_state:ok`: that would hide every pod from DNS after a full restart and deadlock rediscovery |
| New sidecar `cluster-address-reconciler` | Same pinned Valkey image, 5m/16Mi. Every 15 s it resolves `valkey-cluster-{0..5}.valkey-cluster-service` and sends `CLUSTER MEET` to a pod only when that pod's address is not already known **and** its `CLUSTER MYID` is already a member of this node's table | Repairs stale peer addresses after any number of pods move. A pod with an unknown node ID is never introduced, so membership changes stay a human decision |
| Fail-closed guard | The post-renderer raises an error if upstream changes the Valkey command or adds args | An upgraded chart cannot silently drop or double the patch |

How the existing state is handled:

| Concern | Outcome |
|---|---|
| Persistent `nodes.conf` | Kept. Node ID, slots, roles and epochs load from it as today. `--cluster-announce-ip` overrides only the node's own announced address, and MEETs update only peer addresses. |
| Node IDs | Unchanged: the same PVCs. |
| Slots | No reshard, no slot command. Each shard keeps its range. |
| Primary/replica | A restarted primary can fail over to its replica (node timeout 5 s), as seen in G4.5. The pair and range stay the same. |
| Gossip | Correct addresses propagate by gossip once one MEET lands. |
| Data | Stays on the PVC (AOF). No flush, no reset. |
| Helm ownership | One `helm upgrade` of `substrate` with the same chart digest and values. The chart does not set `updateStrategy`, so a temporary `partition` patch survives the upgrade. |

**Not in G5-R:** pod anti-affinity or topology spread. All six pods once sat on one node, which is a single failure domain. The scheduler spreads them today only by chance. This is a separate HA item; G5-R fixes the addressing failure only.

## Rollout (needs approval; about 30 minutes)

1. **Baseline.** Run the G4.5 readback (`cluster info`, `nodes`, `shards`, `myid` on all six pods; `--cluster check`; key counts). Continue only if `cluster_state:ok` and the six known node IDs are present.
2. **Repository.** Apply the post-renderer diff and add a unit test: render, then assert the command, the env, the sidecar and the fail-closed guard. Commit.
3. **Server-side dry run.** Run `helm upgrade substrate oci://ghcr.io/kagent-dev/substrate/helm/substrate --version 0.0.9 -n ate-system -f infra/kubernetes/kagent-pilot/values/substrate.yaml --post-renderer infra/kubernetes/kagent-pilot/scripts/substrate_resource_post_renderer.py --dry-run=server`. Diff its manifest against `helm get manifest substrate`.
   - **The only allowed difference is the `valkey-cluster` StatefulSet.** The chart's `lookup` keeps the existing TLS and JWT Secrets and the CA ConfigMap.
   - Stop if any other object differs.
4. **Hold the rollout.** `kubectl -n ate-system patch sts valkey-cluster --type merge -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":6}}}}'`.
5. **Upgrade.** Run the same `helm upgrade` without `--dry-run`. No pod restarts yet.
6. **One pod at a time,** from ordinal 5 down to 0: set `partition` to N. Wait for `valkey-cluster-N` to be Ready with 2/2 containers, then check:
   - its `myself` address equals its pod IP;
   - all six nodes report `cluster_state:ok` and 16,384 slots;
   - the same six node IDs;
   - `--cluster check` reports OK.

   Stop and roll back on any failure.
7. **Controlled recreation test.**
   - **Single pod:** delete one replica pod. It returns with a new IP and the cluster heals by itself within 30 s.
   - **Every pod at once:** this is the case behind the original failure. Delete all six pods, the way a wake does. The cluster must reach `ok` with no manual MEET, with the same IDs and data.

   This step needs the owner's go-ahead in the approval.
8. **Clients.** `ate-api-server` and `kagent-controller` become Ready with no restart; one synthetic A2A request completes.
9. Set `partition` back to 0 if it is not already.

## Acceptance (all required)

1. Every `myself` address equals its pod IP right after pod creation.
2. The six node IDs are unchanged.
3. No slot conflicts; `--cluster check` reports OK.
4. All 16,384 slots OK.
5. Key counts per shard as before; existing keys readable.
6. A cluster-mode client (`valkey-cli -c`) writes and reads in all three shards, then deletes its keys.
7. The cluster heals by itself after the single-pod and all-pods recreation tests.
8. `kagent-controller` is Ready and a synthetic A2A request completes.

## Rollback

1. `helm rollback substrate 1 -n ate-system`: the StatefulSet returns to today's spec. Use `partition` to pace the pods the same way.
2. Pods restarted by the rollback get new IPs and no longer announce them. Run the manual 6-MEET recovery once.
3. Data and node IDs are untouched in both directions, because `nodes.conf` and the AOF stay on the PVCs.

## Remaining risks

| Risk | Mitigation |
|---|---|
| An IP reused by another member after a move makes the reconciler skip that address | Rare, because pod IPs come from per-node ranges. The next pass corrects it once the stale entry is gone. If not, use the manual MEET. |
| A lost PVC gives a pod a new node ID | The reconciler refuses to introduce it, by design. Recovery stays a manual, approved procedure. |
| A primary restart fails over to its replica | Expected; the slot range does not change. Optionally run `CLUSTER FAILOVER` on the replica before restarting a primary. |
| A future chart version changes the container | The post-renderer fails closed. |
