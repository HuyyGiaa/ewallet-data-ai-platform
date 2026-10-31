from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_platform.ml.modeling.config import ModelConfig
from ai_platform.ml.modeling.data import load_dataset, prepare_modeling_splits
from ai_platform.ml.modeling.experiment import ExperimentRunner
from ai_platform.ml.tracking.config import CandidateConfig
from tests.test_fraud_modeling import write_large_modeling_dataset


class FraudExperimentTest(unittest.TestCase):
    def test_selection_eligibility_from_config_controls_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_large_modeling_dataset(root, rows=32)
            splits = prepare_modeling_splits(load_dataset(root))
            candidates = (
                CandidateConfig(
                    ModelConfig(
                        "ineligible_random_forest",
                        "random_forest",
                        "full",
                        {
                            "n_estimators": 10,
                            "max_depth": 4,
                            "min_samples_leaf": 2,
                            "n_jobs": 1,
                        },
                    ),
                    selection_eligible=False,
                ),
                CandidateConfig(
                    ModelConfig(
                        "eligible_dummy", "dummy", "full", {"strategy": "prior"}
                    ),
                    selection_eligible=True,
                ),
            )

            result = ExperimentRunner(candidates, seed=42).run(splits)

        self.assertEqual("eligible_dummy", result.winner.name)
        self.assertTrue(all(candidate.validation_report for candidate in result.candidates))
        self.assertEqual(
            ["eligible_dummy"],
            [candidate.name for candidate in result.candidates if candidate.tested],
        )

    def test_candidates_share_data_and_only_winner_is_tested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_large_modeling_dataset(root, rows=40)
            splits = prepare_modeling_splits(load_dataset(root))
            candidates = (
                CandidateConfig(
                    ModelConfig("dummy", "dummy", "full", {"strategy": "prior"}),
                    selection_eligible=False,
                ),
                CandidateConfig(
                    ModelConfig(
                        "logistic",
                        "logistic_regression",
                        "full",
                        {"C": 1.0, "max_iter": 100},
                    ),
                    selection_eligible=True,
                ),
                CandidateConfig(
                    ModelConfig(
                        "random_forest",
                        "random_forest",
                        "full",
                        {
                            "n_estimators": 20,
                            "max_depth": 5,
                            "min_samples_leaf": 2,
                            "n_jobs": 1,
                        },
                    ),
                    selection_eligible=True,
                ),
                CandidateConfig(
                    ModelConfig(
                        "hist_gradient_boosting",
                        "hist_gradient_boosting",
                        "full",
                        {
                            "max_iter": 20,
                            "max_leaf_nodes": 15,
                            "learning_rate": 0.1,
                            "l2_regularization": 0.1,
                        },
                    ),
                    selection_eligible=True,
                ),
            )

            result = ExperimentRunner(candidates, seed=42).run(splits)

        self.assertEqual(4, len(result.candidates))
        self.assertNotEqual("dummy", result.winner.name)
        self.assertIsNotNone(result.test_report)
        self.assertEqual(result.winner.name, result.test_report["model"])
        self.assertTrue(all(candidate.validation_report for candidate in result.candidates))
        self.assertEqual(
            [result.winner.name],
            [candidate.name for candidate in result.candidates if candidate.tested],
        )

    def test_experiment_is_deterministic_for_same_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_large_modeling_dataset(root, rows=32)
            splits = prepare_modeling_splits(load_dataset(root))
            candidates = (
                CandidateConfig(
                    ModelConfig(
                        "random_forest",
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
            )

            first = ExperimentRunner(candidates, seed=42).run(splits)
            second = ExperimentRunner(candidates, seed=42).run(splits)

        self.assertEqual(
            first.winner.validation_report["metrics"],
            second.winner.validation_report["metrics"],
        )
        self.assertEqual(first.winner.threshold, second.winner.threshold)
        self.assertEqual(first.test_report["metrics"], second.test_report["metrics"])


if __name__ == "__main__":
    unittest.main()
