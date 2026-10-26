from __future__ import annotations

import unittest

from ai_platform.ml.pipelines.settings import KfpDeploymentSettings
from ai_platform.ml.tracking.settings import MlflowDeploymentSettings


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


if __name__ == "__main__":
    unittest.main()
