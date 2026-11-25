# Vault V2 P4-A: Cloud KMS Terraform, plan guard and preflight, offline validation (2026-11-25)

**OFFLINE ONLY.**

- No Google Cloud or GKE call was made, and no cloud resource exists.
- The real Terraform state and the GCS backend were not touched: no init with the backend, no refresh,
  no plan, no apply.
- `LIVE_TERRAFORM_PLAN_NOT_EXECUTED`.

Design and decisions: [audit §23](../../../../edai2/platform/VAULT_V2_ARCHITECTURE_AUDIT.md). P4-B procedure:
[VAULT_ESO_RUNBOOK.md step 0](../../../../runbooks/edai2/VAULT_ESO_RUNBOOK.md#0-cloud-kms-p4-b-separate-owner-approval-per-step-not-done).

| Check | Verdict | File |
|---|---|---|
| `terraform fmt -recursive -check` | `TERRAFORM_FORMAT_PASS` | `terraform-fmt.txt` |
| `validate_terraform_foundation.sh`: `init -backend=false` + `validate` (bootstrap and dev) and the 15 contract tests | `TERRAFORM_VALIDATE_PASS` | `terraform-validate.txt` |
| Real `terraform plan -target=module.vault_kms` on an **empty local state** in a scratch copy, the network black-holed (`scripts/vault/kms_offline_plan.sh`) | 7 to add, 0 to change, 0 to destroy; only `module.vault_kms` in the dependency closure | `offline-targeted-plan.txt` |
| Guard on that plan / on the full offline plan (every module, 61 creates) | `VAULT_KMS_PLAN_GUARD_PASS` / `VAULT_KMS_PLAN_GUARD_FAIL` (54 problems: every create outside the module) | `plan-guard-real-plans.txt` |
| Guard unit tests + R2 regression (`test_vault_kms_terraform.py`, `test_r2_terraform.py`) | 90 passed, including the opt-in offline plan run | `plan-guard-tests.txt` |
| KMS preflight against a fake `gcloud` (`test_vault_kms_preflight.py`) | 27 passed | `kms-preflight-tests.txt` |
| KMS preflight `--mode offline` with the pinned Vault chart | repository gates PASS (seal, annotation, rendered KSA `ewallet-vault/vault`); 8 cloud gates NOT_CHECKED; `VAULT_KMS_PREFLIGHT_OFFLINE_ONLY` | `kms-preflight-offline.jsonl` |
| Full regression (`tests/ops`, Terraform contract, `tests/inference/test_vault.py`) | 1012 passed, 2 skipped (one pre-existing; the opt-in offline plan without a provider mirror) | — |
| `git diff --check`, secret scan, no Terraform runtime file committed | PASS | `repository-checks.txt` |

Guard coverage (synthetic mutations of the real-format plan, `plan-guard-tests.txt`):

- **Mutations elsewhere in the plan:** GKE node-pool replace (both orders), cluster update, network update,
  GSA delete, extra resource in the module, project-wide IAM, `forget`, an unknown action, an import.
- **Approved addresses:** any action other than create; drift on a KMS address (it already exists).
- **Wrong values:** role, member, condition, key name, rotation, purpose, `deletion_policy`, HSM,
  location, project, GSA ID and email, `create_ignore_already_exists`, the Workload Identity KSA and role,
  the API service and `disable_on_destroy`; a key-scope ID for another key; an unknown scope not derived
  from the approved resource.
- **Unknowns:** an unknown critical attribute; any unexpected unknown.
- **Missing or extra:** each of the seven creates missing; an extra resource declared in the module.
- **Malformed plans:** malformed, empty, errored, not applyable, deferred, duplicate entries.
- **CLI:** exit codes; values of unrelated resources never printed.

Plan-guard test runs on synthetic plans, and the local-state plan, prove the guard and the module's
dependency closure. They are **not** proof of what the real dev state will plan; that is P4-B step 3.

`SHA256SUMS` holds the checksums of the files above.
