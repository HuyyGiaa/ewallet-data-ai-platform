# Vault V2 P4-B1: read-only Google Cloud discovery and Terraform state inspection (2026-11-25)

**READ-ONLY.**

- No Google Cloud, GKE or Terraform-state change was made: no plan, apply, import, refresh or enable.
- No kubectl was run and GKE was not woken.
- No credential was read.

Verdict: **`VAULT_V2_P4B1_READY_WITH_WARNINGS`**.

| Gate | Verdict |
|---|---|
| B1-A Cloud discovery | **PASS with a recorded unknown.** API and GSA determined authoritatively. Key ring and key are not API-determinable while the KMS API is disabled; they are absent by audit history |
| B1-B Terraform state | `P4B1_TERRAFORM_STATE_PASS` |
| B1-C Workload Identity metadata | `P4B1_WORKLOAD_IDENTITY_METADATA_PASS` (configuration only; runtime token exchange is Campaign A2) |
| B1-D Read-only safety | `P4B1_READ_ONLY_SAFETY_PASS` |

## Context

| | |
|---|---|
| Repository | branch `experiment/edai2-vault-clustersecretstore`, HEAD `100bb4e9` (clean) |
| Tools | Terraform 1.16.6, Google Cloud SDK 590.0.0 |
| Identity | one active user account with read access (not recorded); every query passed `--project` explicitly; the gcloud configuration was not changed |
| Project | `ewallet-data-ai-platform-hg26` (number 198818809719), ACTIVE, created 2026-10-21, **no organization or folder** |
| Terraform backend | GCS `ewallet-data-ai-platform-hg26-tfstate`, prefix `terraform/dev`, workspace `default`, from the already-initialised directory; one state object, no lock file |
| GKE | `ewallet-dev` control plane RUNNING, **0 node VMs (asleep)**, management API only |

## Resource inventory

| Resource | Expected | Observed | Status |
|---|---|---|---|
| Cloud KMS API | enabled by `module.vault_kms` | not enabled; never enabled (0 Service Usage audit entries; the positive control on `container.googleapis.com` found 3) | `API_DISABLED` (authoritative) |
| KeyRing `ewallet-vault` (`asia-southeast1`) | created by P4-B | describe and list → HTTP 403 SERVICE_DISABLED: the API cannot answer. 0 Cloud KMS Admin Activity entries ever (400-day retention covers the whole 35-day project life) | `KEYRING_UNKNOWN_OR_BLOCKED` by API; **absent by audit history** |
| CryptoKey `vault-unseal` | created by P4-B | not queryable (parent unreadable); same audit history | `UNKNOWN_VIA_PARENT`; absent by audit history |
| GSA `ewallet-vault-unseal@…` | created by P4-B | IAM NOT_FOUND; none of the 11 service accounts matches; 0 IAM audit entries for the name | `CONFIRMED_ABSENT` |
| Key IAM (encrypterDecrypter, viewer) | created by P4-B | the key cannot be read; the member GSA does not exist; no key IAM ever set (audit) | cannot exist yet |
| Workload Identity member | created by P4-B | its GSA does not exist | `CONFIRMED_ABSENT` |
| Inherited IAM | — | project level only (no organization): `roles/owner` 1 user, `roles/editor` 1 service account (the Compute Engine default SA), no `roles/cloudkms.*` | **warning:** owner inherits key use; editor inherits key and GSA administration rights but not key use ([corrected in P4-B3](../p4b3_kms_provisioning_20261125/README.md#w2-corrected-inherited-project-roles-official-role-definitions)) |
| GKE workload pool | `ewallet-data-ai-platform-hg26.svc.id.goog` | `ewallet-data-ai-platform-hg26.svc.id.goog`; node pool `cpu-pool` `GKE_METADATA` | PASS |

## Terraform ownership

- `terraform state list`:
  - 55 addresses;
  - **0** under `module.vault_kms`;
  - **0** `google_kms_*` resources anywhere;
  - `cloudkms.googleapis.com` is not among the 13 managed project services;
  - the 8 `module.iam` GSAs and the 6 service-account IAM members concern other identities.
- The state object's generation was identical before and after (`1794062463403448`).
- The bootstrap stack (local state) manages only the state bucket.
- There is no other state in the bucket.
- **No competing address exists.** All seven planned creates are new to every Terraform state found.

| Resource | Address (`module.vault_kms.…[0]`) | Conclusion | Future create |
|---|---|---|---|
| Cloud KMS API | `google_project_service.cloudkms` | `NOT_IN_STATE_AND_CONFIRMED_ABSENT` | clearly eligible: a genuine enable, not the adoption of an independently enabled API |
| KeyRing | `google_kms_key_ring.vault` | `UNKNOWN_OR_BLOCKED` (absent by audit history) | eligible with warning W1 |
| CryptoKey | `google_kms_crypto_key.vault_unseal` | `UNKNOWN_OR_BLOCKED` (absent by audit history) | eligible with warning W1 |
| GSA | `google_service_account.vault_unseal` | `NOT_IN_STATE_AND_CONFIRMED_ABSENT` | clearly eligible |
| Key IAM ×2 | `google_kms_crypto_key_iam_member.vault_*` | `NOT_IN_STATE_AND_CONFIRMED_ABSENT` (member does not exist) | eligible |
| Workload Identity | `google_service_account_iam_member.vault_workload_identity` | `NOT_IN_STATE_AND_CONFIRMED_ABSENT` | clearly eligible |

## Warnings (no confirmed conflict)

- **W1:** key ring and key absence is proven by the complete audit history, not by a KMS not-found,
  because the API is disabled.
  - A real plan cannot check it either: Terraform plans a create without reading the API.
  - If, against the evidence, either one existed, the apply would stop with HTTP 409 after the API and GSA
    were created. The runbook's partial-failure rule applies: inspect, then decide; never destroy, never
    `state rm`.
  - Owner decision for P4-B2: accept the audit evidence, or approve enabling the API alone first. That
    would make absence API-confirmable, but it would also mean the plan's `google_project_service` create
    adopts an API that is already enabled.
- **W2** ([corrected in P4-B3](../p4b3_kms_provisioning_20261125/README.md#w2-corrected-inherited-project-roles-official-role-definitions); the original wording was wrong for editor):
  - `roles/owner` (one user) can use and administer the key project-wide.
  - `roles/editor` (the Compute Engine default SA) cannot encrypt, decrypt or change the key's IAM. It can
    disable key versions and disable or delete the GSA. Once the GSA holds encrypterDecrypter, it can also
    reach key use indirectly: by creating a JSON key for the GSA, or by running a pod as the Vault KSA.
  - Key-scoped IAM cannot narrow either role. Removing the default editor grant is a separate project
    decision. `discovery.jsonl` keeps the original wording as recorded.
- **W3:** the documented drift is still present, and was confirmed live where visible without reading
  state: `cpu-pool` autoscaling is **disabled**, while the state records 0–3. A full plan would replace or
  change the pool. Only a guard-approved `-target=module.vault_kms` saved plan may be applied (P4-B2).
- **W4:** runtime Workload Identity (the token exchange from `ewallet-vault/vault`) is unverified until
  Campaign A2.

## Files

| File | Content |
|---|---|
| `discovery.jsonl` | every check: query, outcome, classification (no account names, no stderr, no raw policies) |
| `terraform-state-summary.json` | backend, workspace, generation before and after, address counts by type, relevant addresses |
| `ownership-matrix.json` | the seven planned resources: state status, cloud status, conclusion |
| `SHA256SUMS` | checksums |
