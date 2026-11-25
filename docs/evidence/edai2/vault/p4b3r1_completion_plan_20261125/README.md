# Vault V2 P4-B3R1: completion guard and one-resource saved plan (2026-11-25)

Campaign `p4b3r1-20261125T105200Z`. Verdict: **`VAULT_V2_P4B3R1_COMPLETION_PLAN_READY`**.
**This verdict does not authorize apply.** P4-B3R2 needs your separate approval of the exact plan below.

This campaign recovers from the partial P4-B3 apply
([`p4b3_kms_provisioning_20261125`](../p4b3_kms_provisioning_20261125/README.md)). That apply created six of
seven resources; the GSA's `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the key failed with a 403 while the
API enablement propagated. You chose option (a): a small, tested completion mode in the guard.

**What was done:**

- read-only verification of the partial state;
- a new explicit guard mode, with tests;
- one real targeted saved plan from the current state (plus one superseded attempt, see below).

**What was not done:**

- no apply and no IAM, KMS or state change;
- no import, destroy or state command;
- no GKE wake, no kubectl, no Vault deploy.

| Gate | Verdict |
|---|---|
| R1-A Partial state | `P4B3R1_PARTIAL_STATE_VERIFIED` (`pre-recovery-state.json`) |
| R1-B Completion guard | `P4B3R1_COMPLETION_GUARD_PASS` (`guard-tests.txt`) |
| R1-C Saved plan | `P4B3R1_SAVED_PLAN_PASS` (`plan-summary.json`, `guard-result.json`) |
| R1-D Integrity | `P4B3R1_INTEGRITY_PASS` (`plan-artifacts.json`, `state-integrity-check.json`, `cloud-safety-check.json`) |
| R1-E Evidence | `P4B3R1_EVIDENCE_PASS` (this directory) |

## R1-A: the partial state, re-verified read-only

The partial state was re-checked rather than taken from the P4-B3 report.

**Exists as approved:**

- the Cloud KMS API;
- key ring `ewallet-vault`;
- key `vault-unseal`: ENCRYPT_DECRYPT, SOFTWARE, no rotation, version 1 ENABLED;
- GSA `ewallet-vault-unseal`: enabled, 0 user-managed keys;
- the key's only binding, `roles/cloudkms.viewer` → the GSA;
- the GSA's only binding, `roles/iam.workloadIdentityUser` → `…svc.id.goog[ewallet-vault/vault]`.

**Absent:**

- the encrypterDecrypter grant;
- any key-ring binding;
- any project-level `roles/cloudkms.*`, and any project role for the GSA.

**State:**

- 61 addresses, identical to the P4-B3 post-apply list: the six above are present and the seventh is absent;
- generation `1795603007838257/1`, no lock;
- the preflight is BLOCKED on `kms-key-iam` only;
- GKE has 0 node VMs.

## R1-B: the completion mode

```bash
python3 -m ops.gcp.vault_kms_terraform guard --mode complete --plan <plan.json>
# -> VAULT_KMS_COMPLETION_PLAN_GUARD_PASS | VAULT_KMS_COMPLETION_PLAN_GUARD_FAIL
```

- **Explicit only.** The default `--mode create` is the unchanged P4-A seven-create guard. `guard()` and
  `_check_approved()` were not edited. The mode is never inferred: a completion plan given without
  `--mode complete` fails (it is not seven creates).
- **The one create** is `module.vault_kms.google_kms_crypto_key_iam_member.vault_encrypter_decrypter[0]`,
  action `["create"]`, with `before` null. It must pass the P4-A attribute check: role, member, no condition,
  type, provider, module. It also needs a stricter scope than P4-A: `crypto_key_id` must be **known** and equal
  to the existing key's full ID (P4-A accepted an unknown key ID derived from the key being created).
- **The six-resource baseline** comes from the plan's refreshed `prior_state`, not from missing changes:
  - The six must be present there and the seventh must not.
  - Each must have the approved type, provider and approved attributes, plus its concrete IDs (`id`, the
    key ring and key IDs, the GSA name, email and member, the IAM member IDs). The key's primary must be
    `cryptoKeyVersions/1` ENABLED.
  - `planned_values` must equal `prior_state` for each of the six, and must hold exactly the seven
    approved resources.
  - Any `resource_changes` entry for the six must be `no-op` with `before == after`. Terraform may also
    omit such entries; both forms are tested.
- **Fails closed** on any other address, action, import, drift inside the module, deferred change,
  errored or non-applyable plan, other provider, or missing or extra module configuration.

### A defect found by the real plan, fixed and regression-tested

The first real plan (from `68bf118b`) **failed** the new guard on two provider representations:

- the key's unset `rotation_period` is stored as `""`, while the P4-A expectation is `null`;
- a refresh drift entry on the key, whose sole difference is `labels: null → {}`. `effective_labels`,
  `terraform_labels` and every other attribute are identical, and gcloud shows no rotation and no label
  change.

Commit `4860f0f0` accepts exactly these two forms and nothing else:

- `""` or `null` for `rotation_period`;
- a drift `update` on one of the six whose only differing key is `labels`, with both sides in `{null, {}}`.

The tolerated refresh is reported (`normalised_drift_labels_null_to_empty`). Labels that are set, a drift
on any other attribute or resource, a removed attribute, `"0s"` or any period still fail (tests). The plan
was then regenerated from `4860f0f0`. The first plan is superseded and kept privately in `attempt1/`.
**Never apply it.**

## Tests

| Command | Result |
|---|---|
| `pytest tests/ops/test_vault_kms_terraform.py tests/ops/test_r2_terraform.py tests/ops/test_vault_kms_preflight.py`, with the opt-in real offline plan | **190 passed** (72 completion-mode cases) |
| `pytest tests/ops tests/inference/test_vault.py` (at `4860f0f0`) | 1070 passed, 2 skipped |
| `terraform fmt -recursive -check`; `init -backend=false` + `validate` (bootstrap, dev) on a scratch copy; `tests.contract.test_terraform_foundation` | PASS; valid; 15 OK |
| `git diff --check`, secret scan | PASS |

The Terraform module, environments and dependency graph are unchanged: no `time_sleep` and no new provider.

| Required case | Test |
|---|---|
| one approved create, six unchanged | PASS (with and without no-op entries; the real-plan shape) |
| original mode: 7 creates PASS; 1-create recovery plan FAIL | yes |
| completion mode rejects the 7-create plan | yes |
| missing key ring, key, GSA, viewer, WI, API in the baseline | FAIL each |
| wrong GSA, disabled GSA, key rotation, purpose, disabled version, other ring, viewer role or key, WI KSA | FAIL each |
| existing key changed (update, planned value) or any mutation of an existing resource | FAIL |
| extra key grant, project-wide grant, two creates, zero creates, grant already in state | FAIL |
| wrong principal, `allUsers`, admin or decrypter-only role, other key, condition, unknown role, member or scope | FAIL |
| node-pool replacement, unrelated delete, cluster update, import | FAIL |
| drift inside the module, other provider, wrong `planned_values`, 12 malformed or untrustworthy shapes | FAIL |

## R1-C/R1-D: the real saved plan

| Field | Value |
|---|---|
| Command | `terraform plan -input=false -lock-timeout=0s -target=module.vault_kms -var enable_vault_kms=true -var-file=<private tfvars: enable_gke = true> -out=…` (normal refresh, normal locking) |
| Git | `4860f0f0`, clean |
| Terraform / provider | 1.16.6 / hashicorp/google 7.46.1 |
| Backend / workspace | GCS `ewallet-data-ai-platform-hg26-tfstate`, `terraform/dev` / `default` |
| Terraform said | `Plan: 1 to add, 0 to change, 0 to destroy.` |
| Creates / updates / deletes / replacements / imports / deferred | **1 / 0 / 0 / 0 / 0 / 0**; 6 no-op; 0 outside `module.vault_kms` |
| The create | `roles/cloudkms.cryptoKeyEncrypterDecrypter` → `serviceAccount:ewallet-vault-unseal@…`, on `…/keyRings/ewallet-vault/cryptoKeys/vault-unseal` (known), no condition |
| Guard (`--mode complete`) | **`VAULT_KMS_COMPLETION_PLAN_GUARD_PASS`**, 1 create, 6 existing unchanged, 1 normalised label refresh, 0 unrelated mutations |
| Guard (default mode) | `VAULT_KMS_PLAN_GUARD_FAIL`, as designed |
| Private plan | `~/ewallet-evidence/vault/p4b3r1_private_20261125T105200Z/p4b3r1.tfplan`, 54 069 bytes, mode 600, directory 700 |
| **Plan SHA-256** | **`d87d37d4b38fd51a8e3dcd328986975fa00f72b6e7d2fd79cfe9a5de9154cc72`** |
| Plan JSON SHA-256 | `72e65908568b5f003f711471aec2dde3ad9d7fd15ae9332263d8555b075d0690` (164 398 bytes) |
| State generation | `1795603007838257/1` before and after both plans; no lock left |

**After the plan:**

- the key still has only the viewer binding;
- the preflight is still BLOCKED on `kms-key-iam` only;
- GKE has 0 node VMs and an unchanged pool.

When applied, the plan also writes the refreshed key record (`labels = {}`) to the state. That is not a
cloud change.

## P4-B3R2 (not executed): what you approve

1. The exact plan SHA-256 `d87d37d4…154cc72`, the single create above, and its guard PASS.
2. Before apply:
   - recompute the plan's SHA-256;
   - the state generation must still be `1795603007838257` (otherwise the plan is stale: re-plan,
     re-guard, re-review);
   - re-run `--mode complete` on a fresh `terraform show -json`, which must be byte-identical to the
     reviewed JSON;
   - the key must still lack the grant.
3. Apply only that file (`terraform apply <private>/p4b3r1.tfplan`), once.
4. Afterwards, `kms_preflight.py --mode cloud` must report `VAULT_KMS_PREFLIGHT_READY`.
5. Stop before GKE Campaign A2.

If the apply fails, the P4-B3 rule applies: stop, inspect, decide. Never re-apply the P4-B2 plan or the
`attempt1/` plan.

## Files

| File | Content |
|---|---|
| `pre-recovery-state.json` | R1-A checks |
| `guard-tests.txt` | test names and results |
| `plan-summary.json` | counts, the create, the drift entry, inputs (no raw plan) |
| `guard-result.json` | the completion guard's output on the real plan |
| `plan-artifacts.json` | private location, sizes, modes, SHA-256, commit, versions, superseded attempt |
| `state-integrity-check.json` | state object before and after |
| `cloud-safety-check.json`, `kms-preflight-cloud-after-plan.jsonl` | the read-only re-check after planning |
| `SHA256SUMS` | checksums |
