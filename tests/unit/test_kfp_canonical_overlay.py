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


class KfpCanonicalOverlayTest(unittest.TestCase):
    def test_overlay_excludes_inverse_proxy_and_preserves_internal_control_plane(
        self,
    ) -> None:
        kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
        self.assertIn(UPSTREAM_DEV, kustomization["resources"])

        result = subprocess.run(
            ["kubectl", "kustomize", str(OVERLAY)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        resources = [
            resource
            for resource in yaml.safe_load_all(result.stdout)
            if resource is not None
        ]
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
