# Campaign A+ fail-fast and interrupted-recovery cleanup on kind (2026-11-25)

These runs check the two safety fixes in [VAULT_GKE_REHEARSAL.md §9](../../../../runbooks/edai2/VAULT_GKE_REHEARSAL.md#9-campaign-a-the-feature-api-release-storage-and-wake):

- cleanup works from the state of each recorded Vault volume;
- the run stops at its first failure.

**KIND PASS; this is not GKE evidence.** Every value is synthetic.

| | |
|---|---|
| Cluster | kind `kind-llm-agent-local`. cert-manager v1.16.2 was installed for the runs, then removed together with its CRDs |
| Software | Vault 2.0.4 (chart 0.34.1), ESO v2.14.0, the serving chart as release `rehearsal-serving`, the Feature API image `localhost:5001/ewallet-inference@sha256:8a6106c3…` |
| Script | `scripts/vault/gke_rehearsal.sh` sha256 `1671c1882f587a80…`, `scripts/vault/rehearsal/volume_states.jq` `c3e62d169c7465f5…` (the committed versions) |
| Afterwards | kind has its 6 original releases. No Vault, ESO, cert-manager or rehearsal namespace, CRD or PV is left, and no port-forward or private material |

## 1. Full A+ sequence through the §9 gates: `vault-rehearsal-20261125T043059Z`

The phases ran in the order of block 2: `vault` → `retain`. Then:

- `gate retain` (twice: once at the end of block 2, once at the start of the next block);
- a simulated wake: `vault-0` and the Redis pod deleted;
- `after-wake`, then `gate after-wake`;
- `cleanup` → `verify-clean` → `gate clean`.

**Result:** 362 s, 97 records, 0 FAIL ([`aplus-results.jsonl`](aplus-results.jsonl)).

**Lookups:** all seven returned `BASELINE_OK` with HTTP 200 and 31 features: baseline, switch,
rollback, re-upgrade, while sealed, after the Released-PV recovery, and after the wake.

**Cleanup:** the volumes were `bound`/`Retain`. They went back to `Delete`, then were deleted with
their PVCs.

## 2. Retain drill stopped at a fault point, then cleanup: five runs

Each run executed `preflight` → `consumer`, then `REHEARSAL_FAULT=<point> retain`. Each run then
checked that:

- the fault was hit (`fault-injected` FAIL, exit 1);
- the next normal phase (`lifecycle`) was refused (exit 2, "this run has stopped (retain failed at fault-injected)");
- `gate retain` stayed closed;
- `inspect` reported the state;
- `cleanup`, `verify-clean` and `gate clean` passed, and none of the recorded PVs was left.

The output is in [`faults.log`](faults.log) and every record in [`fault-results.jsonl`](fault-results.jsonl).

| Fault point | State seen by `inspect` (audit / data) | Cleanup |
|---|---|---|
| `retain-set` | bound / bound (both `Retain`) | PASS, 176 s |
| `pvc-deleted:audit-vault-0` | released / bound | PASS, 175 s |
| `pvcs-released`, then a cleanup stopped at `cleanup-reclaim-restored` and run again | released / released | first run stopped as injected; the retry PASS, 187 s |
| `claimref-removed:audit-vault-0` | unclaimed (PV `Available`) / released | `recovery-completed` first, then PASS, 179 s |
| `rebound:audit-vault-0` | bound / released | PASS, 179 s |

The two states kind cannot reach on its own have stub tests in `tests/ops/test_vault_rehearsal.py`:

- a recreated PVC that is not bound yet;
- a cleanup retried after a volume was already gone.

Those tests also cover every refusal: an unrecorded PV, a PV claimed by another PVC, a Lost PVC, a
`Failed` PV, a stale claimRef, a missing namespace, a PVC that never binds, a missing
`volumes.json`, an orphaned disk, and an unreadable disk.

## Limits

- kind's local-path volumes stand in for Persistent Disks. A disk proven absent by a 404, and a PD
  detaching and re-attaching across a node-pool sleep, are only checked on GKE.
- The guarded sleeps and wakes in blocks 1, 3, 4 and 6 are cloud-lab commands that were not run here.
