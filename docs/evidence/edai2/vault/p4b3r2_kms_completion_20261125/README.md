# Vault V2 P4-B3R2: KMS IAM completion and P4 closure (2026-11-25)

Campaign `p4b3r2-20261125T111338Z`. Verdict: **`VAULT_V2_P4_CLOUD_INFRASTRUCTURE_COMPLETE`**.

The owner-approved one-create plan from P4-B3R1 was applied **once**. The missing
`roles/cloudkms.cryptoKeyEncrypterDecrypter` grant now exists, all seven P4 resources match the approved
configuration, and the Cloud KMS preflight is **`VAULT_KMS_PREFLIGHT_READY`**.

There was no other mutation: no new plan, no second apply, no state command, no gcloud IAM or KMS change, no
GKE wake, no kubectl. **P4 means the cloud infrastructure only.** Vault has not used the key on GKE yet
(see *Limitations*).

| Gate | Verdict | File |
|---|---|---|
| R2-A Pre-apply integrity | `P4B3R2_PRE_APPLY_PASS` (10/10 checks) | `pre-apply-gates.json`, `completion-guard-result.json` |
| R2-B IAM recovery apply | `P4B3R2_IAM_APPLY_PASS`: 1 added, 0 changed, 0 destroyed | `recovery-apply-summary.json` |
| R2-C Cloud KMS resources | `P4B3R2_KMS_RESOURCES_PASS`: 7/7 | `kms-resource-inventory.json`, `kms-iam-summary.json` |
| R2-D KMS preflight | **`VAULT_KMS_PREFLIGHT_READY`**: 11/11 gates PASS | `kms-preflight-cloud.jsonl` |
| R2-E Terraform and GKE safety | `P4B3R2_INFRASTRUCTURE_SAFETY_PASS` | `terraform-state-check.json`, `gke-unchanged-check.json` |
| R2-F P4 closure | `P4_CLOSURE_DOCS_AND_EVIDENCE_PASS` | this README, the runbook step 0, VAULT.md |

## The incident and its resolution

The P4 records, in order:

| Phase | Record | Result |
|---|---|---|
| P4-B2 | [`p4b2_real_plan_20261125`](../p4b2_real_plan_20261125/README.md) | 7-create saved plan, guard PASS, approved |
| **P4-B3** | [`p4b3_kms_provisioning_20261125`](../p4b3_kms_provisioning_20261125/README.md) | **`VAULT_V2_P4B3_PARTIAL_APPLY_STOPPED`**: 6/7 created. Kept as it happened |
| P4-B3R1 | [`p4b3r1_completion_plan_20261125`](../p4b3r1_completion_plan_20261125/README.md) | completion guard mode; a one-create plan, guard PASS |
| P4-B3R2 | this directory | the one create applied; 7/7; preflight READY |

1. **Failure.** The first apply enabled `cloudkms.googleapis.com` and, about 10 s later, created the key ring,
   the key, the viewer member and (in parallel) the encrypterDecrypter member. That last create's read of the
   key's IAM policy got HTTP 403: Cloud KMS said its API was not enabled, and to wait a few minutes if it was
   enabled recently.
2. **Root cause.** API enablement propagates through Google's backends with eventual consistency.
   - Terraform's dependency chain (API → key ring → key → IAM member) guarantees ordering only, not
     readiness.
   - The `google_project_service` create returned as soon as Service Usage reported the service enabled.
     Some Cloud KMS methods (here `getIamPolicy`) were not yet served everywhere. Others (create key ring,
     create key, and the viewer member's IAM call) already were.
   - Classified `API_ENABLEMENT_PROPAGATION_403`: not a permission, quota, 409 or lock problem.
3. **Containment.**
   - Every mutation stopped.
   - Read-only inspection showed a consistent state of six resources, fail-closed (the GSA could not use
     the key).
   - The stale saved plan could not be reapplied.
4. **Recovery.**
   - An explicit guard mode (`guard --mode complete`) allows exactly the one missing create, on the exact
     existing key, over a verified six-resource baseline.
   - The first real recovery plan failed that guard on two provider representations: `rotation_period = ""`,
     and a refresh changing `labels` from null to `{}`. The guard was fixed narrowly, regression-tested and
     the plan regenerated.
   - The resulting plan was reviewed, approved and applied here once.
5. **Resolution.**
   - The grant was created in 5 s.
   - The state gained exactly that address (62 = 55 + 7). All earlier resource values are identical to the
     reviewed prior state.
   - The only state-only change is the accepted `labels` normalisation on the key.
   - The preflight's `kms-key-iam` gate, the only P4-B3 blocker, now passes.

## Pre-apply (R2-A) and apply (R2-B)

**Pre-apply checks (R2-A):**

- **Git:** `d3204975`, clean. Since the plan's commit `4860f0f0`, only `docs/` changed; the module,
  environment, guard, preflight and lock file are identical.
- **Plan file:** `p4b3r1.tfplan`, 54 069 bytes, mode 600 in a 700 directory, SHA-256
  `d87d37d4b38fd51a8e3dcd328986975fa00f72b6e7d2fd79cfe9a5de9154cc72`. A fresh `terraform show -json` of
  that same binary is byte-identical to the reviewed JSON (`72e65908…e0d0690`).
- **Backend:** gcs `ewallet-data-ai-platform-hg26-tfstate`, `terraform/dev`, workspace `default`, Terraform
  1.16.6, google 7.46.1.
- **State:** generation `1795603007838257/1` and no lock. Both the SHA-256 and the generation were re-checked
  in the same command, immediately before the apply.
- **Cloud:** the six resources as approved; the key held only the viewer binding.
- **Guard:** `VAULT_KMS_COMPLETION_PLAN_GUARD_PASS` with 1 create, 6 unchanged, and only the accepted
  normalisation.

**The apply (R2-B):** `terraform apply -input=false -no-color <private>/p4b3r1.tfplan`, 11:14:09–11:14:17 UTC,
exit 0.

- `vault_encrypter_decrypter[0]: Creation complete after 5s`.
- `Apply complete! Resources: 1 added, 0 changed, 0 destroyed.`

## Final inventory (R2-C)

| # | Address (`module.vault_kms.…[0]`) | Observed | Status |
|---|---|---|---|
| 1 | `google_project_service.cloudkms` | `cloudkms.googleapis.com` enabled | PASS |
| 2 | `google_kms_key_ring.vault` | `ewallet-vault`, `asia-southeast1`, project `ewallet-data-ai-platform-hg26` | PASS |
| 3 | `google_kms_crypto_key.vault_unseal` | `vault-unseal`, ENCRYPT_DECRYPT, SOFTWARE, no rotation, version 1 ENABLED (primary) | PASS |
| 4 | `google_service_account.vault_unseal` | `ewallet-vault-unseal@…`, enabled, 0 user-managed keys | PASS |
| 5 | `google_kms_crypto_key_iam_member.vault_viewer` | `roles/cloudkms.viewer` → the GSA only, unconditional, on the key; unchanged | PASS |
| 6 | `google_kms_crypto_key_iam_member.vault_encrypter_decrypter` | `roles/cloudkms.cryptoKeyEncrypterDecrypter` → the GSA only, unconditional, on the key; **new** | PASS |
| 7 | `google_service_account_iam_member.vault_workload_identity` | `roles/iam.workloadIdentityUser` → `…svc.id.goog[ewallet-vault/vault]` only, on the GSA | PASS |

There are no key-ring bindings, no project-level `roles/cloudkms.*`, and no project role for the GSA. The
inherited `roles/owner` and `roles/editor` are unchanged, as corrected in P4-B3 (W2).

## Preflight (R2-D)

`kms_preflight.py --mode cloud --vault-chart vault-0.34.1.tgz` passed all 11 gates on the first run, so no
retry was needed:

- `vault-seal-config`, `vault-ksa-annotation`, `vault-ksa-rendered`;
- `kms-api-enabled`, `kms-key-ring`, `kms-crypto-key`, `kms-key-versions`;
- **`kms-key-iam`** (the GSA holds encrypterDecrypter + viewer; 0 other principals with use rights);
- `vault-gsa`, `vault-gsa-no-json-key`, `vault-workload-identity`.

The verdict is **`VAULT_KMS_PREFLIGHT_READY`**, with nothing blocking and nothing NOT_CHECKED.

## Terraform state and GKE (R2-E)

**Terraform state:**

- generation `1795603007838257` → `1795605257706973` (metageneration 1); no lock left;
- 61 → 62 addresses: the 55 earlier ones identical, 7 under `module.vault_kms`, 1 added, 0 removed;
- every earlier resource's values equal the reviewed plan's refreshed prior state;
- the only state-only change is `labels` null → `{}` on the key.

No full plan and no refresh-only apply was run.

**GKE:** `ewallet-dev` RUNNING, workload pool unchanged, and `cpu-pool` identical before and after (GKE_METADATA,
autoscaling disabled, same version). There were 0 node VMs and no GKE operation during the campaign.

## Preventing the propagation failure on a future fresh install

| | H1: two-stage provisioning (**recommended**) | H2: a Terraform wait (`time_sleep`) |
|---|---|---|
| Change to the module | none | a new resource and a new provider (`hashicorp/time`) in the lock file and mirror |
| Guard | needs two explicit modes, one per stage | the allowlist grows and the seven-resource state gains an eighth |
| Readiness | **observed**: a bounded probe of the real API, fail-closed | **assumed**: a fixed delay; propagation time is not bounded |
| Existing state | untouched | a plan against the live state would add the wait resource |
| Maintenance | one probe and two plans | an extra provider to keep and audit |

**Recommendation: H1.** Its procedure for a fresh environment, with every stage separately approved:

1. **Stage 1:** a saved plan targeted at
   `module.vault_kms.google_project_service.cloudkms[0]` only, guarded, approved and applied.
2. **Readiness:**
   - run `python3 scripts/vault/kms_preflight.py --mode api-ready [--required 5 --interval 30 --timeout 900]`.
     This new mode is read-only. It probes `gcloud kms keyrings list` until 5 consecutive probes succeed,
     30 s apart; any failure resets the count;
   - `VAULT_KMS_API_READY` → continue;
   - `VAULT_KMS_API_NOT_READY` (the timeout) → **stop**: no stage-2 plan; wait and re-run the probe, or
     escalate.
3. **Stage 2:** a new saved plan for the six remaining resources, from the post-stage-1 state, guarded,
   reviewed, approved and applied once.
4. **Afterwards:** the preflight `--mode cloud` must be READY.

**Not yet implemented:** the guard modes for stage 1 (the API create only) and stage 2 (six creates on an
API-only baseline). They are a separate, reviewed change, and a prerequisite before any fresh install.

**Not proven:** H1 has **not** been run in a fresh environment. A stable probe lowers the risk that a Cloud
KMS method is still unready, but it does not prove every method is. The partial-apply rule stays in force.

**After any partial failure,** never retry blindly:

- a saved plan applied once is stale, and Terraform refuses it;
- a plain `terraform apply` re-plans unreviewed, and in this dev stack a full plan touches the drifted GKE
  node pool, or with `enable_gke = false` would destroy the cluster.

The path is always:

1. stop and inspect read-only;
2. a new targeted saved plan from the current state;
3. a guard mode written for that exact remaining change;
4. owner review of its SHA-256;
5. one apply.

## Tests

| Command | Result |
|---|---|
| `pytest tests/ops/test_vault_kms_terraform.py tests/ops/test_r2_terraform.py tests/ops/test_vault_kms_preflight.py`, with the opt-in real offline plan | **195 passed** (the seven-create guard, the completion mode, R2, the preflight including 5 new `api-ready` tests) |
| `pytest tests/ops tests/inference/test_vault.py` | **1075 passed, 2 skipped** |
| `terraform fmt -recursive -check`; `init -backend=false` + `validate` (bootstrap, dev) on a scratch copy; `tests.contract.test_terraform_foundation` | PASS; valid; 15 OK |
| `git diff --check`, Markdown links, secret scan | PASS |
| `--mode api-ready --required 3 --interval 5 --timeout 60` against the live, already-enabled API | `VAULT_KMS_API_READY` (positive control only, `kms-api-ready-probe.json`) |

## Recommended owner screenshots (take them yourself; not taken by Claude)

Name them `p4b3r2-NN-<subject>.png`, record `sha256sum p4b3r2-*.png` next to them, and never edit them.
Keep private material (state, plan, tokens) out of the frame. They are not required for the
infrastructure verdict above.

1. Security → Key Management: key ring `ewallet-vault` (`asia-southeast1`) → `p4b3r2-01-keyring.png`.
2. Key `vault-unseal`: SOFTWARE, ENCRYPT_DECRYPT, rotation "never", version 1 Enabled (primary) →
   `p4b3r2-02-cryptokey.png`.
3. Key `vault-unseal` → Permissions: `Cloud KMS CryptoKey Encrypter/Decrypter` **and** `Cloud KMS Viewer` for
   `ewallet-vault-unseal@…` → `p4b3r2-03-key-permissions.png`.
4. IAM → Service accounts → `ewallet-vault-unseal`: enabled; Keys tab empty → `p4b3r2-04-gsa-keys.png`.
5. The same GSA → Permissions: `Workload Identity User` for `…svc.id.goog[ewallet-vault/vault]` →
   `p4b3r2-05-gsa-workload-identity.png`.
6. Terminal: `Apply complete! Resources: 1 added, 0 changed, 0 destroyed.`. Show it from the private
   `apply.log` with `grep 'Apply complete'`, never with the state → `p4b3r2-06-apply.png`.
7. Terminal: `python3 -m ops.gcp.vault_kms_terraform guard --mode complete --plan <private>/p4b3r1.plan.json`
   → `VAULT_KMS_COMPLETION_PLAN_GUARD_PASS` → `p4b3r2-07-guard.png`.
8. Terminal: `python3 scripts/vault/kms_preflight.py --mode cloud --vault-chart …` →
   `VAULT_KMS_PREFLIGHT_READY` → `p4b3r2-08-preflight.png`.
9. Kubernetes Engine → `ewallet-dev` → Nodes: `cpu-pool` with 0 nodes → `p4b3r2-09-gke-nodes.png`.

## Limitations (what P4 does not prove)

- **Real Vault auto-unseal with Cloud KMS on GKE: NOT VERIFIED.** Vault has never used the key; on kind,
  auto-unseal was `SIMULATED_AUTO_UNSEAL`.
- **Runtime Workload Identity token exchange** from `ewallet-vault/vault` to the GSA: NOT VERIFIED (the
  binding exists).
- **V2 recovery from GKE Persistent Disk and across a GKE sleep and wake:** NOT VERIFIED.
- **The existing dev Terraform drift** (`cpu-pool` autoscaling state 0–3 vs disabled live; network
  differences) is **unresolved**. Only targeted, guarded saved plans are safe.
- **Production credentials** have not been migrated; nothing of Vault or ESO V2 is deployed on GKE.
- The H1 fresh-install procedure is documented, but its stage guard modes are not implemented and it has not
  been tested in a fresh environment.
- The inherited project roles (W2) are unchanged and remain a separate decision.

Next: **P5, Vault V2 GKE Campaign A2** (synthetic secrets, real KMS auto-unseal, owner approval). Not started.

## Not committed

The binary plans, plan JSON, state, tfvars, apply log and raw IAM policies stay in the private 700
directories `~/ewallet-evidence/vault/p4b3r1_private_20261125T105200Z/` and
`~/ewallet-evidence/vault/p4b3r2_private_20261125T111338Z/`.

## Files

| File | Content |
|---|---|
| `pre-apply-gates.json` | the ten R2-A checks |
| `completion-guard-result.json` | the guard output on the fresh JSON of the approved plan |
| `recovery-apply-summary.json` | command, times, exit code, the create, counts |
| `kms-resource-inventory.json` | the seven resources as observed |
| `kms-iam-summary.json` | key, key-ring, project and GSA IAM: roles, Vault principals, counts |
| `kms-preflight-cloud.jsonl` | the preflight gates and READY verdict |
| `terraform-state-check.json` | generations, lock, addresses, value comparison |
| `gke-unchanged-check.json` | cluster, node pool, node VMs, GKE operations |
| `kms-api-ready-probe.json` | the new readiness probe's positive control |
| `SHA256SUMS` | checksums |
