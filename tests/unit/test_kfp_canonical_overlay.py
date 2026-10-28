from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY = REPO_ROOT / "infra/kubernetes/kfp"
UPSTREAM_DEV = (
    "github.com/kubeflow/pipelines/manifests/kustomize/env/dev?ref=2.17.3"
)
F9_IMAGE = (
    "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/"
    "ewallet-platform/f9-kfp-runtime:kfp-2.17.3-f9-v1"
)
MLFLOW_IMAGE = (
    "asia-southeast1-docker.pkg.dev/ewallet-data-ai-platform-hg26/"
    "ewallet-platform/mlflow:v3.17.1-gcs-postgres-1"
)
INVERSE_PROXY_RESOURCES = (
    "serviceaccount/proxy-agent-runner",
    "role.rbac.authorization.k8s.io/proxy-agent-runner",
    "rolebinding.rbac.authorization.k8s.io/proxy-agent-runner",
    "configmap/inverse-proxy-config",
    "deployment.apps/proxy-agent",
)
KFP_RELEASE_IMAGES = {
    "ghcr.io/kubeflow/kfp-api-server": (
        "sha256:1e03770233cadc146df0b83a82603b93f32fc3b57bd6dcb29375e2e8d28eb605"
    ),
    "ghcr.io/kubeflow/kfp-cache-deployer": (
        "sha256:97784329a5af2274097d13095ddc71f8e0c72ccad76fc4e67d40a06eb14417e7"
    ),
    "ghcr.io/kubeflow/kfp-cache-server": (
        "sha256:09f16342f896b05714933661fb65bf49b4faa5f07f2414002f1e015bc8a608e0"
    ),
    "ghcr.io/kubeflow/kfp-frontend": (
        "sha256:06d5481f404f89647ac5ee958f6665446014fdd78e6c1b195a1687ff1badf1d8"
    ),
    "ghcr.io/kubeflow/kfp-metadata-envoy": (
        "sha256:5cc007cfa3ec2f3f9442430a66d4e65239760cae2ff66dfe576e1b2f685e0cfb"
    ),
    "ghcr.io/kubeflow/kfp-metadata-writer": (
        "sha256:fbcf3704239ee92135a7f7c82ba177c65977ab1da7044b4cc02b6de5ace698bb"
    ),
    "ghcr.io/kubeflow/kfp-persistence-agent": (
        "sha256:c0b417e60009d058c2d342ab33424e10cf57121d9b771cca9c09dda32475c03e"
    ),
    "ghcr.io/kubeflow/kfp-scheduled-workflow-controller": (
        "sha256:e2e5b80707955f3295e8a24c340c838f81398b988a9d73318918bea5ffdde7d8"
    ),
    "ghcr.io/kubeflow/kfp-viewer-crd-controller": (
        "sha256:2b6e0e92b99f709c11a484fbf4163fc519003d22c4f9d040f3fba862f5b69780"
    ),
    "ghcr.io/kubeflow/kfp-visualization-server": (
        "sha256:d143f368b428c92cccf2b33c4c1b0274f8b1a14cf04f25659fe0f091e96cef6f"
    ),
    "ghcr.io/kubeflow/kfp-driver": (
        "sha256:4461e2f63739a80810405fdf76bd4727ea68561a54b51e4bbf869b12d7a8d163"
    ),
    "ghcr.io/kubeflow/kfp-launcher": (
        "sha256:4f1c3cff2725413cbb903f623aed074c52e3c70c355501c87b29db044c83fcd2"
    ),
}


def _render_overlay() -> list[dict]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(OVERLAY)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [resource for resource in yaml.safe_load_all(result.stdout) if resource]


def _all_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _all_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _all_strings(child)]
    return []


class KfpCanonicalOverlayTest(unittest.TestCase):
    def test_overlay_excludes_inverse_proxy_and_preserves_internal_control_plane(
        self,
    ) -> None:
        kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
        self.assertIn(UPSTREAM_DEV, kustomization["resources"])

        resources = _render_overlay()
        self.assertEqual(78, len(resources))
        identities = {
            (resource["kind"], resource.get("metadata", {}).get("name"))
            for resource in resources
        }

        excluded = {
            ("ServiceAccount", "proxy-agent-runner"),
            ("Role", "proxy-agent-runner"),
            ("RoleBinding", "proxy-agent-runner"),
            ("ConfigMap", "inverse-proxy-config"),
            ("Deployment", "proxy-agent"),
        }
        self.assertTrue(excluded.isdisjoint(identities))
        self.assertIn(("ServiceAccount", "pipeline-runner"), identities)
        for required in (
            ("Deployment", "ml-pipeline"),
            ("Deployment", "ml-pipeline-ui"),
            ("Service", "ml-pipeline"),
            ("Service", "ml-pipeline-ui"),
            ("Deployment", "workflow-controller"),
        ):
            self.assertIn(required, identities)

        for resource in resources:
            pod_spec = (
                resource.get("spec", {}).get("template", {}).get("spec", {})
            )
            self.assertIsNot(True, pod_spec.get("hostNetwork"))
            self.assertNotIn(resource["kind"], {"Ingress", "Gateway", "HTTPRoute"})
            if resource["kind"] == "Service":
                self.assertNotEqual("LoadBalancer", resource["spec"].get("type"))

    def test_render_pins_the_complete_kfp_2173_runtime_by_digest(self) -> None:
        resources = _render_overlay()
        expected_references = {
            f"{repository}@{digest}"
            for repository, digest in KFP_RELEASE_IMAGES.items()
        }
        rendered_references = {
            value
            for resource in resources
            for value in _all_strings(resource)
            if value.startswith("ghcr.io/kubeflow/kfp-")
        }

        self.assertEqual(expected_references, rendered_references)
        self.assertFalse(
            any(
                reference.endswith(":master") or reference.endswith(":latest")
                for reference in rendered_references
            )
        )

        api_server = next(
            resource
            for resource in resources
            if resource["kind"] == "Deployment"
            and resource["metadata"]["name"] == "ml-pipeline"
        )
        container = next(
            item
            for item in api_server["spec"]["template"]["spec"]["containers"]
            if item["name"] == "ml-pipeline-api-server"
        )
        for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertEqual(
                "/apis/v1beta1/healthz",
                container[probe_name]["httpGet"]["path"],
            )

    def test_deploy_dry_run_plans_only_exact_inverse_proxy_reconciliation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            invocation_log = temporary_path / "kubectl-invoked"
            fake_kubectl = temporary_path / "kubectl"
            fake_kubectl.write_text(
                "#!/usr/bin/env bash\n"
                f"touch {invocation_log}\n"
                "exit 99\n"
            )
            fake_kubectl.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{temporary_path}:{os.environ['PATH']}",
                "GCP_PROJECT_ID": "ewallet-data-ai-platform-hg26",
                "GCP_REGION": "asia-southeast1",
                "F9_RUNTIME_IMAGE": F9_IMAGE,
                "MLFLOW_IMAGE": MLFLOW_IMAGE,
                "KFP_VERSION": "2.17.3",
                "KFP_NAMESPACE": "kubeflow",
            }
            result = subprocess.run(
                [str(REPO_ROOT / "scripts/mlops/deploy_kfp.sh")],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn(f"kubectl apply -k {OVERLAY}", result.stdout)
            for resource in INVERSE_PROXY_RESOURCES:
                self.assertIn(
                    f"kubectl -n kubeflow delete {resource} --ignore-not-found",
                    result.stdout,
                )
                self.assertIn(
                    f"kubectl -n kubeflow get {resource} --ignore-not-found "
                    "-o name",
                    result.stdout,
                )
            self.assertNotIn("delete namespace", result.stdout)
            self.assertNotIn("--all", result.stdout)
            self.assertNotIn("--selector", result.stdout)
            self.assertNotIn("--prune", result.stdout)
            self.assertFalse(invocation_log.exists())

    def test_execute_fails_closed_when_any_inverse_proxy_resource_remains(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            invocation_log = temporary_path / "kubectl-invocations"
            fake_kubectl = temporary_path / "kubectl"
            fake_kubectl.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' \"$*\" >> {invocation_log}\n"
                "if [[ \"$*\" == *\"get "
                "rolebinding.rbac.authorization.k8s.io/proxy-agent-runner "
                "--ignore-not-found -o name\"* ]]; then\n"
                "  echo rolebinding.rbac.authorization.k8s.io/proxy-agent-runner\n"
                "fi\n"
                "exit 0\n"
            )
            fake_kubectl.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{temporary_path}:{os.environ['PATH']}",
                "GCP_PROJECT_ID": "ewallet-data-ai-platform-hg26",
                "GCP_REGION": "asia-southeast1",
                "F9_RUNTIME_IMAGE": F9_IMAGE,
                "MLFLOW_IMAGE": MLFLOW_IMAGE,
                "KFP_VERSION": "2.17.3",
                "KFP_NAMESPACE": "kubeflow",
            }

            result = subprocess.run(
                [str(REPO_ROOT / "scripts/mlops/deploy_kfp.sh"), "--execute"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn(
                "inverse-proxy resources remain after bounded deletion: "
                "rolebinding.rbac.authorization.k8s.io/proxy-agent-runner",
                result.stderr,
            )
            invocations = invocation_log.read_text().splitlines()
            for resource in INVERSE_PROXY_RESOURCES:
                self.assertIn(
                    f"-n kubeflow delete {resource} --ignore-not-found",
                    invocations,
                )
                self.assertIn(
                    f"-n kubeflow get {resource} --ignore-not-found -o name",
                    invocations,
                )


if __name__ == "__main__":
    unittest.main()
