"""Vault V2 P4: the Cloud KMS Terraform module, its plan guard, and their agreement with the Vault values.

The base fixture tests/fixtures/vault_kms/targeted.plan.json is a REAL `terraform show -json` of
`terraform plan -target=module.vault_kms` (Terraform 1.16, google 7.46.1), made offline by
scripts/vault/kms_offline_plan.sh against an EMPTY LOCAL state, with the network black-holed and the
placeholder token removed. Every negative case mutates that plan. None of this shows what the real dev
state would plan (P4-B).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from ops.gcp import vault_kms_terraform as kms
from ops.gcp.r2_terraform import APPROVED_CREATES as R2_APPROVED

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "infra/terraform/gcp"
MODULE = (TF / "modules/vault_kms/main.tf").read_text()
MODULE_CODE = "\n".join(line.split("#")[0] for line in MODULE.splitlines())      # without comments
DEV_MAIN = (TF / "environments/dev/main.tf").read_text()
BASE = json.loads((ROOT / "tests/fixtures/vault_kms/targeted.plan.json").read_text())
A = {name: f"module.vault_kms.{name}[0]" for name in (
    "google_project_service.cloudkms", "google_kms_key_ring.vault", "google_kms_crypto_key.vault_unseal",
    "google_service_account.vault_unseal", "google_kms_crypto_key_iam_member.vault_encrypter_decrypter",
    "google_kms_crypto_key_iam_member.vault_viewer", "google_service_account_iam_member.vault_workload_identity")}


def plan():
    return copy.deepcopy(BASE)


def entry(p, short):
    return next(c for c in p["resource_changes"] if c["address"] == A[short])


def foreign(address, actions, *, mode="managed", type_="google_container_node_pool"):
    return {"address": address, "mode": mode, "type": type_, "provider_name": kms.PROVIDER,
            "change": {"actions": actions, "before": {}, "after": {}, "after_unknown": {}}}


def problems(p):
    return kms.guard(p)[0]


# ---- the approved change

def test_the_real_offline_targeted_plan_is_exactly_the_approved_change():
    found, summary = kms.guard(plan())
    assert found == [] and sorted(summary["approved_creates"]) == sorted(kms.APPROVED) == sorted(A.values())
    assert summary["targeted_or_incomplete"] is True       # a targeted plan is never "complete"; that is not a pass condition


def test_no_op_and_data_reads_elsewhere_do_not_hide_anything():
    p = plan()
    p["resource_changes"] += [foreign("module.gke.google_container_node_pool.cpu[0]", ["no-op"]),
                              foreign("module.jenkins_vm.data.google_compute_image.ubuntu", ["read"], mode="data",
                                      type_="google_compute_image")]
    found, summary = kms.guard(p)
    assert found == [] and summary["unchanged_or_read"] == 2


# ---- unrelated mutations: always FAIL, even with every expected create present

@pytest.mark.parametrize("extra,needle", [
    (foreign("module.gke.google_container_node_pool.cpu[0]", ["delete", "create"]), "delete+create module.gke.google_container_node_pool.cpu[0]"),
    (foreign("module.gke.google_container_node_pool.cpu[0]", ["create", "delete"]), "create+delete module.gke"),
    (foreign("module.gke.google_container_cluster.platform[0]", ["update"], type_="google_container_cluster"), "update module.gke.google_container_cluster"),
    (foreign("module.network.google_compute_network.vpc", ["update"], type_="google_compute_network"), "update module.network"),
    (foreign('module.iam.google_service_account.workload["ci"]', ["delete"], type_="google_service_account"), "delete module.iam"),
    (foreign("module.vault_kms.google_kms_crypto_key_iam_binding.everyone[0]", ["create"], type_="google_kms_crypto_key_iam_binding"), "outside the approved P4 change"),
    (foreign("google_project_iam_member.vault_kms_admin", ["create"], type_="google_project_iam_member"), "outside the approved P4 change"),
    (foreign("module.iam.google_project_iam_member.ci", ["forget"], type_="google_project_iam_member"), "forget module.iam"),
    (foreign("module.gke.google_container_node_pool.cpu[0]", ["replace"]), "unknown action"),
], ids=["node-pool-replace", "node-pool-create-before-destroy", "cluster-update", "network-update", "gsa-delete",
        "extra-resource-in-module", "project-wide-iam", "forget", "unsupported-action"])
def test_any_other_mutation_fails_the_whole_plan(extra, needle):
    p = plan()
    p["resource_changes"].append(extra)
    found = problems(p)
    assert any(needle in f for f in found), found


@pytest.mark.parametrize("actions", [["delete"], ["update"], ["delete", "create"], ["create", "delete"], ["no-op"], ["read"]])
def test_an_approved_address_may_only_be_created(actions):
    p = plan()
    entry(p, "google_kms_crypto_key.vault_unseal")["change"]["actions"] = actions
    assert any("only create is approved" in f for f in problems(p))


def test_an_import_is_refused_even_as_no_op():
    p = plan()
    p["resource_changes"].append(foreign("module.gke.google_container_node_pool.cpu[0]", ["no-op"]))
    p["resource_changes"][-1]["change"]["importing"] = {"id": "projects/x/locations/y/clusters/z/nodePools/cpu"}
    assert any("imports are not part" in f for f in problems(p))


def test_a_drifted_kms_resource_means_it_already_exists_and_stops():
    p = plan()
    p["resource_drift"] = [{"address": A["google_kms_key_ring.vault"]}, {"address": "module.gke.google_container_node_pool.cpu[0]"}]
    found, summary = kms.guard(p)
    assert any("ownership decision" in f for f in found)
    assert summary["drift_outside_the_change"] == ["module.gke.google_container_node_pool.cpu[0]"]


# ---- attribute checks on the approved creates

@pytest.mark.parametrize("short,key,value,needle", [
    ("google_kms_crypto_key_iam_member.vault_encrypter_decrypter", "role", "roles/cloudkms.admin", "role"),
    ("google_kms_crypto_key_iam_member.vault_viewer", "role", "roles/cloudkms.cryptoKeyDecrypter", "role"),
    ("google_kms_crypto_key_iam_member.vault_encrypter_decrypter", "member", "allUsers", "member"),
    ("google_kms_crypto_key_iam_member.vault_viewer", "condition", [{"expression": "true", "title": "x"}], "condition"),
    ("google_kms_crypto_key.vault_unseal", "name", "vault-unseal-2", "name"),
    ("google_kms_crypto_key.vault_unseal", "rotation_period", "7776000s", "rotation_period"),
    ("google_kms_crypto_key.vault_unseal", "purpose", "ASYMMETRIC_DECRYPT", "purpose"),
    ("google_kms_crypto_key.vault_unseal", "deletion_policy", "DELETE", "deletion_policy"),
    ("google_kms_crypto_key.vault_unseal", "version_template", [{"algorithm": "GOOGLE_SYMMETRIC_ENCRYPTION", "protection_level": "HSM"}], "version_template"),
    ("google_kms_crypto_key.vault_unseal", "skip_initial_version_creation", True, "skip_initial_version_creation"),
    ("google_kms_key_ring.vault", "location", "us-central1", "location"),
    ("google_kms_key_ring.vault", "project", "another-project", "project"),
    ("google_service_account.vault_unseal", "account_id", "ewallet-vault", "account_id"),
    ("google_service_account.vault_unseal", "email", "someone@another-project.iam.gserviceaccount.com", "email"),
    ("google_service_account.vault_unseal", "create_ignore_already_exists", True, "create_ignore_already_exists"),
    ("google_service_account_iam_member.vault_workload_identity", "member",
     "serviceAccount:ewallet-data-ai-platform-hg26.svc.id.goog[ewallet-vault/default]", "member"),
    ("google_service_account_iam_member.vault_workload_identity", "role", "roles/iam.serviceAccountTokenCreator", "role"),
    ("google_project_service.cloudkms", "disable_on_destroy", True, "disable_on_destroy"),
    ("google_project_service.cloudkms", "service", "container.googleapis.com", "service"),
], ids=lambda v: v if isinstance(v, str) and "." not in v and "/" not in v else None)
def test_a_wrong_value_on_an_approved_create_fails(short, key, value, needle):
    p = plan()
    entry(p, short)["change"]["after"][key] = value
    found = problems(p)
    assert any(A[short] in f and needle in f for f in found), found


def test_iam_scoped_to_another_key_or_the_project_fails():
    p = plan()
    member = entry(p, "google_kms_crypto_key_iam_member.vault_encrypter_decrypter")
    member["change"]["after_unknown"]["crypto_key_id"] = False
    member["change"]["after"]["crypto_key_id"] = f"projects/{kms.PROJECT}/locations/{kms.LOCATION}/keyRings/other/cryptoKeys/other"
    assert any("crypto_key_id" in f for f in problems(p))


def test_an_unknown_scope_must_come_from_the_approved_resource():
    p = plan()
    config = next(r for r in p["configuration"]["root_module"]["module_calls"]["vault_kms"]["module"]["resources"]
                  if r["address"] == "google_kms_crypto_key_iam_member.vault_viewer")
    config["expressions"]["crypto_key_id"] = {"references": ["var.some_other_key"]}
    assert any("not derived from google_kms_crypto_key.vault_unseal[0].id" in f for f in problems(p))


@pytest.mark.parametrize("short,key", [
    ("google_kms_crypto_key_iam_member.vault_encrypter_decrypter", "role"),
    ("google_kms_crypto_key_iam_member.vault_viewer", "member"),
    ("google_service_account_iam_member.vault_workload_identity", "member"),
    ("google_kms_key_ring.vault", "location"),
    ("google_kms_crypto_key.vault_unseal", "purpose"),
])
def test_an_unknown_critical_attribute_fails(short, key):
    p = plan()
    change = entry(p, short)["change"]
    change["after"].pop(key, None)
    change["after_unknown"][key] = True
    assert any(f"{key} is unknown" in f for f in problems(p))


def test_an_unexpected_unknown_attribute_fails():
    p = plan()
    entry(p, "google_kms_key_ring.vault")["change"]["after_unknown"]["mystery"] = True
    assert any("mystery is unknown" in f for f in problems(p))


@pytest.mark.parametrize("short", list(A))
def test_every_approved_create_is_required(short):
    p = plan()
    p["resource_changes"] = [c for c in p["resource_changes"] if c["address"] != A[short]]
    assert any(f"{A[short]}: missing" in f for f in problems(p))


def test_an_extra_resource_declared_in_the_module_fails():
    p = plan()
    p["configuration"]["root_module"]["module_calls"]["vault_kms"]["module"]["resources"].append(
        {"address": "google_kms_crypto_key.spare", "mode": "managed", "type": "google_kms_crypto_key", "expressions": {}})
    assert any("beyond or short of the approved resources" in f for f in problems(p))


# ---- malformed plans: fail closed

@pytest.mark.parametrize("bad,needle", [
    ({}, "format_version"),
    ([], "format_version"),
    ({"format_version": "1.2"}, "no resource_changes"),
    ({"format_version": "1.2", "resource_changes": "x"}, "no resource_changes"),
    ({"format_version": "0.1", "resource_changes": []}, "format_version"),
], ids=["empty-object", "list", "no-changes", "changes-not-list", "old-format"])
def test_malformed_plans_fail(bad, needle):
    assert any(needle in f for f in problems(bad))


def test_an_empty_plan_fails():
    p = plan()
    p["resource_changes"] = []
    assert len([f for f in problems(p) if "missing from the plan" in f]) == len(A)


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p.update(errored=True), "errored"),
    (lambda p: p.update(applyable=False), "not applyable"),
    (lambda p: p.update(deferred_changes=[{"resource_change": {}}]), "defers"),
    (lambda p: p["resource_changes"].append(copy.deepcopy(p["resource_changes"][0])), "appears twice"),
    (lambda p: p["resource_changes"].append({"address": "x"}), "without address or actions"),
    (lambda p: p["resource_changes"][0]["change"].update(actions=[]), "malformed action list"),
    (lambda p: p["resource_changes"][0]["change"].update(actions="create"), "malformed action list"),
    (lambda p: p["configuration"]["root_module"]["module_calls"].pop("vault_kms"), "no module call vault_kms"),
    (lambda p: p.update(resource_drift="x"), "resource_drift is not a list"),
], ids=["errored", "not-applyable", "deferred", "duplicate", "no-actions", "empty-actions", "actions-not-list", "no-module", "drift-shape"])
def test_untrustworthy_plan_structure_fails(mutate, needle):
    p = plan()
    mutate(p)
    assert any(needle in f for f in problems(p))


# ---- CLI: verdicts, exit codes, nothing sensitive printed

def run_cli(tmp_path, content, capsys):
    path = tmp_path / "plan.json"
    if content is not None:
        path.write_text(content)
    code = kms.main(["guard", "--plan", str(path)])
    return code, capsys.readouterr()


def test_cli_pass_prints_the_exact_creates(tmp_path, capsys):
    code, out = run_cli(tmp_path, json.dumps(plan()), capsys)
    shown = json.loads(out.out)
    assert code == 0 and shown["verdict"] == "VAULT_KMS_PLAN_GUARD_PASS" and shown["approved_creates"] == 7
    assert shown["unrelated_mutations"] == 0 and sorted(shown["creates"]) == sorted(A.values())


@pytest.mark.parametrize("content", [None, "{not json", json.dumps({"format_version": "1.2", "resource_changes": []})],
                         ids=["missing-file", "invalid-json", "empty-plan"])
def test_cli_fails_closed(tmp_path, capsys, content):
    code, out = run_cli(tmp_path, content, capsys)
    assert code == 1 and "VAULT_KMS_PLAN_GUARD_FAIL" in out.err and "PASS" not in out.out


def test_cli_never_prints_values_of_unrelated_resources(tmp_path, capsys):
    p = plan()
    extra = foreign("module.iam.google_secret_manager_secret_version.x", ["create"], type_="google_secret_manager_secret_version")
    extra["change"]["after"] = {"secret_data": "TOP-SECRET-SENTINEL"}
    p["resource_changes"].append(extra)
    code, out = run_cli(tmp_path, json.dumps(p), capsys)
    assert code == 1 and "TOP-SECRET-SENTINEL" not in out.out + out.err


# ---- completion mode (P4-B3R1): exactly the one missing member on the six-resource P4-B3 baseline
# The baseline values below are the attributes Terraform recorded for the six resources P4-B3 created
# (names and scopes only; nothing secret). The plan shape follows `terraform show -json`.

KEY_ID = kms.CRYPTO_KEY_ID
LABELS = {"environment": "dev", "goog-terraform-provisioned": "true", "managed_by": "terraform", "project": "ewallet"}
BASELINE = {
    "google_project_service.cloudkms": {
        "deletion_policy": "ABANDON", "disable_dependent_services": False, "disable_on_destroy": False,
        "id": f"{kms.PROJECT}/cloudkms.googleapis.com", "project": kms.PROJECT, "service": "cloudkms.googleapis.com",
        "timeouts": None},
    "google_kms_key_ring.vault": {"id": kms.KEY_RING_ID, "location": kms.LOCATION, "name": kms.KEY_RING,
                                  "project": kms.PROJECT, "timeouts": None},
    "google_kms_crypto_key.vault_unseal": {
        "crypto_key_backend": None, "deletion_policy": "PREVENT", "destroy_scheduled_duration": "2592000s",
        "effective_labels": LABELS, "id": KEY_ID, "import_only": False, "key_access_justifications_policy": [],
        "key_ring": kms.KEY_RING_ID, "labels": None, "name": kms.CRYPTO_KEY,
        "primary": [{"name": f"{KEY_ID}/cryptoKeyVersions/1", "state": "ENABLED"}], "purpose": "ENCRYPT_DECRYPT",
        "rotation_period": None, "skip_initial_version_creation": False, "terraform_labels": LABELS, "timeouts": None,
        "version_template": [{"algorithm": "GOOGLE_SYMMETRIC_ENCRYPTION", "protection_level": "SOFTWARE"}]},
    "google_service_account.vault_unseal": {
        "account_id": "ewallet-vault-unseal", "create_ignore_already_exists": None, "deletion_policy": "PREVENT",
        "description": "Vault gcpckms seal", "disabled": False, "display_name": "E-Wallet Vault auto-unseal",
        "email": kms.GSA_EMAIL, "id": kms.GSA_ID, "member": kms.GSA_MEMBER, "name": kms.GSA_ID,
        "project": kms.PROJECT, "timeouts": None, "unique_id": "100000000000000000000"},
    "google_kms_crypto_key_iam_member.vault_viewer": {
        "condition": [], "crypto_key_id": KEY_ID, "etag": "BwX=", "id": f"{KEY_ID}/roles/cloudkms.viewer/{kms.GSA_MEMBER}",
        "member": kms.GSA_MEMBER, "role": "roles/cloudkms.viewer"},
    "google_service_account_iam_member.vault_workload_identity": {
        "condition": [], "etag": "BwY=", "id": f"{kms.GSA_ID}/roles/iam.workloadIdentityUser/{kms.WI_MEMBER}",
        "member": kms.WI_MEMBER, "role": "roles/iam.workloadIdentityUser", "service_account_id": kms.GSA_ID},
}
MISSING_SHORT = "google_kms_crypto_key_iam_member.vault_encrypter_decrypter"


def _resource(short, values):
    kind, name = short.split(".")
    return {"address": A[short], "mode": "managed", "type": kind, "name": name, "index": 0,
            "provider_name": kms.PROVIDER, "schema_version": 0, "values": copy.deepcopy(values), "sensitive_values": {}}


def completion_plan():
    """A one-create completion plan on the P4-B3 baseline, in the `terraform show -json` shape."""
    gke = {"address": "module.gke", "resources": [{"address": "module.gke.google_container_node_pool.cpu", "mode": "managed",
                                                   "type": "google_container_node_pool", "provider_name": kms.PROVIDER,
                                                   "values": {"name": "cpu-pool"}}]}
    prior = [_resource(short, values) for short, values in BASELINE.items()]
    missing = {"condition": [], "crypto_key_id": KEY_ID, "member": kms.GSA_MEMBER, "role": "roles/cloudkms.cryptoKeyEncrypterDecrypter"}
    changes = [{"address": A[MISSING_SHORT], "module_address": "module.vault_kms", "mode": "managed",
                "type": "google_kms_crypto_key_iam_member", "name": "vault_encrypter_decrypter", "index": 0,
                "provider_name": kms.PROVIDER,
                "change": {"actions": ["create"], "before": None, "after": copy.deepcopy(missing),
                           "after_unknown": {"condition": [], "etag": True, "id": True},
                           "before_sensitive": False, "after_sensitive": {"condition": []}}}]
    for short, values in BASELINE.items():
        changes.append({"address": A[short], "module_address": "module.vault_kms", "mode": "managed",
                        "type": short.split(".")[0], "name": short.split(".")[1], "index": 0, "provider_name": kms.PROVIDER,
                        "change": {"actions": ["no-op"], "before": copy.deepcopy(values), "after": copy.deepcopy(values),
                                   "after_unknown": {}, "before_sensitive": {}, "after_sensitive": {}}})
    p = {key: copy.deepcopy(BASE[key]) for key in ("format_version", "terraform_version", "configuration", "variables")}
    p.update(errored=False, applyable=True, complete=False, resource_changes=changes,
             prior_state={"format_version": "1.0", "values": {"root_module": {"child_modules": [
                 {"address": "module.vault_kms", "resources": prior}, gke]}}},
             planned_values={"root_module": {"child_modules": [
                 {"address": "module.vault_kms", "resources": copy.deepcopy(prior) + [_resource(MISSING_SHORT, missing)]}]}})
    return p


def cproblems(p):
    return kms.guard_complete(p)[0]


def prior_of(p, short):
    return next(r for r in p["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"] if r["address"] == A[short])


def planned_of(p, short):
    return next(r for r in p["planned_values"]["root_module"]["child_modules"][0]["resources"] if r["address"] == A[short])


def test_completion_one_approved_create_on_the_unchanged_baseline_passes():
    found, summary = kms.guard_complete(completion_plan())
    assert found == [] and summary["approved_creates"] == [A[MISSING_SHORT]]
    assert summary["existing_unchanged"] == sorted(A[s] for s in BASELINE) and summary["unchanged_or_read"] == 6


def test_completion_passes_when_terraform_omits_the_no_op_entries():
    p = completion_plan()
    p["resource_changes"] = [c for c in p["resource_changes"] if c["address"] == A[MISSING_SHORT]]
    found, summary = kms.guard_complete(p)
    assert found == [] and len(summary["existing_unchanged"]) == 6     # the baseline comes from prior_state


def test_original_mode_still_passes_seven_creates_and_rejects_the_completion_plan():
    assert kms.guard(plan())[0] == []
    found = problems(completion_plan())
    assert len([f for f in found if "only create is approved" in f]) == 6, found


def test_completion_mode_rejects_the_seven_create_plan():
    found = cproblems(plan())
    assert any("missing from the baseline" in f for f in found)
    assert len([f for f in found if "an existing resource may not change" in f]) == 6, found


def test_the_mode_is_never_inferred(tmp_path, capsys):
    code, out = run_cli(tmp_path, json.dumps(completion_plan()), capsys)
    assert code == 1 and "VAULT_KMS_PLAN_GUARD_FAIL" in out.err


@pytest.mark.parametrize("short", ["google_kms_key_ring.vault", "google_kms_crypto_key.vault_unseal",
                                   "google_service_account.vault_unseal", "google_kms_crypto_key_iam_member.vault_viewer",
                                   "google_service_account_iam_member.vault_workload_identity", "google_project_service.cloudkms"])
def test_completion_a_missing_existing_resource_fails(short):
    p = completion_plan()
    child = p["prior_state"]["values"]["root_module"]["child_modules"][0]
    child["resources"] = [r for r in child["resources"] if r["address"] != A[short]]
    assert any(f"prior_state {A[short]}: missing from the baseline" in f for f in cproblems(p))


@pytest.mark.parametrize("short,key,value", [
    ("google_service_account.vault_unseal", "email", "other@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com"),
    ("google_service_account.vault_unseal", "disabled", True),
    ("google_kms_crypto_key.vault_unseal", "rotation_period", "7776000s"),
    ("google_kms_crypto_key.vault_unseal", "purpose", "ASYMMETRIC_DECRYPT"),
    ("google_kms_crypto_key.vault_unseal", "primary", [{"name": f"{KEY_ID}/cryptoKeyVersions/1", "state": "DISABLED"}]),
    ("google_kms_crypto_key.vault_unseal", "key_ring", f"projects/{kms.PROJECT}/locations/{kms.LOCATION}/keyRings/other"),
    ("google_kms_crypto_key_iam_member.vault_viewer", "role", "roles/cloudkms.admin"),
    ("google_kms_crypto_key_iam_member.vault_viewer", "crypto_key_id", f"{kms.KEY_RING_ID}/cryptoKeys/other"),
    ("google_service_account_iam_member.vault_workload_identity", "member",
     f"serviceAccount:{kms.PROJECT}.svc.id.goog[ewallet-vault/default]"),
    ("google_kms_key_ring.vault", "location", "us-central1"),
], ids=["wrong-gsa", "gsa-disabled", "key-rotation", "key-purpose", "key-version-disabled", "key-other-ring",
        "viewer-role", "viewer-other-key", "wi-other-ksa", "ring-location"])
def test_completion_a_wrong_existing_resource_fails(short, key, value):
    p = completion_plan()
    prior_of(p, short)["values"][key] = value
    planned_of(p, short)["values"][key] = value
    assert any(f"prior_state {A[short]}" in f for f in cproblems(p))


def test_completion_an_existing_key_changed_in_the_plan_fails():
    p = completion_plan()
    change = next(c for c in p["resource_changes"] if c["address"] == A["google_kms_crypto_key.vault_unseal"])
    change["change"]["actions"] = ["update"]
    change["change"]["after"]["rotation_period"] = "7776000s"
    planned_of(p, "google_kms_crypto_key.vault_unseal")["values"]["rotation_period"] = "7776000s"
    found = cproblems(p)
    assert any("update module.vault_kms.google_kms_crypto_key.vault_unseal[0]: an existing resource may not change" in f for f in found)
    assert any("planned_values module.vault_kms.google_kms_crypto_key.vault_unseal[0]: differs" in f for f in found)


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"], ["update"], ["forget"]])
def test_completion_any_mutation_of_an_existing_resource_fails(actions):
    p = completion_plan()
    next(c for c in p["resource_changes"] if c["address"] == A["google_service_account.vault_unseal"])["change"]["actions"] = actions
    assert any("an existing resource may not change" in f for f in cproblems(p))


def test_completion_an_existing_no_op_with_different_before_and_after_fails():
    p = completion_plan()
    next(c for c in p["resource_changes"] if c["address"] == A["google_kms_crypto_key_iam_member.vault_viewer"])["change"]["after"]["role"] = "roles/owner"
    assert any("an existing resource may not change" in f for f in cproblems(p))


@pytest.mark.parametrize("key,value,needle", [
    ("member", "serviceAccount:other@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com", "member"),
    ("member", "allUsers", "member"),
    ("role", "roles/cloudkms.admin", "role"),
    ("role", "roles/cloudkms.cryptoKeyDecrypter", "role"),
    ("crypto_key_id", f"{kms.KEY_RING_ID}/cryptoKeys/other", "crypto_key_id"),
    ("condition", [{"expression": "true", "title": "x"}], "condition"),
], ids=["other-gsa", "all-users", "admin-role", "decrypter-only", "other-key", "condition"])
def test_completion_a_wrong_missing_grant_fails(key, value, needle):
    p = completion_plan()
    p["resource_changes"][0]["change"]["after"][key] = value
    found = cproblems(p)
    assert any(A[MISSING_SHORT] in f and needle in f for f in found), found


@pytest.mark.parametrize("key", ["crypto_key_id", "role", "member"])
def test_completion_an_unknown_grant_attribute_or_scope_fails(key):
    p = completion_plan()
    change = p["resource_changes"][0]["change"]
    change["after"].pop(key)
    change["after_unknown"][key] = True
    assert any(A[MISSING_SHORT] in f for f in cproblems(p))


@pytest.mark.parametrize("extra,needle", [
    (foreign("module.vault_kms.google_kms_crypto_key_iam_member.vault_admin[0]", ["create"],
             type_="google_kms_crypto_key_iam_member"), "not an approved module.vault_kms resource"),
    (foreign("google_project_iam_member.vault_kms", ["create"], type_="google_project_iam_member"), "outside the approved completion"),
    (foreign("module.gke.google_container_node_pool.cpu[0]", ["delete", "create"]), "delete+create module.gke"),
    (foreign('module.iam.google_service_account.workload["ci"]', ["delete"], type_="google_service_account"), "delete module.iam"),
    (foreign("module.gke.google_container_cluster.platform[0]", ["update"], type_="google_container_cluster"), "update module.gke"),
], ids=["additional-key-grant", "project-wide-grant", "node-pool-replace", "unrelated-delete", "cluster-update"])
def test_completion_any_other_mutation_fails(extra, needle):
    p = completion_plan()
    p["resource_changes"].append(extra)
    assert any(needle in f for f in cproblems(p))


def test_completion_two_creates_or_zero_creates_fail():
    two = completion_plan()
    two["resource_changes"].append(foreign("module.vault_kms.google_kms_crypto_key_iam_member.vault_encrypter_decrypter[1]",
                                           ["create"], type_="google_kms_crypto_key_iam_member"))
    assert any("[1]: not an approved" in f for f in cproblems(two))
    zero = completion_plan()
    zero["resource_changes"] = zero["resource_changes"][1:]
    assert any(f"{A[MISSING_SHORT]}: missing from the plan" in f for f in cproblems(zero))


def test_completion_the_missing_grant_already_in_state_fails():
    p = completion_plan()
    p["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"].append(planned_of(p, MISSING_SHORT))
    assert any("not part of the six-resource baseline" in f for f in cproblems(p))


def test_completion_an_import_fails():
    p = completion_plan()
    p["resource_changes"][0]["change"]["importing"] = {"id": "x"}
    assert any("imports are not part" in f for f in cproblems(p))


def test_completion_drift_inside_the_module_fails():
    p = completion_plan()
    p["resource_drift"] = [{"address": A["google_kms_crypto_key_iam_member.vault_viewer"]},
                           {"address": "module.gke.google_container_node_pool.cpu[0]"}]
    found, summary = kms.guard_complete(p)
    assert any("drifted outside Terraform since P4-B3" in f for f in found)
    assert summary["drift_outside_the_change"] == ["module.gke.google_container_node_pool.cpu[0]"]


def test_completion_an_unexpected_provider_fails():
    p = completion_plan()
    prior_of(p, "google_kms_key_ring.vault")["provider_name"] = "registry.terraform.io/example/google"
    p["configuration"]["provider_config"]["google"]["full_name"] = "registry.terraform.io/example/google"
    p["resource_changes"][0]["provider_name"] = "registry.terraform.io/example/google"
    found = cproblems(p)
    assert any("google provider is not" in f for f in found) and any("provider differs" in f for f in found)


def test_completion_planned_values_must_hold_exactly_the_seven():
    p = completion_plan()
    child = p["planned_values"]["root_module"]["child_modules"][0]
    child["resources"] = [r for r in child["resources"] if r["address"] != A["google_kms_crypto_key.vault_unseal"]]
    assert any("planned_values module.vault_kms.google_kms_crypto_key.vault_unseal[0]: expected exactly" in f for f in cproblems(p))


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p.clear(), "format_version"),
    (lambda p: p.pop("resource_changes"), "no resource_changes"),
    (lambda p: p.pop("prior_state"), "prior_state has no root_module"),
    (lambda p: p.pop("planned_values"), "planned_values has no root_module"),
    (lambda p: p.update(errored=True), "errored"),
    (lambda p: p.update(applyable=False), "not applyable"),
    (lambda p: p.update(deferred_changes=[{"resource_change": {}}]), "defers"),
    (lambda p: p["configuration"]["root_module"]["module_calls"].pop("vault_kms"), "no module call vault_kms"),
    (lambda p: p["configuration"]["root_module"]["module_calls"]["vault_kms"]["module"]["resources"].pop(), "beyond or short"),
    (lambda p: p["resource_changes"].append(copy.deepcopy(p["resource_changes"][0])), "appears twice"),
    (lambda p: p["resource_changes"][0]["change"].update(actions="create"), "malformed action list"),
    (lambda p: p.update(resource_drift="x"), "resource_drift is not a list"),
], ids=["empty", "no-changes", "no-prior-state", "no-planned-values", "errored", "not-applyable", "deferred",
        "no-module", "module-short", "duplicate", "actions-not-list", "drift-shape"])
def test_completion_untrustworthy_plans_fail(mutate, needle):
    p = completion_plan()
    mutate(p)
    assert any(needle in f for f in cproblems(p))


def run_complete_cli(tmp_path, content, capsys):
    path = tmp_path / "plan.json"
    if content is not None:
        path.write_text(content)
    return kms.main(["guard", "--mode", "complete", "--plan", str(path)]), capsys.readouterr()


def test_cli_completion_pass_and_fail_closed(tmp_path, capsys):
    code, out = run_complete_cli(tmp_path, json.dumps(completion_plan()), capsys)
    shown = json.loads(out.out)
    assert code == 0 and shown["verdict"] == "VAULT_KMS_COMPLETION_PLAN_GUARD_PASS" and shown["approved_creates"] == 1
    assert shown["creates"] == [A[MISSING_SHORT]] and len(shown["existing_unchanged"]) == 6
    for index, content in enumerate((None, "{not json", json.dumps(plan()))):
        (tmp_path / str(index)).mkdir()
        code, out = run_complete_cli(tmp_path / str(index), content, capsys)
        assert code == 1 and "VAULT_KMS_COMPLETION_PLAN_GUARD_FAIL" in out.err and "PASS" not in out.out


# ---- the module itself (static) and its agreement with the Vault values

def resource_block(kind, name):
    match = re.search(rf'resource "{kind}" "{name}" \{{(.*?)\n\}}', MODULE, re.S)
    assert match, (kind, name)
    return match.group(1)


def test_module_declares_exactly_the_approved_resources_and_nothing_authoritative():
    declared = re.findall(r'resource "([a-z0-9_]+)" "([a-z0-9_]+)"', MODULE)
    assert sorted(f"module.vault_kms.{t}.{n}[0]" for t, n in declared) == sorted(kms.APPROVED)
    assert not re.search(r"_iam_(binding|policy)\b", MODULE_CODE) and "google_service_account_key" not in MODULE_CODE
    assert "data \"" not in MODULE_CODE and "provisioner" not in MODULE_CODE and "module." not in MODULE_CODE


def test_key_ring_key_and_gsa_are_protected_and_the_api_is_never_disabled():
    for kind, name in (("google_kms_key_ring", "vault"), ("google_kms_crypto_key", "vault_unseal"),
                       ("google_service_account", "vault_unseal")):
        assert re.search(r"lifecycle \{\s*prevent_destroy = true\s*\}", resource_block(kind, name)), name
    assert 'deletion_policy            = "PREVENT"' in resource_block("google_kms_crypto_key", "vault_unseal")
    assert 'deletion_policy = "PREVENT"' in resource_block("google_service_account", "vault_unseal")
    api = resource_block("google_project_service", "cloudkms")
    assert "disable_on_destroy         = false" in api and 'deletion_policy            = "ABANDON"' in api
    key = resource_block("google_kms_crypto_key", "vault_unseal")
    assert "rotation_period" not in key and 'protection_level = "SOFTWARE"' in key


def test_root_module_has_no_dependency_on_gke_or_workload_identity():
    block = re.search(r'module "vault_kms" \{(.*?)\n\}', DEV_MAIN, re.S).group(1)
    assert "depends_on" not in block and "module." not in block
    assert re.findall(r"^\s*(\w+)\s*=", block, re.M) == ["source", "enabled", "project_id"]
    variables = (TF / "environments/dev/variables.tf").read_text()
    assert re.search(r'variable "enable_vault_kms" \{[^}]*default\s*=\s*false', variables, re.S)
    module_vars = (TF / "modules/vault_kms/variables.tf").read_text()
    assert re.findall(r'variable "(\w+)"', module_vars) == ["enabled", "project_id"]
    assert re.search(r'variable "enabled" \{[^}]*default\s*=\s*false', module_vars, re.S)


def test_cloudkms_api_has_one_owner():
    services = re.search(r"required_services = toset\(\[(.*?)\]\)", DEV_MAIN, re.S).group(1)
    assert "cloudkms.googleapis.com" not in services      # owned by module.vault_kms only


def test_module_names_equal_the_vault_seal_and_annotation_and_the_guard():
    values = yaml.safe_load((ROOT / "infra/kubernetes/vault/values-gke.yaml").read_text())
    seal = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', re.search(r'seal "gcpckms" \{([^}]*)\}', values["server"]["ha"]["raft"]["config"]).group(1)))
    local = dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]*)"', re.search(r"locals \{(.*?)\n\}", MODULE, re.S).group(1), re.M))
    assert seal == {"project": kms.PROJECT, "region": local["location"], "key_ring": local["key_ring_name"],
                    "crypto_key": local["crypto_key_name"]}
    assert (kms.LOCATION, kms.KEY_RING, kms.CRYPTO_KEY) == (local["location"], local["key_ring_name"], local["crypto_key_name"])
    annotation = values["server"]["serviceAccount"]["annotations"]["iam.gke.io/gcp-service-account"]
    assert annotation == kms.GSA_EMAIL == f'{local["service_account_id"]}@{kms.PROJECT}.iam.gserviceaccount.com'
    namespace = yaml.safe_load((ROOT / "infra/kubernetes/vault/namespace.yaml").read_text())["metadata"]["name"]
    assert kms.WI_MEMBER == f"serviceAccount:{kms.PROJECT}.svc.id.goog[{local['vault_ksa']}]" and local["vault_ksa"] == f"{namespace}/vault"
    project_rule = (TF / "environments/dev/variables.tf").read_text()
    assert f'var.project_id == "{kms.PROJECT}"' in project_rule


def test_the_r2_guard_is_unchanged():
    assert R2_APPROVED == frozenset({
        'module.iam.google_service_account.workload["fraud_api"]', 'module.iam.google_service_account.workload["feature_api"]',
        'module.iam.google_storage_bucket_iam_member.registry_reader["fraud_api"]',
        'module.iam.google_storage_bucket_iam_member.registry_reader["feature_api"]'})
    assert not set(R2_APPROVED) & set(kms.APPROVED)


# ---- optional: regenerate the real offline plan (needs terraform and a google provider mirror)

MIRROR = os.environ.get("VAULT_KMS_TF_MIRROR", "")


@pytest.mark.skipif(not (shutil.which("terraform") and MIRROR and Path(MIRROR).is_dir()),
                    reason="set VAULT_KMS_TF_MIRROR to a google provider filesystem mirror to run the offline plan")
def test_offline_targeted_plan_contains_only_the_module_and_passes(tmp_path):
    done = subprocess.run(["bash", str(ROOT / "scripts/vault/kms_offline_plan.sh"), str(tmp_path / "out"), MIRROR],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    fresh = json.loads((tmp_path / "out/targeted.plan.json").read_text())
    assert sorted(c["address"] for c in fresh["resource_changes"]) == sorted(kms.APPROVED)
    assert kms.guard(fresh)[0] == []
