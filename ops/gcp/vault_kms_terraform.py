"""Plan guard for the Vault V2 Cloud KMS change (P4) on the dev stack.

The dev state has drifted from the live GKE cluster (see ops/gcp/r2_terraform.py): a plain
apply replaces the node pool. ``module.vault_kms`` therefore has no dependency on the GKE or
Workload Identity modules, and P4-B applies only a saved plan that this guard has passed:

    terraform plan -target=module.vault_kms -var enable_vault_kms=true -out=p4.tfplan
    terraform show -json p4.tfplan > p4.plan.json
    python3 -m ops.gcp.vault_kms_terraform guard --plan p4.plan.json

PASS means exactly the seven approved creates, each with its expected attributes, and no other
mutation anywhere in the plan. It never means the rest of the dev state is free of drift. Fail
closed: a missing or malformed plan, an unknown action, an import, a deferred change or an
unexpected value is a FAIL. Only resource addresses, actions and the expected non-secret
attribute names and values are printed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

PROJECT = "ewallet-data-ai-platform-hg26"
LOCATION = "asia-southeast1"
KEY_RING = "ewallet-vault"
CRYPTO_KEY = "vault-unseal"
GSA_EMAIL = f"ewallet-vault-unseal@{PROJECT}.iam.gserviceaccount.com"
GSA_MEMBER = f"serviceAccount:{GSA_EMAIL}"
WI_MEMBER = f"serviceAccount:{PROJECT}.svc.id.goog[ewallet-vault/vault]"
KEY_RING_ID = f"projects/{PROJECT}/locations/{LOCATION}/keyRings/{KEY_RING}"
CRYPTO_KEY_ID = f"{KEY_RING_ID}/cryptoKeys/{CRYPTO_KEY}"
GSA_ID = f"projects/{PROJECT}/serviceAccounts/{GSA_EMAIL}"
MODULE = "module.vault_kms"
MODULE_SOURCE = "../../modules/vault_kms"
PROVIDER = "registry.terraform.io/hashicorp/google"
KNOWN_ACTIONS = {"create", "read", "update", "delete", "no-op", "forget"}

# address -> type, exact attributes, and the attributes that may be unknown at plan time only when
# the configuration derives them from the named approved resource (the expected value once known).
APPROVED: dict[str, dict[str, Any]] = {
    f"{MODULE}.google_project_service.cloudkms[0]": {
        "type": "google_project_service",
        "exact": {"project": PROJECT, "service": "cloudkms.googleapis.com", "disable_on_destroy": False,
                  "disable_dependent_services": False, "deletion_policy": "ABANDON"},
        "derived": {},
    },
    f"{MODULE}.google_kms_key_ring.vault[0]": {
        "type": "google_kms_key_ring",
        "exact": {"project": PROJECT, "name": KEY_RING, "location": LOCATION},
        "derived": {},
    },
    f"{MODULE}.google_kms_crypto_key.vault_unseal[0]": {
        "type": "google_kms_crypto_key",
        "exact": {"name": CRYPTO_KEY, "purpose": "ENCRYPT_DECRYPT", "rotation_period": None,
                  "destroy_scheduled_duration": "2592000s", "deletion_policy": "PREVENT",
                  "version_template": [{"algorithm": "GOOGLE_SYMMETRIC_ENCRYPTION", "protection_level": "SOFTWARE"}]},
        "derived": {"key_ring": ("google_kms_key_ring.vault[0].id", KEY_RING_ID)},
        "false_or_unset": ("skip_initial_version_creation", "import_only"),
    },
    f"{MODULE}.google_service_account.vault_unseal[0]": {
        "type": "google_service_account",
        "exact": {"project": PROJECT, "account_id": "ewallet-vault-unseal", "email": GSA_EMAIL,
                  "disabled": False, "deletion_policy": "PREVENT"},
        "derived": {},
        "false_or_unset": ("create_ignore_already_exists",),
    },
    f"{MODULE}.google_kms_crypto_key_iam_member.vault_encrypter_decrypter[0]": {
        "type": "google_kms_crypto_key_iam_member",
        "exact": {"role": "roles/cloudkms.cryptoKeyEncrypterDecrypter", "member": GSA_MEMBER, "condition": []},
        "derived": {"crypto_key_id": ("google_kms_crypto_key.vault_unseal[0].id", CRYPTO_KEY_ID)},
    },
    f"{MODULE}.google_kms_crypto_key_iam_member.vault_viewer[0]": {
        "type": "google_kms_crypto_key_iam_member",
        "exact": {"role": "roles/cloudkms.viewer", "member": GSA_MEMBER, "condition": []},
        "derived": {"crypto_key_id": ("google_kms_crypto_key.vault_unseal[0].id", CRYPTO_KEY_ID)},
    },
    f"{MODULE}.google_service_account_iam_member.vault_workload_identity[0]": {
        "type": "google_service_account_iam_member",
        "exact": {"role": "roles/iam.workloadIdentityUser", "member": WI_MEMBER, "condition": []},
        "derived": {"service_account_id": ("google_service_account.vault_unseal[0].name", GSA_ID)},
    },
}
# Provider-computed attributes that may stay unknown: identifiers, etags and labels, never a scope.
COMPUTED_OK = {"id", "etag", "name", "unique_id", "primary", "crypto_key_backend", "import_only",
               "effective_labels", "terraform_labels", "version_template", "member", "email"}


def _module_config(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Module-relative address -> resource configuration of module.vault_kms."""
    call = plan.get("configuration", {}).get("root_module", {}).get("module_calls", {}).get("vault_kms")
    if not isinstance(call, dict) or call.get("source") != MODULE_SOURCE:
        raise ValueError(f"configuration has no module call vault_kms with source {MODULE_SOURCE}")
    return {r["address"]: r for r in call.get("module", {}).get("resources", [])}


def _check_approved(address: str, change: dict[str, Any], config: dict[str, Any]) -> list[str]:
    spec, problems = APPROVED[address], []
    if change.get("type") != spec["type"] or change.get("mode") != "managed":
        return [f"{address}: type {change.get('type')}/{change.get('mode')}, expected managed {spec['type']}"]
    if change.get("provider_name") != PROVIDER or change.get("module_address") != MODULE:
        problems.append(f"{address}: provider or module differs")
    body = change["change"]
    after, unknown = body.get("after"), body.get("after_unknown")
    if not isinstance(after, dict) or not isinstance(unknown, dict):
        return problems + [f"{address}: no planned attributes"]
    for key, want in spec["exact"].items():
        if unknown.get(key) is True:
            problems.append(f"{address}: {key} is unknown at plan time, expected {want!r}")
        elif after.get(key) != want:
            problems.append(f"{address}: {key} = {after.get(key)!r}, expected {want!r}")
    for key in spec.get("false_or_unset", ()):
        if after.get(key) not in (None, False) and unknown.get(key) is not True:
            problems.append(f"{address}: {key} = {after.get(key)!r}, expected false or unset")
    relative = address.removeprefix(f"{MODULE}.").split("[")[0]
    for key, (reference, value) in spec["derived"].items():
        if unknown.get(key) is True:
            refs = config.get(relative, {}).get("expressions", {}).get(key, {}).get("references", [])
            if reference not in refs:
                problems.append(f"{address}: {key} is unknown and not derived from {reference}")
        elif after.get(key) != value:
            problems.append(f"{address}: {key} = {after.get(key)!r}, expected {value!r}")
    for key, flag in unknown.items():
        if flag is True and key not in COMPUTED_OK and key not in spec["derived"]:
            problems.append(f"{address}: {key} is unknown at plan time")
    return problems


def guard(plan: Any) -> tuple[list[str], dict[str, Any]]:
    """Return (problems, summary); an empty problem list means exactly the approved P4 change."""

    if not isinstance(plan, dict) or not str(plan.get("format_version", "")).startswith("1."):
        return ["not a terraform show -json plan (format_version 1.x)"], {}
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        return ["plan has no resource_changes list"], {}
    problems: list[str] = []
    if plan.get("errored") is not False:
        problems.append("the plan errored or does not say it did not")
    if plan.get("applyable") is not True:
        problems.append("the plan is not applyable")
    if plan.get("deferred_changes"):
        problems.append("the plan defers changes")
    try:
        config = _module_config(plan)
    except (ValueError, AttributeError, KeyError, TypeError) as error:
        return problems + [str(error)], {}
    expected_config = {a.removeprefix(f"{MODULE}.").split("[")[0] for a in APPROVED}
    if set(config) != expected_config:
        problems.append(f"module.vault_kms declares {sorted(set(config) ^ expected_config)} beyond or short of the approved resources")
    seen, created, ignored = set(), [], 0
    for change in changes:
        try:
            address, actions = change["address"], change["change"]["actions"]
        except (KeyError, TypeError):
            problems.append("a resource change without address or actions")
            continue
        if not isinstance(actions, list) or not actions or not all(isinstance(a, str) for a in actions):
            problems.append(f"{address}: malformed action list")
            continue
        if address in seen:
            problems.append(f"{address}: appears twice")
            continue
        seen.add(address)
        if set(actions) - KNOWN_ACTIONS:
            problems.append(f"{'+'.join(actions)} {address}: unknown action")
            continue
        if change["change"].get("importing"):
            problems.append(f"import {address}: imports are not part of the approved change")
            continue
        if address in APPROVED:
            if actions != ["create"]:
                problems.append(f"{'+'.join(actions)} {address}: only create is approved")
                continue
            found = _check_approved(address, change, config)
            problems.extend(found)
            if not found:
                created.append(address)
            continue
        if actions == ["no-op"] or (actions == ["read"] and change.get("mode") == "data"):
            ignored += 1
            continue
        problems.append(f"{'+'.join(actions)} {address}: outside the approved P4 change")
    for missing in sorted(set(APPROVED) - seen):
        problems.append(f"{missing}: missing from the plan")
    drift = plan.get("resource_drift") or []
    if not isinstance(drift, list):
        problems.append("resource_drift is not a list")
        drift = []
    drifted = sorted(str(d.get("address")) for d in drift if isinstance(d, dict))
    for address in drifted:
        if address.startswith(f"{MODULE}."):
            problems.append(f"{address}: drifted outside Terraform (it already exists): stop for an ownership decision")
    summary = {"approved_creates": sorted(created), "unchanged_or_read": ignored,
               "drift_outside_the_change": [a for a in drifted if not a.startswith(f"{MODULE}.")],
               "targeted_or_incomplete": plan.get("complete") is not True}
    return problems, summary


def main(argv: list[str] | None = None) -> int:
    """Run ``guard``; exit 0 only for VAULT_KMS_PLAN_GUARD_PASS."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("guard")
    check.add_argument("--plan", type=Path, required=True, help="terraform show -json output of the saved plan")
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"VAULT_KMS_PLAN_GUARD_FAIL: cannot read the plan ({type(error).__name__})", file=sys.stderr)
        return 1
    problems, summary = guard(plan)
    if problems:
        for problem in problems:
            print(f"STOP: {problem}", file=sys.stderr)
        print(f"VAULT_KMS_PLAN_GUARD_FAIL: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(json.dumps({"verdict": "VAULT_KMS_PLAN_GUARD_PASS", "approved_creates": len(summary["approved_creates"]),
                      "creates": summary["approved_creates"], "unrelated_mutations": 0, **{
                          k: summary[k] for k in ("unchanged_or_read", "drift_outside_the_change", "targeted_or_incomplete")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
