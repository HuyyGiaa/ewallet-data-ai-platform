from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
PILOT = ROOT / "infra" / "kubernetes" / "kagent-pilot"


def load_documents(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


class KagentBoundedPilotTest(unittest.TestCase):
    def test_versions_are_pinned(self) -> None:
        text = (PILOT / "versions.env").read_text()
        self.assertRegex(text, r"(?m)^KAGENT_VERSION=0\.10\.3$")
        self.assertRegex(text, r"(?m)^SUBSTRATE_VERSION=0\.0\.9$")
        self.assertNotRegex(text.lower(), r"latest")

    def test_substrate_uses_exact_gke_jwt_issuer(self) -> None:
        values = yaml.safe_load((PILOT / "values" / "substrate.yaml").read_text())
        self.assertEqual(values["auth"]["mode"], "jwt")
        self.assertEqual(
            values["auth"]["jwt"]["issuer"],
            "https://container.googleapis.com/v1/projects/ewallet-data-ai-platform-hg26/locations/asia-southeast1-b/clusters/ewallet-dev",
        )
        self.assertEqual(values["valkey"]["replicas"], 6)
        self.assertEqual(values["valkey"]["storageSize"], "256Mi")
        self.assertEqual(values["rustfs"]["storageSize"], "256Mi")

    def test_kagent_disables_unneeded_components_and_scopes_rbac(self) -> None:
        values = yaml.safe_load((PILOT / "values" / "kagent.yaml").read_text())
        self.assertEqual(values["rbac"]["namespaces"], ["kagent"])
        self.assertEqual(values["controller"]["watchNamespaces"], ["kagent"])
        self.assertTrue(values["controller"]["substrate"]["enabled"])
        self.assertEqual(values["substrateWorkerPool"]["replicas"], 1)
        self.assertEqual(values["ui"]["replicas"], 0)
        self.assertIsNone(values["providers"])
        bundled_db = values["database"]["postgres"]["bundled"]
        self.assertEqual(bundled_db["storage"], "256Mi")
        self.assertEqual(bundled_db["resources"]["requests"]["memory"], "128Mi")
        for component in (
            "kmcp",
            "kagent-tools",
            "grafana-mcp",
            "k8s-agent",
            "kgateway-agent",
            "istio-agent",
            "promql-agent",
            "observability-agent",
            "argo-rollouts-agent",
            "helm-agent",
            "cilium-policy-agent",
            "cilium-manager-agent",
            "cilium-debug-agent",
        ):
            self.assertFalse(values[component]["enabled"], component)

    def test_mock_is_internal_deterministic_and_hardened(self) -> None:
        docs = load_documents(PILOT / "manifests" / "mock-openai.yaml")
        by_kind = {doc["kind"]: doc for doc in docs}
        service = by_kind["Service"]
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        deployment = by_kind["Deployment"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("@sha256:", container["image"])
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/readyz")
        self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/healthz")
        security = container["securityContext"]
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        script = next(doc for doc in docs if doc["kind"] == "ConfigMap")["data"]["server.py"]
        self.assertIn("KAGENT_PILOT_OK", script)
        self.assertIn("/v1/chat/completions", script)
        compile(script, "mock-openai/server.py", "exec")

    def test_model_and_sandbox_form_the_intended_path(self) -> None:
        model_docs = load_documents(PILOT / "manifests" / "model-config.yaml")
        model = next(doc for doc in model_docs if doc["kind"] == "ModelConfig")
        self.assertEqual(model["apiVersion"], "kagent.dev/v1alpha2")
        self.assertEqual(model["spec"]["provider"], "OpenAI")
        self.assertEqual(
            model["spec"]["openAI"]["baseUrl"],
            "http://mock-openai.kagent.svc.cluster.local:8080/v1",
        )
        agent = load_documents(PILOT / "manifests" / "sandbox-agent.yaml")[0]
        self.assertEqual(agent["kind"], "SandboxAgent")
        self.assertEqual(agent["spec"]["declarative"]["modelConfig"], "pilot-mock-openai")
        self.assertEqual(agent["spec"]["declarative"]["tools"], [])
        self.assertEqual(agent["spec"]["substrate"]["workerPoolRef"]["name"], "kagent-pilot")
        self.assertEqual(
            agent["spec"]["sandbox"]["network"]["allowedDomains"],
            ["mock-openai.kagent.svc.cluster.local"],
        )

    def test_no_external_service_or_cluster_admin(self) -> None:
        manifest_text = "\n".join(
            path.read_text() for path in sorted((PILOT / "manifests").glob("*.yaml"))
        )
        self.assertNotIn("type: LoadBalancer", manifest_text)
        self.assertNotIn("kind: Ingress", manifest_text)
        self.assertNotIn("cluster-admin", manifest_text)
        self.assertNotIn("kind: ClusterRoleBinding", manifest_text)

    def test_substrate_post_renderer_bounds_every_workload_container(self) -> None:
        rendered = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ate-api-server-deployment
spec:
  template:
    spec:
      initContainers:
        - name: init
          image: example.invalid/init:1
      containers:
        - name: ate-api-server
          image: example.invalid/api:1
---
apiVersion: v1
kind: Service
metadata:
  name: untouched
spec: {}
"""
        bounded = subprocess.run(
            [str(PILOT / "scripts" / "substrate_resource_post_renderer.py")],
            input=rendered,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        workloads = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
        for document in yaml.safe_load_all(bounded):
            if not document or document.get("kind") not in workloads:
                continue
            pod_spec = document["spec"]["template"]["spec"]
            for section in ("initContainers", "containers"):
                for container in pod_spec.get(section, []):
                    with self.subTest(
                        workload=document["metadata"]["name"], container=container["name"]
                    ):
                        self.assertIn("requests", container["resources"])
                        self.assertIn("limits", container["resources"])


if __name__ == "__main__":
    unittest.main()
