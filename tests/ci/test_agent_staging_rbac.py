"""CI/CD V2-P3B-0: the proposed Jenkins staging RBAC covers exactly what agent_staging.py and Helm do (offline).

Every operation the staging release performs is listed below and must be allowed by a namespaced rule; the
proposal must stay namespaced, bound only to ewallet-ci/jenkins, outside production, and never write Secrets.
The live equivalent is `kubectl auth can-i` (docs/workplans/edai2/CICD_V2_P3B0_PREREQUISITES.md, section C).
"""

from __future__ import annotations

from pathlib import Path
import shutil
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROPOSAL = ROOT / "infra/kubernetes/jenkins/rbac/agent-staging-cd.proposed.yaml"
AGENTS, SANDBOX, KAGENT = "ewallet-agents-staging", "ewallet-sandbox-staging", "kagent"
SUBJECT = {"kind": "ServiceAccount", "name": "jenkins", "namespace": "ewallet-ci"}
HELM_VERBS = ("get", "list", "create", "patch", "update", "delete")
# (apiGroup, resource) of every kind values-staging.yaml renders, plus the Helm configmap-driver record.
RENDERED = {"Deployment": ("apps", "deployments"), "StatefulSet": ("apps", "statefulsets"),
            "Agent": ("kagent.dev", "agents"), "ModelConfig": ("kagent.dev", "modelconfigs"),
            "RemoteMCPServer": ("kagent.dev", "remotemcpservers"),
            "NetworkPolicy": ("networking.k8s.io", "networkpolicies"),
            "AuthorizationPolicy": ("security.istio.io", "authorizationpolicies"),
            "ConfigMap": ("", "configmaps"), "Service": ("", "services"), "ServiceAccount": ("", "serviceaccounts")}
OPERATIONS = [(AGENTS, group, resource, verb) for group, resource in RENDERED.values() for verb in HELM_VERBS] + [
    (AGENTS, "", "secrets", "get"), (AGENTS, "", "secrets", "list"),            # preflight existence checks
    (AGENTS, "apps", "deployments", "get"),                                     # dependency + Coordinator readiness
    (AGENTS, "", "pods/portforward", "create"),                                 # A2A / MCP gateway E2E
    (SANDBOX, "", "secrets", "get"), (SANDBOX, "", "secrets", "list"),
    *[(SANDBOX, group, resource, verb) for group, resource in
      (("ate.dev", "workerpools"), ("kagent.dev", "sandboxagents"), ("", "serviceaccounts"))
      for verb in ("get", "create", "delete")],
    (SANDBOX, "ate.dev", "actortemplates", "list"),
    (KAGENT, "", "services", "get"), (KAGENT, "", "pods", "list"), (KAGENT, "", "pods/portforward", "create"),
]
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*"}


def documents() -> list[dict]:
    return [doc for doc in yaml.safe_load_all(PROPOSAL.read_text(encoding="utf-8")) if doc]


def allowed(namespace: str, group: str, resource: str, verb: str) -> bool:
    for role in (d for d in documents() if d["kind"] == "Role" and d["metadata"]["namespace"] == namespace):
        for rule in role["rules"]:
            if group in rule["apiGroups"] and resource in rule["resources"] and verb in rule["verbs"]:
                return True
    return False


class StagingRbacTests(unittest.TestCase):
    def test_every_staging_operation_is_allowed(self):
        self.assertEqual([op for op in OPERATIONS if not allowed(*op)], [])

    def test_identity_is_namespaced_and_bound_only_to_jenkins(self):
        docs = documents()
        self.assertEqual({d["kind"] for d in docs}, {"Role", "RoleBinding"})
        self.assertEqual({d["metadata"]["namespace"] for d in docs}, {AGENTS, SANDBOX, KAGENT})
        for binding in (d for d in docs if d["kind"] == "RoleBinding"):
            self.assertEqual(binding["subjects"], [SUBJECT])
            self.assertEqual(binding["roleRef"]["kind"], "Role")

    def test_no_secret_writes_no_namespace_rights_no_wildcards_and_kagent_read_only(self):
        for role in (d for d in documents() if d["kind"] == "Role"):
            for rule in role["rules"]:
                with self.subTest(role=role["metadata"]["name"], resources=rule["resources"]):
                    self.assertNotIn("*", rule["verbs"] + rule["resources"] + rule["apiGroups"])
                    self.assertNotIn("namespaces", rule["resources"])
                    if "secrets" in rule["resources"]:
                        self.assertEqual(set(rule["verbs"]), {"get", "list"})
                    if role["metadata"]["namespace"] == KAGENT and set(rule["verbs"]) & WRITE_VERBS:
                        self.assertEqual((rule["resources"], rule["verbs"]), (["pods/portforward"], ["create"]))

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_rendered_kinds_are_the_ones_the_rbac_covers(self):
        from tests.llm_agent.test_v2p1_staging_chart import render_staging

        rendered = render_staging()
        self.assertEqual(rendered.returncode, 0, rendered.stderr[-400:])
        kinds = {doc["kind"] for doc in yaml.safe_load_all(rendered.stdout) if doc}
        self.assertNotIn("Secret", kinds)   # nothing for Helm to write as a Secret
        self.assertLessEqual(kinds, set(RENDERED))


if __name__ == "__main__":
    unittest.main()
