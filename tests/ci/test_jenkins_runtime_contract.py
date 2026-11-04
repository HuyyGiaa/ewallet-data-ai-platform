from __future__ import annotations

from pathlib import Path
import re
import unittest

import yaml

from scripts.ci.catalog import load_catalog
from scripts.ci.change_detection import detect_scope
from scripts.ci.render_jenkins_runtime import render


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "jenkins/catalog/components.json"


class JenkinsRuntimeContractTests(unittest.TestCase):
    def test_catalog_has_separate_ml_and_agent_profiles(self) -> None:
        catalog = load_catalog(CATALOG_PATH)
        self.assertEqual(
            set(catalog.profiles["ml"]),
            {"feast", "ml-training", "mlflow", "fraud-serving", "fraud-api"},
        )
        self.assertEqual(
            set(catalog.profiles["llm-agent"]),
            {"llm-serving", "observability", "rag-pipeline", "mcp-agents", "llm-d-routing",
             "agents", "prompt-management"},
        )

    def test_documentation_only_scope_selects_no_runtime_components(self) -> None:
        catalog = load_catalog(CATALOG_PATH)
        for path in ("README.md", "docs/architecture/overview.md"):
            with self.subTest(path=path):
                scope = detect_scope(catalog, [path])
                self.assertEqual(scope.components, ())
                self.assertEqual(scope.runtime_units, ())
                self.assertEqual(scope.full_validation_reason, "non_runtime_paths_only")

    def test_unknown_runtime_path_still_fails_closed_to_full_validation(self) -> None:
        catalog = load_catalog(CATALOG_PATH)
        scope = detect_scope(catalog, ["unknown_runtime/service.py"])
        self.assertEqual(scope.components, catalog.active_components)
        self.assertEqual(scope.full_validation_reason, "unmapped_changed_paths")

    def test_pipeline_has_explicit_negative_gate_before_release(self) -> None:
        text = (ROOT / "jenkins/pipeline/componentPipeline.groovy").read_text()
        self.assertIn("def run(Map configuration)", text)
        self.assertNotIn("def run(Map configuration =", text)
        self.assertIn("INTENTIONAL_GATE_FAILURE", text)
        self.assertIn("Intentional negative contract gate", text)
        self.assertLess(
            text.index("Intentional negative contract gate"),
            text.index("Controlled release gate"),
        )
        self.assertIn("exit 42", text)

    def test_job_dsl_owns_parameters_and_pipeline_never_resets_them(self) -> None:
        pipeline = (ROOT / "jenkins/pipeline/componentPipeline.groovy").read_text()
        guarded = pipeline.split("if (configuration.get('declareParameters', false)) {", 1)
        self.assertEqual(len(guarded), 2, "properties() must sit behind declareParameters")
        self.assertNotIn("properties(", guarded[0])
        self.assertEqual(pipeline.count("properties("), 1)
        self.assertIn("params.containsKey(required)", pipeline)
        self.assertRegex(pipeline, r"booleanParam\(name: 'DEPLOY_APPROVED', defaultValue: false")
        for name in ("Jenkinsfile", "Jenkinsfile.ml", "Jenkinsfile.llm-agent"):
            self.assertNotIn("declareParameters", (ROOT / name).read_text(), name)
        casc = (ROOT / "infra/kubernetes/jenkins/jenkins.yaml").read_text()
        self.assertIn("booleanParam('DEPLOY_APPROVED', false", casc)
        self.assertNotRegex(casc, r"booleanParam\('DEPLOY_APPROVED', true")
        self.assertIn("stringParam('RELEASE_BRANCH'", casc)

    def test_ml_entrypoint_is_a_thin_shared_pipeline_wrapper(self) -> None:
        text = (ROOT / "Jenkinsfile.ml").read_text()
        self.assertIn("checkout scm", text)
        self.assertIn("jenkins/pipeline/componentPipeline.groovy", text)
        self.assertIn("profile: 'ml'", text)

    def test_controller_image_is_pinned_and_has_no_docker_or_privileged_runtime(self) -> None:
        dockerfile = (ROOT / "infra/docker/jenkins-controller/Dockerfile").read_text()
        plugins = (ROOT / "infra/docker/jenkins-controller/plugins.txt").read_text()
        self.assertRegex(
            dockerfile,
            r"FROM jenkins/jenkins:[^\s]+@sha256:[0-9a-f]{64}",
        )
        self.assertRegex(
            dockerfile,
            r"FROM ghcr\.io/astral-sh/uv:[^\s]+@sha256:[0-9a-f]{64}",
        )
        self.assertNotIn("docker-ce", dockerfile)
        self.assertNotIn("/var/run/docker.sock", dockerfile)
        self.assertIn("USER jenkins", dockerfile)
        self.assertRegex(plugins, r"(?m)^junit:[^\s]+$")

    def test_jenkins_manifest_is_clusterip_only_non_privileged_and_bounded(self) -> None:
        manifest = ROOT / "infra/kubernetes/jenkins/jenkins.yaml"
        documents = [item for item in yaml.safe_load_all(manifest.read_text()) if item]
        by_kind = {}
        for document in documents:
            by_kind.setdefault(document["kind"], []).append(document)
        self.assertNotIn("Ingress", by_kind)
        self.assertEqual(by_kind["Service"][0]["spec"]["type"], "ClusterIP")
        self.assertFalse(by_kind["ServiceAccount"][0]["automountServiceAccountToken"])
        deployment = by_kind["Deployment"][0]
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        for container in [*pod.get("initContainers", []), *pod["containers"]]:
            security = container["securityContext"]
            self.assertFalse(security["allowPrivilegeEscalation"])
            self.assertTrue(security["runAsNonRoot"])
            self.assertTrue(security["capabilities"]["drop"] == ["ALL"])
            self.assertIn("requests", container["resources"])
            self.assertIn("limits", container["resources"])
        self.assertIn("PersistentVolumeClaim", by_kind)
        self.assertIn("ResourceQuota", by_kind)
        self.assertGreaterEqual(len(by_kind["NetworkPolicy"]), 2)
        runtime_policy = next(
            item for item in by_kind["NetworkPolicy"]
            if item["metadata"]["name"] == "jenkins-runtime-allow"
        )
        dns_blocks = [
            target["ipBlock"]["cidr"]
            for rule in runtime_policy["spec"]["egress"]
            for target in rule.get("to", [])
            if "ipBlock" in target and any(port["port"] == 53 for port in rule.get("ports", []))
        ]
        self.assertEqual(dns_blocks, ["34.118.224.10/32"])

    def test_runtime_template_contains_one_controller_and_immutable_source_contract(self) -> None:
        text = (ROOT / "infra/kubernetes/jenkins/jenkins.yaml").read_text()
        self.assertEqual(text.count("kind: Deployment"), 1)
        self.assertIn("__JENKINS_CONTROLLER_IMAGE__", text)
        self.assertIn("__JENKINS_SOURCE_IMAGE__", text)
        self.assertIn("EDAI2_SOURCE_SHA", text)
        self.assertIn("repository.bundle", text)
        self.assertNotRegex(text, re.compile(r"hostPath:|privileged:\s*true"))

    def test_casc_uses_supported_default_crumb_issuer_contract(self) -> None:
        text = (ROOT / "infra/kubernetes/jenkins/jenkins.yaml").read_text()
        self.assertIn("standard: {}", text)
        self.assertNotIn("excludeClientIPFromCrumb", text)

    def test_job_dsl_does_not_shadow_script_path_or_leak_groovy_interpolation(self) -> None:
        text = (ROOT / "infra/kubernetes/jenkins/jenkins.yaml").read_text()
        self.assertNotIn("scriptPath(scriptPath)", text)
        self.assertNotIn("${expectedSha}", text)
        self.assertIn("scriptPath(pipelineScriptPath)", text)

    def test_runtime_renderer_rejects_mutable_images_and_eliminates_tokens(self) -> None:
        template = (ROOT / "infra/kubernetes/jenkins/jenkins.yaml").read_text()
        values = {
            "__JENKINS_CONTROLLER_IMAGE__": "registry.example/jenkins@sha256:" + "a" * 64,
            "__JENKINS_SOURCE_IMAGE__": "registry.example/source@sha256:" + "b" * 64,
            "__SOURCE_SHA__": "1" * 40,
            "__BASE_SHA__": "2" * 40,
            "__ML_SHA__": "3" * 40,
            "__AGENT_SHA__": "4" * 40,
            "__DOCS_SHA__": "5" * 40,
            "__JENKINS_GSA__": "ewallet-jenkins@ewallet-data-ai-platform-hg26.iam.gserviceaccount.com",
        }
        output = render(template, values)
        self.assertNotRegex(output, r"__[A-Z0-9_]+__")
        with self.assertRaisesRegex(ValueError, "immutable Jenkins runtime value"):
            render(template, {**values, "__JENKINS_CONTROLLER_IMAGE__": "jenkins:latest"})


if __name__ == "__main__":
    unittest.main()
