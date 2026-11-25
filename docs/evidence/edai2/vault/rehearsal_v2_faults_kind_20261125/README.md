# Vault V2 fail-fast and interrupted-recovery cleanup on kind (2026-11-25)

`ENVIRONMENT=kind` · `AUTO_UNSEAL=SIMULATED_AUTO_UNSEAL` · **KIND PASS; not GKE evidence.** Every value is synthetic.

These runs repeat the Campaign A+ fault set ([V1 runs](../rehearsal_aplus_faults_kind_20261125/README.md)) against
the **V2** driver, with the transit seal, the shared ClusterSecretStore and recovery keys. The same
`scripts/vault/gke_rehearsal.sh` (sha256 `15cf46275dd943f1…`) produced the full campaign
[`rehearsal_v2_kind_vault-rehearsal-20261125T074917Z`](../rehearsal_v2_kind_vault-rehearsal-20261125T074917Z/README.md).

Each of the five runs had a fresh ID and went through these steps:

1. `preflight` → `vault` → `init` → `configure` → `eso` → `consumer`;
2. `REHEARSAL_FAULT=<point> retain`, which must exit 1 with a `fault-injected` FAIL;
3. then it checked that:
   - the next normal phase (`lifecycle`) is refused with exit 2 ("this run has stopped");
   - `gate retain` stays closed (exit 1);
   - `inspect` reports the volume state;
   - `cleanup`, `verify-clean` and `gate clean` pass.

| Fault point | State seen by `inspect` (audit / data) | Cleanup | Duration |
|---|---|---|---|
| `retain-set` | bound / bound | PASS | 127 s |
| `pvc-deleted:audit-vault-0` | released / bound | PASS | 191 s |
| `pvcs-released`, then a cleanup stopped at `cleanup-reclaim-restored` and run again | released / released | the first cleanup stopped as injected (exit 1); the retry passed | 198 s |
| `claimref-removed:audit-vault-0` | unclaimed / released | `recovery-completed` first, then PASS | 199 s |
| `rebound:audit-vault-0` | bound / released | PASS | 186 s |

After the last run, kind had its original releases and nothing from the runs: no Vault, ESO or rehearsal
namespace, no CRD, no ClusterSecretStore, no PV, no port-forward and no private material.

## Files

| File | What it holds |
|---|---|
| `faults.txt` | the runner's output: every command, its exit code, the `inspect` volume state |
| `fault-results.jsonl` | every record of the five runs (field `run` = the run ID). The only FAIL records are the six injected faults (`fault-injected`) and the five closed `gate-retain`, as designed |
| `SHA256SUMS` | checksums |

The states kind cannot reach on its own are covered by stub tests in `tests/ops/test_vault_rehearsal.py`:

- a recreated PVC that never binds;
- a volume already gone;
- an orphaned or unreadable disk;
- an unrecorded or foreign PV;
- a ClusterSecretStore not created by the rehearsal (V2).

## Limits

- kind local-path volumes stand in for Persistent Disks; disk absence (HTTP 404) is GKE-only.
- The guarded sleeps of the GKE procedure are cloud-lab commands that were not run.
