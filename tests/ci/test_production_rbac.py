"""The proposed Jenkins RBAC must stay narrow and match its reviewed matrix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.ci import rbac_matrix

ROOT = Path(__file__).resolve().parents[2]
PROPOSAL = ROOT / "infra/kubernetes/jenkins/production-rollout-rbac.proposed.yaml"
DOCUMENTS = [item for item in yaml.safe_load_all(PROPOSAL.read_text(encoding="utf-8")) if item]
ROLES = [item for item in DOCUMENTS if item["kind"] == "Role"]
MATRIX = json.loads((ROOT / "jenkins/cd/production-rbac-matrix.json").read_text(encoding="utf-8"))


def allowed(row: dict) -> bool:
    """Evaluate one matrix row against the proposed Roles, as the API server would."""

    resource, _, group = row["resource"].partition(".")
    for role in ROLES:
        if role["metadata"]["namespace"] != row["namespace"]:
            continue
        for rule in role["rules"]:
            names = rule.get("resourceNames")
            if (group in rule["apiGroups"] and resource in rule["resources"] and row["verb"] in rule["verbs"]
                    and (not names or row["name"] in names)):
                return True
    return False


@pytest.mark.parametrize("row", MATRIX["rows"], ids=lambda r: f"{r['namespace']}-{r['verb']}-{r['resource']}-{r['name']}")
def test_roles_grant_exactly_the_matrix(row) -> None:
    assert allowed(row) is row["after"]


def test_only_namespaced_objects_of_expected_kinds() -> None:
    assert {item["kind"] for item in DOCUMENTS} == {"Role", "RoleBinding"}
    assert {item["metadata"]["namespace"] for item in DOCUMENTS} == {"ewallet-mcp", "ewallet-agents"}


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r["metadata"]["name"])
def test_no_wildcard_secret_or_mutating_broad_rule(role) -> None:
    for rule in role["rules"]:
        assert "*" not in rule["verbs"] + rule["resources"] + rule["apiGroups"]
        assert not {"secrets", "pods/exec", "pods/portforward", "pods/attach", "rolebindings", "roles"} & set(rule["resources"])
        assert not {"create", "delete", "deletecollection", "update", "escalate", "bind", "impersonate"} & set(rule["verbs"])
        if "patch" in rule["verbs"]:
            assert rule["resourceNames"], "a write must name its objects"


def test_bindings_name_only_the_jenkins_account() -> None:
    for item in DOCUMENTS:
        if item["kind"] == "RoleBinding":
            assert item["subjects"] == [{"kind": "ServiceAccount", "name": "jenkins", "namespace": "ewallet-ci"}]
            assert item["roleRef"]["kind"] == "Role"


def test_nothing_applies_the_proposal() -> None:
    name = PROPOSAL.name
    for pattern in ("jenkins/**/*", "scripts/**/*", "infra/kubernetes/jenkins/*"):
        for path in ROOT.glob(pattern):
            if path.is_file() and path != PROPOSAL and path.suffix in {".sh", ".py", ".groovy", ".yaml", ".yml", ".json"}:
                assert name not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_before_state_expects_every_production_right_absent() -> None:
    answers = rbac_matrix.evaluate(MATRIX, "ctx", "before", lambda arguments: "no")
    assert answers == []


def test_an_existing_right_fails_the_before_check() -> None:
    answers = rbac_matrix.evaluate(MATRIX, "ctx", "before", lambda arguments: "yes" if "patch" in arguments else "no")
    assert answers


def test_unreadable_answer_is_a_failure() -> None:
    assert rbac_matrix.evaluate(MATRIX, "ctx", "after", lambda arguments: "error")


def test_command_shape() -> None:
    row = {"namespace": "ewallet-mcp", "verb": "patch", "resource": "deployments.apps", "name": "drift-mcp"}
    assert rbac_matrix.command("c", "s", row) == ["kubectl", "--context", "c", "auth", "can-i", "patch",
                                                  "deployments.apps/drift-mcp", "-n", "ewallet-mcp", "--as=s"]
