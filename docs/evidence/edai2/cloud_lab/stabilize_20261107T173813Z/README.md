# G4.5 runtime stabilization, 2026-11-07

Read-only except for the Valkey recovery. The full results table is in [G3_G4_CLOUD_CAMPAIGN.md](../../../../runbooks/edai2/G3_G4_CLOUD_CAMPAIGN.md#g45-runtime-stabilization-after-the-gke-auto-upgrade-2026-11-07).

| Path | Content |
|---|---|
| `before/gke-state.txt` | Upgrade operation, node pool, nodes, labels, PDBs, PVCs at 17:38Z |
| `valkey/before/` | Pod JSON, `CLUSTER MYID/INFO/NODES/SHARDS/SLOTS/LINKS`, `CONFIG GET cluster-announce-*`, `INFO server` (8.0.12), StatefulSet, `valkey-config`, `summary.txt`, `precheck.txt` |
| `valkey/recovery.log` | The 6 `CLUSTER MEET` commands with timestamps and replies, and the state polls |
| `valkey/after/` | The same readback after recovery: `verify.txt`, plus `client-check.txt` (cluster check, synthetic `-c` write/read/delete, replication) |
| `before/`, `after/` `*-server.log`, `kagent-controller.log` | Agent runtime before (crash loop, stale `10.216.1.17`) and after |
| `a2a/` | Synthetic typed A2A request and response, timing, per-hop logs |
| `serving/` | Runtime identities and digests, one lookup and one predict |
| `policy/maintenance-readback.txt` | Cluster maintenance policy, channel, operations, Terraform settings |
| `after/capacity-pdb.txt` | PDBs, `drift-mcp`, per-node requests, Valkey placement, Kubeflow |
| `pkg5/readiness-readback.txt` | KEDA, agent images, HPAs, fixture, scripts |

The "verified IPs" line in `recovery.log` prints the IPs in Bash map order, not pod order. The per-command lines name each target pod correctly: 0=10.216.5.31, 1=10.216.2.9, 2=10.216.1.24, 3=10.216.0.19, 4=10.216.2.11, 5=10.216.0.20.

All request data is synthetic: canonical-fixture entity IDs and `g45-synthetic-*` request IDs.
