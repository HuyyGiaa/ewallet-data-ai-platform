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
        ):
            self.assertIn(dependency, requirements)


class MlflowConfigTest(unittest.TestCase):
    def test_checked_in_config_loads_fixed_four_candidate_matrix(self) -> None:
        config = load_experiment_config(
            Path("ai_platform/ml/config/f9_mlflow_experiment.yaml"),
            repo_root=Path.cwd(),
        )
        self.assertEqual(42, config.seed)
        self.assertEqual("manual", config.tracking.registry_mode)
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
tracking:
  uri: http://127.0.0.1:5000
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
                provenance={"dataset": "unit-test", "seed": 42},
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

        self.assertEqual(2, len(children))
        self.assertEqual(2, len(predictions))
        self.assertEqual(
            ["fraud_probability", "fraud_prediction"], list(predictions.columns)
        )
        self.assertIsNone(published.registered_model_version)


if __name__ == "__main__":
    unittest.main()
