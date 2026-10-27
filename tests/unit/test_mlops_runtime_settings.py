from __future__ import annotations

import re
import unittest
from pathlib import Path

from ai_platform.ml.pipelines.settings import KfpDeploymentSettings
from ai_platform.ml.tracking.settings import MlflowDeploymentSettings


REPO_ROOT = Path(__file__).resolve().parents[2]


class MlflowDeploymentSettingsTest(unittest.TestCase):
    def test_cloud_settings_require_internal_postgres_gcs_and_pinned_image(self) -> None:
        settings = MlflowDeploymentSettings.from_environment(
            {
                "GCP_PROJECT_ID": "project-1",
                "GCP_REGION": "asia-southeast1",
                "MLFLOW_TRACKING_URI": "http://mlflow.ml-platform.svc:5000",
                "MLFLOW_BACKEND_STORE_URI": (
                    "postgresql+psycopg2://mlflow:secret@postgres:5432/mlflow"
                ),
                "MLFLOW_ARTIFACT_ROOT": "gs://bucket/mlflow/artifacts",
                "MLFLOW_IMAGE": "registry/mlflow:v3.17.1-gcs-postgres-1",
                "MLFLOW_EXPERIMENT_NAME": "fraud",
                "MLFLOW_REGISTERED_MODEL_NAME": "fraud-model",
            }
        )
        self.assertEqual("gs://bucket/mlflow/artifacts", settings.artifact_root)

    def test_localhost_and_latest_fail_closed(self) -> None:
        base = {
            "GCP_PROJECT_ID": "project-1",
            "GCP_REGION": "asia-southeast1",
            "MLFLOW_TRACKING_URI": "http://localhost:5000",
            "MLFLOW_BACKEND_STORE_URI": "postgresql+psycopg2://u:p@db/mlflow",
            "MLFLOW_ARTIFACT_ROOT": "gs://bucket/artifacts",
            "MLFLOW_IMAGE": "registry/mlflow:latest",
            "MLFLOW_EXPERIMENT_NAME": "fraud",
            "MLFLOW_REGISTERED_MODEL_NAME": "fraud-model",
        }
        with self.assertRaises(ValueError):
            MlflowDeploymentSettings.from_environment(base)
        base["MLFLOW_TRACKING_URI"] = "http://mlflow.ml-platform.svc:5000"
        with self.assertRaisesRegex(ValueError, "non-latest"):
            MlflowDeploymentSettings.from_environment(base)


class KfpDeploymentSettingsTest(unittest.TestCase):
    def test_kfp_requires_exact_version_and_explicit_identity(self) -> None:
        settings = KfpDeploymentSettings.from_environment(
            {
                "GCP_PROJECT_ID": "project-1",
                "GCP_REGION": "asia-southeast1",
                "GKE_CLUSTER": "cluster-1",
                "KFP_NAMESPACE": "kubeflow",
                "KFP_RUNNER_KSA": "pipeline-runner",
                "TRAINING_GSA": "training@project-1.iam.gserviceaccount.com",
                "F9_RUNTIME_IMAGE": "registry/f9:kfp-2.17.3-f9-v1",
                "KFP_VERSION": "2.17.3",
            }
        )
        self.assertEqual("2.17.3", settings.version)


class GkeDnsEndpointSourceContractTest(unittest.TestCase):
    def test_gke_connection_is_dns_only_and_fail_closed(self) -> None:
        connect = (REPO_ROOT / "scripts/mlops/connect_cluster.sh").read_text()
        preflight = (REPO_ROOT / "scripts/mlops/preflight.sh").read_text()
        common = (REPO_ROOT / "scripts/mlops/lib/common.sh").read_text()

        self.assertRegex(
            connect,
            re.compile(
                r"gcloud container clusters get-credentials.*?--dns-endpoint",
                re.DOTALL,
            ),
        )
        self.assertNotIn("--internal-ip", connect)
        self.assertIn("require_gke_auth_plugin", connect)
        self.assertIn("require_gke_auth_plugin", preflight)
        self.assertIn("controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint", common)
        self.assertIn("controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic", common)
        self.assertIn("assert_active_gke_dns_server", preflight)
        self.assertIn(".gke.goog", common)
        self.assertNotIn("--enable-k8s-tokens-via-dns", connect + preflight + common)
        self.assertNotIn("--enable-k8s-certs-via-dns", connect + preflight + common)


if __name__ == "__main__":
    unittest.main()
