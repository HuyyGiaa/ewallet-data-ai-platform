# Vault V2 P5-A: GKE Campaign A2 readiness (2026-11-28)

**Verdict: `VAULT_V2_P5A_BLOCKED`.** The project's billing account is closed.

Because of that:

- Cloud KMS refuses every key query (`FAILED_PRECONDITION: Billing is disabled`).
- The GKE API refuses the cluster describe (HTTP 403, "requires billing to be enabled").
- The required KMS preflight is `VAULT_KMS_PREFLIGHT_BLOCKED`, not READY.

P5-A §2 says to stop P5 preparation in that case, so the campaign audit, execution matrix and
screenshot plan were **not** prepared.

Every check was read-only. No kubectl ran against GKE, nothing was woken or changed, and no
credential was read. The account, the billing account ID and the project number are kept out of
this record.

| Gate | Verdict | Observed |
|---|---|---|
| P5A-1 P4 prerequisite | **`P5A_KMS_READY_FAIL`** | [`kms-gates-cloud.jsonl`](kms-gates-cloud.jsonl): 7 PASS (repository seal and KSA, the rendered ServiceAccount, the API listed as enabled, the GSA enabled with no JSON key, Workload Identity exactly `ewallet-vault/vault`); 4 BLOCKED: key ring, key, key versions, key IAM. A blocked query concludes nothing; P4's 7/7 result (2026-11-25) is not contradicted, only unverifiable now |
| P5A-2 GKE environment | **BLOCKED** | cluster describe: 403 (billing). Still readable through Compute: the `cpu-pool` instance group at size 0 and no VM; 16 disks, 90 GB, all `READY`, the same 16 `pvc-` disks as the newest recovery manifest (none missing, none new) |
| P5A-3 to P5A-6 | NOT_STARTED | stopped by P5-A §2 |

Billing as reported by the read-only APIs:

- the project is linked to a billing account (`billingEnabled: true`);
- that account reports `open: false`.

It was still working on 2026-11-25 at 11:14Z, when the P4 apply ran. The cause and the date of the
closure are not visible to these queries.

**Newest recovery manifest (local):** `~/ewallet-evidence/vault/vault-rehearsal-20261124T173156Z/recovery-manifest`.

| | |
|---|---|
| Recorded | 2026-11-24T18:04:36Z |
| Identity | `ewallet-data-ai-platform-hg26` / `ewallet-dev` / `asia-southeast1-b` / `cpu-pool` |
| Content | 5 pool VMs, 83 workloads, no Vault or ESO workload |
| Newer manifests | none on the laptop |
| Live validation | DEFERRED: the GKE API is blocked |

## Owner decision needed

The owner must restore billing in the Google Cloud console, under *Billing*: reopen the account, or
link the project to an open account. The owner should also review what Google's policy for a
closed billing account means for the project's disks, buckets and KMS key.

After that, P5-A must start again from §2. That means:

- `kms_preflight.py --mode cloud` must be READY;
- the cluster describe must succeed;
- the node pool must still show 0 nodes;
- the 16 disks must still match the manifest.

Nothing else in this campaign can proceed before that.
