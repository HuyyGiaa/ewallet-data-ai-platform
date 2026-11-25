# Vault V2 P4-B2: real targeted saved plan, strict guard, owner review package (2026-11-25)

Campaign `p4b2-20261125T102630Z`. Verdict: **`VAULT_V2_P4B2_PLAN_READY_FOR_OWNER_APPROVAL`**.
**This verdict does not authorize apply.** P4-B3 needs your explicit approval of the exact plan below.

What was done: one real `terraform plan` against the existing dev backend, saved privately; its JSON shown
privately; the unchanged P4-A guard. What was not done: no apply, import, refresh, state command,
force-unlock or backend migration; no API enabled; no IAM changed; no KMS resource created; no GKE wake; no kubectl.

| Gate | Verdict |
|---|---|
| B2-A Environment | `P4B2_ENVIRONMENT_PASS` |
| B2-B Saved plan | `P4B2_SAVED_PLAN_PASS` |
| B2-C Strict guard | `P4B2_STRICT_GUARD_PASS` |
| B2-D Integrity and safety | `P4B2_INTEGRITY_AND_SAFETY_PASS` |
| B2-E Owner review package | `P4B2_REVIEW_PACKAGE_PASS` |

## Environment

| | |
|---|---|
| Git | `experiment/edai2-vault-clustersecretstore` at `cd112b58`, clean; guard source unchanged since P4-A |
| Terraform / provider | 1.16.6 / hashicorp/google 7.46.1 (local filesystem mirror; matches `.terraform.lock.hcl`, init with `-lockfile=readonly`) |
| Root | `infra/terraform/gcp/environments/dev` of this branch, never initialised before: fresh `init` with `-backend-config` bucket and prefix. No `-reconfigure`, no `-migrate-state`, no local state |
| Backend / workspace | GCS `ewallet-data-ai-platform-hg26-tfstate`, prefix `terraform/dev`, workspace `default` (`terraform-backend-check.json`) |
| Project | `ewallet-data-ai-platform-hg26`, explicit; gcloud account and configuration unchanged |
| Inputs | `-var enable_vault_kms=true`; private tfvars `enable_gke = true` (the live cluster exists); everything else defaults. A plan targeted at `module.vault_kms` evaluates only that module, whose inputs are `project_id` and `enable_vault_kms`. No value came from reading the state |
| Concurrency | no lock object before; the plan ran with `-lock-timeout=0s` (stop, never wait); no lock object after |

## The plan

| Field | Value |
|---|---|
| Plan type | saved, targeted (`-target=module.vault_kms`), normal refresh |
| Prior state | the real dev state: 55 resources, none under `module.vault_kms` |
| Creates / updates / deletes / replacements | **7 / 0 / 0 / 0** |
| Imports / deferred / outside `module.vault_kms` | 0 / 0 / 0 |
| Guard | **`VAULT_KMS_PLAN_GUARD_PASS`**: 7 accepted creates, 0 rejected operations, 0 unrelated mutations |
| Private plan | `~/ewallet-evidence/vault/p4b2_private_p4b2-20261125T102630Z/p4b2.tfplan` (51 959 bytes, mode 600, directory 700) |
| Plan SHA-256 | `19c7f9d853d144384e437c15197eb2237c21af0d85a7ab89cb508464da68df77` |
| Plan JSON SHA-256 | `3b88b381dab543856782208b3f7294655fc882370dd65005955619c2b26c5b37` (`p4b2.plan.json`, 146 645 bytes, mode 600) |

| Address (`module.vault_kms.…[0]`) | Action | Planned identity | Scope |
|---|---|---|---|
| `google_project_service.cloudkms` | create | `cloudkms.googleapis.com`, `disable_on_destroy = false`, `deletion_policy = ABANDON` | project |
| `google_kms_key_ring.vault` | create | `ewallet-vault`, `asia-southeast1` | project |
| `google_kms_crypto_key.vault_unseal` | create | `vault-unseal`, ENCRYPT_DECRYPT, SOFTWARE, no rotation, `deletion_policy = PREVENT` | key ring |
| `google_service_account.vault_unseal` | create | `ewallet-vault-unseal@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com`, `deletion_policy = PREVENT` | project |
| `google_kms_crypto_key_iam_member.vault_encrypter_decrypter` | create | `roles/cloudkms.cryptoKeyEncrypterDecrypter` → the GSA | **that key only** |
| `google_kms_crypto_key_iam_member.vault_viewer` | create | `roles/cloudkms.viewer` → the GSA | **that key only** |
| `google_service_account_iam_member.vault_workload_identity` | create | `roles/iam.workloadIdentityUser` → `…svc.id.goog[ewallet-vault/vault]` | **that GSA only** |

No GKE cluster, node pool, authorized network, VPC, existing service account, existing IAM binding or existing
project service appears in the plan.

## Drift and what PASS does not mean

- **No drift entry** appears in the plan. A targeted plan refreshes only the targeted resources, and none exist
  yet. The documented drift was therefore **not observed and is not resolved**:
  - the state records node-pool autoscaling 0–3, while the live pool has autoscaling disabled (P4-B1);
  - there may be authorized-network and network differences.
- A full plan or a full apply would still propose unrelated changes. With `enable_gke` at its default `false`,
  it could even plan to destroy the cluster.
- Only this guard-approved targeted saved plan may be applied.
- PASS means: **this exact saved plan contains only the seven approved creates.** It does not mean the dev
  stack is synchronised, the KMS API is enabled, the key exists, Workload Identity works at runtime, or that
  any apply succeeded.

## Warnings carried from P4-B1

- **W1:** key ring and key absence rests on the complete audit history, accepted by you
  (`W1_ACCEPT_AUDIT_EVIDENCE_WITH_RESIDUAL_RISK`). The KMS API could not confirm it, and a plan cannot either.
  If one existed anyway, the apply would stop with HTTP 409 after the API and GSA had been created. Then
  follow the partial-failure rule: inspect and decide; never destroy, `state rm` or re-apply blindly.
- **W2** ([corrected in P4-B3](../p4b3_kms_provisioning_20261125/README.md#w2-corrected-inherited-project-roles-official-role-definitions)): the project-level `roles/owner` (one user) inherits use of the new key. `roles/editor` (the
  Compute Engine default SA) does not inherit direct use, but it inherits disable and delete rights over the
  key versions and the GSA, and indirect paths to key use (a JSON key for the GSA, or a pod as the Vault
  KSA). Key-scoped IAM cannot narrow this; it is a separate review.
- **W3:** Terraform drift, as above.
- **W4:** runtime Workload Identity is unverified until Campaign A2.

## Safety verification (after the plan)

- **State:** the object's generation and metageneration are unchanged (`1794062463403448/1`), and no lock is left
  (`state-integrity-check.json`).
- **Cloud:** the KMS API is still not enabled, the GSA still returns NOT_FOUND, the key ring query still answers
  SERVICE_DISABLED, and GKE has 0 node VMs with the same workload pool (`cloud-unchanged-check.json`).
- **Git:** no binary plan, plan JSON, state, tfvars or `.terraform/` is committed.
- **Local:** the initialised `.terraform/` of this branch's dev root (git-ignored) is kept for P4-B3.

## P4-B3 prerequisites (not executed)

1. Your explicit approval of plan SHA-256 `19c7f9d8…68df77`.
2. Recompute the SHA-256 of the private `p4b2.tfplan`; it must equal the approved value.
3. The state object's generation must still be `1794062463403448`. A saved plan from older state is stale and
   is refused by Terraform anyway; a new plan means a new guard run and a new review.
4. Apply only that file: `terraform apply <private>/p4b2.tfplan`, from this branch's initialised dev root.
   No `-target`, no re-plan, no auto-approve of anything else.
5. Afterwards:
   - `kms_preflight.py --mode cloud --vault-chart …` must report `VAULT_KMS_PREFLIGHT_READY`;
   - record the sanitized evidence;
   - stop before GKE (Campaign A2 is separate).
6. If the apply fails part-way, do not destroy, `state rm` or retry: inspect the state list and the preflight,
   then decide.

## Files

| File | Content |
|---|---|
| `plan-summary.json` | counts, inputs, the seven planned resources (names, roles, members only) |
| `plan-guard-result.json` | the guard's verdict and output |
| `plan-artifacts.json` | private location, sizes, modes, SHA-256 of the plan and its JSON |
| `terraform-backend-check.json` | root, init mode, backend, workspace, provider, concurrency |
| `state-integrity-check.json` | the state object before and after |
| `cloud-unchanged-check.json` | the read-only cloud re-check after the plan |
| `SHA256SUMS` | checksums |
