from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import mlflow
import pandas as pd
import yaml

from ai_platform.ml.modeling.config import ModelConfig
from ai_platform.ml.modeling.data import load_dataset, prepare_modeling_splits
from ai_platform.ml.modeling.experiment import ExperimentRunner
from ai_platform.ml.tracking.config import CandidateConfig, load_experiment_config
from ai_platform.ml.tracking.mlflow_tracker import MlflowTracker
from ai_platform.ml.tracking.model import FraudPyFuncModel
from tests.test_fraud_modeling import write_large_modeling_dataset


class MlflowEnvironmentContractTest(unittest.TestCase):
    def test_environment_and_direct_dependencies_are_versioned(self) -> None:
        environment = yaml.safe_load(
            Path("ai_platform/ml/environment.yml").read_text(encoding="utf-8")
        )
        requirements = Path("ai_platform/ml/requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertEqual("ewallet-ml", environment["name"])
        for dependency in (
            "mlflow==3.17.1",
            "pandas==2.3.3",
            "pyarrow==25.0.1",
            "scikit-learn==1.7.2",
            "psycopg2-binary==2.9.13",
            "google-cloud-storage==3.4.1",
            "joblib==1.6.0",
        ):
            self.assertIn(dependency, requirements)


class MlflowConfigTest(unittest.TestCase):
    def test_checked_in_config_loads_fixed_four_candidate_matrix(self) -> None:
        config = load_experiment_config(
            Path("ai_platform/ml/config/f9_mlflow_experiment.yaml"),
            repo_root=Path.cwd(),
            environment={
                "F9_DATASET_PATH": "/tmp/f9-data",
                "F9_OUTPUT_PATH": "/tmp/f9-output",
                "F9_TRAINING_SNAPSHOT_URI": (
                    "gs://bucket/training/canonical-100gb-v1/"
                    "canonical-training-1"
                ),
                "DATASET_VERSION": "canonical-100gb-v1",
                "SILVER_RUN_ID": "canonical-silver-1",
                "FEATURE_RUN_ID": "canonical-features-1",
                "SNAPSHOT_ID": "canonical-training-1",
                "MLFLOW_TRACKING_URI": "http://mlflow.ml-platform.svc:5000",
            },
        )
        self.assertEqual(42, config.seed)
        self.assertEqual("winner_only", config.tracking.registry_mode)
        self.assertEqual(
            [
                "dummy_baseline",
                "logistic_regression",
                "random_forest",
                "hist_gradient_boosting",
            ],
            [candidate.model.name for candidate in config.candidates],
        )
        self.assertFalse(config.candidates[0].selection_eligible)
        self.assertTrue(all(c.selection_eligible for c in config.candidates[1:]))

    def test_config_rejects_duplicate_names_and_unknown_registry_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "f9.yaml"
            path.write_text(
                """
dataset_path: data
output_path: output
seed: 42
selection: {primary_metric: pr_auc, tie_breaker_metric: f1}
snapshot:
  training_snapshot_uri: gs://bucket/training/snapshot-1
  dataset_version: canonical-100gb-v1
  silver_run_id: canonical-silver-1
  feature_run_id: canonical-features-1
  snapshot_id: canonical-training-1
tracking:
  uri: http://mlflow.ml-platform.svc:5000
  experiment_name: fraud
  registered_model_name: fraud
  registry_mode: invalid
candidates:
  - {name: rf, estimator: random_forest, strategy: full, selection_eligible: true, params: {n_estimators: 2}}
  - {name: rf, estimator: random_forest, strategy: full, selection_eligible: true, params: {n_estimators: 2}}
""".lstrip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "registry_mode"):
                load_experiment_config(path, repo_root=Path(temporary_directory))


class FraudPyFuncModelTest(unittest.TestCase):
    def test_predict_returns_probability_and_thresholded_label(self) -> None:
        class FixedPipeline:
            def predict_proba(self, frame):
                import numpy as np

                return np.asarray([[0.8, 0.2], [0.1, 0.9]])

        prediction = FraudPyFuncModel(FixedPipeline(), threshold=0.7).predict(
            None, pd.DataFrame({"amount": [1.0, 2.0]})
        )

        self.assertEqual(
            ["fraud_probability", "fraud_prediction"], list(prediction.columns)
        )
        self.assertEqual([0.2, 0.9], prediction["fraud_probability"].tolist())
        self.assertEqual([0, 1], prediction["fraud_prediction"].tolist())


class MlflowManualTrackingTest(unittest.TestCase):
    def test_tracker_persists_nested_candidates_and_reloadable_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dataset = root / "dataset"
            write_large_modeling_dataset(dataset, rows=32)
            splits = prepare_modeling_splits(load_dataset(dataset))
            experiment = ExperimentRunner(
                (
                    CandidateConfig(
                        ModelConfig(
                            "dummy", "dummy", "full", {"strategy": "prior"}
                        ),
                        selection_eligible=False,
                    ),
                    CandidateConfig(
                        ModelConfig(
                            "rf",
                            "random_forest",
                            "full",
                            {
                                "n_estimators": 10,
                                "max_depth": 4,
                                "min_samples_leaf": 2,
                                "n_jobs": 1,
                            },
                        ),
                        selection_eligible=True,
                    ),
                ),
                seed=42,
            ).run(splits)
            tracking_uri = f"sqlite:///{root / 'mlflow.db'}"
            client = mlflow.MlflowClient(tracking_uri=tracking_uri)
            client.create_experiment(
                "fraud-test", artifact_location=(root / "artifacts").as_uri()
            )
            tracker = MlflowTracker(
                tracking_uri=tracking_uri,
                experiment_name="fraud-test",
                registered_model_name="fraud-test-model",
                registry_mode="manual",
            )

            published = tracker.publish(
                experiment,
                input_example=splits["validation"].X.iloc[:2],
                provenance={
                    "dataset_version": "unit-test-v1",
                    "silver_run_id": "silver-test-1",
                    "feature_run_id": "features-test-1",
                    "snapshot_id": "snapshot-test-1",
                    "training_snapshot_uri": "file:///tmp/snapshot-test-1",
                    "snapshot_manifest_sha256": "a" * 64,
                    "snapshot_row_count": 32,
                    "feature_count": 41,
                    "feature_schema_sha256": "b" * 64,
                    "git_commit_sha": "c" * 40,
                    "random_seed": 42,
                },
            )
            loaded = mlflow.pyfunc.load_model(published.winner_model_uri)
            predictions = loaded.predict(splits["validation"].X.iloc[:2])
            client = mlflow.MlflowClient(tracking_uri=tracking_uri)
            children = client.search_runs(
                [published.experiment_id],
                filter_string=(
                    f"tags.mlflow.parentRunId = '{published.parent_run_id}'"
                ),
            )
            winner_only = MlflowTracker(
                tracking_uri=tracking_uri,
                experiment_name="fraud-test",
                registered_model_name="fraud-test-model",
                registry_mode="winner_only",
            ).publish(
                experiment,
                input_example=splits["validation"].X.iloc[:2],
                provenance={
                    "dataset_version": "unit-test-v1",
                    "silver_run_id": "silver-test-1",
                    "feature_run_id": "features-test-1",
                    "snapshot_id": "snapshot-test-1",
                    "training_snapshot_uri": "file:///tmp/snapshot-test-1",
                    "snapshot_manifest_sha256": "a" * 64,
                    "snapshot_row_count": 32,
                    "feature_count": 41,
                    "feature_schema_sha256": "b" * 64,
                    "git_commit_sha": "c" * 40,
                    "random_seed": 42,
                },
            )
            registered = client.get_model_version(
                "fraud-test-model", winner_only.registered_model_version
            )
            winner_children = client.search_runs(
                [winner_only.experiment_id],
                filter_string=(
                    f"tags.mlflow.parentRunId = '{winner_only.parent_run_id}'"
                ),
            )

        self.assertEqual(2, len(children))
        self.assertEqual(2, len(predictions))
        self.assertEqual(
            ["fraud_probability", "fraud_prediction"], list(predictions.columns)
        )
        self.assertIsNone(published.registered_model_version)
        self.assertEqual(winner_only.parent_run_id, registered.run_id)
        self.assertEqual("snapshot-test-1", registered.tags["snapshot_id"])
        self.assertEqual(
            1,
            sum("test_pr_auc" in run.data.metrics for run in winner_children),
        )
        for run in winner_children:
            self.assertEqual("unit-test-v1", run.data.params["dataset_version"])
            self.assertEqual("silver-test-1", run.data.params["silver_run_id"])
            self.assertEqual("features-test-1", run.data.params["feature_run_id"])
            self.assertEqual("snapshot-test-1", run.data.params["snapshot_id"])
            self.assertEqual("a" * 64, run.data.params["snapshot_manifest_sha256"])


if __name__ == "__main__":
    unittest.main()
