"""R2 Terraform guard: only the four approved IAM creates may pass."""

from __future__ import annotations

import json
import os

import pytest

from ops.gcp.r2_terraform import APPROVED_CREATES, guard, main, tfvars

CONDITION = 'resource.name.startsWith("projects/_/buckets/b/objects/features/registries/")'


def change(address, actions, after=None):
    return {"address": address, "change": {"actions": actions, "after": after or {}}}


def approved_plan():
    changes = []
    for address in sorted(APPROVED_CREATES):
        after = {"role": "roles/storage.objectViewer", "condition": [{"expression": CONDITION}]} \
            if "registry_reader" in address else {}
        changes.append(change(address, ["create"], after))
    changes.append(change("module.jenkins_vm.data.google_compute_image.ubuntu", ["read"]))
    return {"resource_changes": changes}


def test_the_approved_change_passes():
    assert guard(approved_plan()) == []


@pytest.mark.parametrize("extra", [
    change("module.gke.google_container_node_pool.cpu[0]", ["delete", "create"]),
    change("module.gke.google_container_cluster.platform[0]", ["update"]),
    change('module.workload_identity.google_service_account_iam_member.workload_identity["fraud_api"]', ["create"]),
])
def test_any_other_action_stops_the_apply(extra):
    plan = approved_plan()
    plan["resource_changes"].append(extra)
    assert any(extra["address"] in problem for problem in guard(plan))


def test_a_bucket_wide_reader_is_refused():
    plan = approved_plan()
    for item in plan["resource_changes"]:
        if "registry_reader" in item["address"]:
            item["change"]["after"]["condition"] = []
    assert any("registry-only" in problem for problem in guard(plan))


def test_a_missing_create_is_reported():
    plan = approved_plan()
    plan["resource_changes"].pop(0)
    assert any("missing" in problem for problem in guard(plan))


def state(cidrs=("203.0.113.7/32",)):
    return {"resources": [
        {"mode": "managed", "type": "google_container_cluster", "instances": [{"attributes": {
            "deletion_protection": True,
            "master_authorized_networks_config": [{"cidr_blocks": [{"cidr_block": c} for c in cidrs]}],
        }}]},
        {"mode": "managed", "type": "google_container_node_pool", "instances": [{"attributes": {
            "autoscaling": [{"min_node_count": 0, "max_node_count": 3}],
            "node_config": [{"machine_type": "e2-standard-4"}],
        }}]},
    ]}


def test_tfvars_reproduce_the_applied_settings():
    text = tfvars(state())
    assert "enable_gke = true" in text
    assert 'gke_master_authorized_cidrs = ["203.0.113.7/32"]' in text
    assert "gke_min_nodes = 0" in text and "gke_max_nodes = 3" in text
    assert 'gke_machine_type = "e2-standard-4"' in text


def test_tfvars_file_is_private_and_the_cidr_is_not_printed(tmp_path, capsys):
    source = tmp_path / "state.json"
    source.write_text(json.dumps(state()))
    out = tmp_path / "r2.auto.tfvars"
    assert main(["tfvars", "--state", str(source), "--out", str(out)]) == 0
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    assert "203.0.113.7" not in capsys.readouterr().out


def test_guard_command_exit_codes(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(approved_plan()))
    assert main(["guard", "--plan", str(good)]) == 0
    bad_plan = approved_plan()
    bad_plan["resource_changes"].append(change("module.gke.google_container_node_pool.cpu[0]", ["delete", "create"]))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_plan))
    assert main(["guard", "--plan", str(bad)]) == 1
