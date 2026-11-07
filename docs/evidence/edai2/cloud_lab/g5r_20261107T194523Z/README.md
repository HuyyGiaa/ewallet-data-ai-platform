# G5-R: Valkey durable fix rollout, 2026-11-07

Runbook and results: [G5_R_VALKEY_DURABILITY.md](../../../../runbooks/edai2/G5_R_VALKEY_DURABILITY.md).

| Path | Content |
|---|---|
| `before/gke.txt`, `before/pvc.txt`, `before/helm-history.txt` | Preflight: no GKE operation, nodes Ready, six PVCs Bound, single Helm revision 1 |
| `before/valkey/` | Full readback of all six pods; `summary.txt` reports HEALTHY |
| `before/valkey-statefulset-dryrun.diff` | Server-side `helm upgrade --dry-run=server`: the only changed object, live revision 1 vs revision 2 |
| `rollout.log` | Every command and gate result in order: partition 6, Helm upgrade, partition 5 → 0, two recreations |
| `rollout/p5` … `rollout/p0` | Readback and reconciler log after each pod |
| `rollout/reconciler-logs.txt` | Reconciler output on all pods after the rollout (only transient `skip` lines) |
| `recreate-2/`, `recreate-3/` | Readback after each single-pod recreation |
| `after/valkey/`, `after/cluster-check.txt`, `after/existing-keys.txt` | Final topology, `--cluster check`, existing key names (IDs masked) |
| `after/agent-runtime.txt`, `after/ate-api-errors.txt` | `ate-api-server` and `kagent-controller` with no new restart; error lines are only OTel exporter noise |
| `after/a2a/` | Synthetic A2A request, response, timing, MCP tool log |

The synthetic Valkey keys (`g5r:synthetic:*`) were deleted after each check, and the A2A request used the canonical synthetic fixture.
