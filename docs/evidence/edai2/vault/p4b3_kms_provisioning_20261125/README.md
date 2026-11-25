# Vault V2 P4-B3: approved apply of the saved KMS plan, partial, stopped (2026-11-25)

Campaign `p4b3-20261125T103555Z`. Verdict: **`VAULT_V2_P4B3_PARTIAL_APPLY_STOPPED`**.

The approved saved plan (SHA-256 `19c7f9d8…68df77`) was applied **once**. **Six of seven creates
completed.** The last one, the Vault GSA's `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the key, failed
with a transient HTTP 403. Cloud KMS reported its own API as not yet enabled; that API had been enabled
by the same apply about 10 s earlier.

Per the partial-failure rule, every mutation then stopped. There was no retry, second apply, destroy,
`state rm`, import, gcloud mutation or IAM change. **A new owner decision is needed** (see *Recovery
decision*). GKE was not woken or touched. Campaign A2 has not started.

| Gate | Verdict |
|---|---|
| B3-1 Pre-apply gates | `P4B3_PRE_APPLY_GATES_PASS` (`pre-apply-gates.json`) |
| B3-2 Apply of the exact saved plan | **PARTIAL**: 6/7 created, exit 1 (`terraform-apply-summary.json`) |
| B3-3 KMS preflight `--mode cloud` | `VAULT_KMS_PREFLIGHT_BLOCKED`: only `kms-key-iam` fails (`kms-preflight-cloud.jsonl`) |
| B3-4 State | consistent with the cloud: 61 addresses = the 55 earlier ones (unchanged) + 6 new; no lock (`terraform-state-check.json`) |
| B3-5 GKE | `GKE_UNCHANGED`: 0 node VMs, the same workload pool and node pool (`gke-unchanged-check.json`) |

## Pre-apply gates (B3-1)

- **Git:** `730b98f7`, clean; no Terraform or guard change since P4-B1.
- **Saved plan:**
  - the SHA-256 recomputed equals the approved value;
  - size 51 959 bytes, mode 600 in a 700 directory;
  - a fresh `terraform show -json` is byte-identical to the reviewed JSON;
  - the guard passed again: 7 creates, 0 unrelated mutations.
- **Backend and workspace:** the same as P4-B2.
- **State:** the object generation was still `1794062463403448`, with no lock.
- **Cloud:** the KMS API was not enabled, and the GSA was NOT_FOUND.

## The apply (B3-2)

`terraform apply -input=false -no-color <private>/p4b2.tfplan`, 10:36:13–10:36:48 UTC, exit 1.

| Address (`module.vault_kms.…[0]`) | Result |
|---|---|
| `google_project_service.cloudkms` | created (26 s) |
| `google_service_account.vault_unseal` | created (15 s) |
| `google_service_account_iam_member.vault_workload_identity` | created (6 s) |
| `google_kms_key_ring.vault` | created |
| `google_kms_crypto_key.vault_unseal` | created |
| `google_kms_crypto_key_iam_member.vault_viewer` | created (6 s) |
| `google_kms_crypto_key_iam_member.vault_encrypter_decrypter` | **FAILED**: HTTP 403 while reading the key's IAM policy |

### Error classification

`API_ENABLEMENT_PROPAGATION_403`: eventual consistency right after enabling `cloudkms.googleapis.com`.
Cloud KMS's own message says to "wait a few minutes for the action to propagate" if the API was enabled
recently.

- The key ring and the key were created by the same API in the seconds before.
- The viewer member on the same key, created in parallel, succeeded.

It is **not**:

- a 409 "already exists" (the W1 concern);
- a caller permission problem;
- a quota error;
- a lock conflict.

Module design point: the IAM members depend on the key, and the key depends on the API. Nothing waits
for API propagation, so a first-ever enable can race. This is recorded for the owner, not fixed here.

The saved plan is now **stale**: the state changed, so Terraform refuses to apply it again.

## Partial state (read-only inspection)

| Resource | Observed | Status |
|---|---|---|
| Cloud KMS API | enabled | present |
| KeyRing `ewallet-vault`, `asia-southeast1` | exists | present |
| CryptoKey `vault-unseal` | ENCRYPT_DECRYPT, SOFTWARE, no rotation, primary ENABLED; 1 version, ENABLED | as planned |
| GSA `ewallet-vault-unseal@…` | exists, enabled, **0 user-managed keys** | as planned |
| Key IAM | 1 binding: `roles/cloudkms.viewer` → the GSA only, unconditional | as planned |
| Key IAM | `roles/cloudkms.cryptoKeyEncrypterDecrypter` → the GSA | **missing** |
| GSA IAM | `roles/iam.workloadIdentityUser` → exactly `…svc.id.goog[ewallet-vault/vault]` | as planned |
| Terraform state | the 6 created resources and nothing else new; the 55 earlier addresses identical | consistent |

The partial state **fails closed**: the Vault GSA can describe the key but cannot use it, and Vault is not
deployed. Nothing is at risk while it stays as it is. The only cost is about US$0.06 a month for the one
active key version.

## W2 corrected: inherited project roles (official role definitions)

P4-B1 and P4-B2 said that `roles/owner` and `roles/editor` both carry Cloud KMS use rights. **That is wrong
for `roles/editor`.** The source is `gcloud iam roles describe`, recorded in `inherited-iam-roles.json`.

| Permission | `roles/owner` (1 user) | `roles/editor` (Compute Engine default SA) |
|---|---|---|
| `cryptoKeyVersions.useToEncrypt` / `useToDecrypt` | yes | **no** |
| `cryptoKeys.setIamPolicy` | yes | **no** |
| `cryptoKeyVersions.destroy` / `restore` | yes | **no** |
| `cryptoKeyVersions.update` (enable or disable a version), `cryptoKeys.update`, `cryptoKeyVersions.create` | yes | **yes** |
| `iam.serviceAccountKeys.create` (JSON key for any GSA, including the Vault GSA) | yes | **yes** |
| `iam.serviceAccounts.disable` / `delete` | yes | **yes** |
| `container.pods.create` (a GKE pod running as KSA `ewallet-vault/vault`) | yes | **yes** |
| `resourcemanager.projects.setIamPolicy` | yes | no |

What each role means for the key:

- **Owner:** direct key use and full administration.
- **Editor:** no direct use, but two kinds of reach:
  - **availability control:** it can disable the key version, which stops auto-unseal, or disable or delete
    the GSA;
  - **indirect use paths, once the GSA holds encrypterDecrypter:** it can mint a JSON key for the Vault GSA,
    or run a pod as the Vault KSA.

Key-scoped IAM cannot narrow either role. Reducing the default editor grant remains a separate project
decision.

## Recovery decision (owner)

Not executed. Each option needs your explicit approval.

1. **Recommended: complete with a new reviewed plan of exactly one create.**
   - The API has now propagated: the preflight's KMS reads succeed.
   - Make a new saved plan with `-target=module.vault_kms` and the same inputs. Expected result: 1 create
     (`vault_encrypter_decrypter`), 0 updates, 0 deletes; the six existing addresses refresh as no-ops.
   - The P4-A guard requires all seven creates, so it **fails this plan by design**. Choose one of:
     - **(a)** a small, tested `complete` mode in the guard: the 6 existing addresses must be no-op with
       their exact attributes, and the one missing create must be exact;
     - **(b)** a manual owner review of that single create.
   - Then a separately approved apply of that exact file, and the preflight must reach `READY`.
2. **Optional hardening for fresh environments** (a separate change, not needed here because the API is
   enabled now): make the module wait after enabling the API before creating KMS resources and IAM (for
   example `time_sleep`, which needs one more provider in the mirror). It can also be left as a documented
   gotcha.
3. **Leave the partial state as it is** until you decide. It is fail-closed; Campaign A2 stays blocked
   because the GKE `vault` phase requires the preflight to pass.

**Not options:** re-applying the stale saved plan (Terraform refuses it), destroy, `state rm`, import, or
adding the binding with gcloud outside Terraform.

## Recommended owner screenshots (not taken by Claude)

1. Cloud Console → Security → Key Management: key ring `ewallet-vault` (asia-southeast1), key `vault-unseal`,
   purpose, protection level SOFTWARE, rotation "never", version 1 Enabled.
2. Key `vault-unseal` → Permissions: `Cloud KMS Viewer` for `ewallet-vault-unseal@…`, and no
   `CryptoKey Encrypter/Decrypter` yet.
3. IAM → Service accounts → `ewallet-vault-unseal`: enabled, **Keys** tab empty, **Permissions** tab showing
   `Workload Identity User` for `…svc.id.goog[ewallet-vault/vault]`.
4. APIs & Services: Cloud Key Management Service API enabled.
5. Kubernetes Engine → `ewallet-dev` → Nodes: `cpu-pool` with 0 nodes.
6. Cloud Storage → `ewallet-data-ai-platform-hg26-tfstate` → `terraform/dev/default.tfstate` object details
   (generation), without opening the content.

## Not committed

The saved plan, plan JSON, state, raw IAM policies, the apply log and tfvars stay in the private 700
directory `~/ewallet-evidence/vault/p4b3_private_20261125T103555Z/`.

## Files

| File | Content |
|---|---|
| `pre-apply-gates.json` | B3-1 gates |
| `terraform-apply-summary.json` | per-resource result, error classification, what was not done afterwards |
| `kms-resources.json` | the key ring, key, versions and GSA as observed |
| `kms-iam-summary.json` | key and GSA IAM: roles, the Vault principals, counts |
| `kms-preflight-cloud.jsonl` | `kms_preflight.py --mode cloud` gates and verdict |
| `terraform-state-check.json` | state generation, lock, addresses before and after |
| `gke-unchanged-check.json` | cluster, node pool, node VMs |
| `inherited-iam-roles.json` | the W2 correction from the official role definitions |
| `SHA256SUMS` | checksums |
