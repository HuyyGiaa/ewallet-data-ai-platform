from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import joblib
import yaml
from sklearn.ensemble import RandomForestClassifier
from sklearn.pipeline import Pipeline

from ai_platform.ml.modeling.config import load_config
from ai_platform.ml.modeling.artifacts import ArtifactPayload, publish_artifacts
from ai_platform.ml.modeling.data import load_dataset, prepare_modeling_splits
from ai_platform.ml.modeling.evaluation import (
    choose_threshold,
    evaluate_scores,
    scenario_report,
)
from ai_platform.ml.modeling.features import build_feature_transformer
from ai_platform.ml.training.contract import (
    FORBIDDEN_MODEL_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    OUTPUT_COLUMNS,
)


def modeling_frame(split: str, prefix: str, labels=(0, 1, 0)) -> pd.DataFrame:
    rows = len(labels)
    values = {
        column: [float(index + 1) for index in range(rows)]
        for column in MODEL_FEATURE_COLUMNS
    }
    values.update(
        {
            "transaction_id": [f"{prefix}-tx-{index}" for index in range(rows)],
            "event_timestamp": pd.date_range(
                "2026-01-01", periods=rows, freq="min", tz="UTC"
            ),
            "user_id": [f"{prefix}-u-{index}" for index in range(rows)],
            "account_id": [f"{prefix}-a-{index}" for index in range(rows)],
            "device_id": [f"{prefix}-d-{index}" for index in range(rows)],
            "merchant_id": [f"{prefix}-m-{index}" for index in range(rows)],
            "label": list(labels),
            "fraud_type": [None, "velocity", None][:rows],
            "split": [split] * rows,
            "type": ["payment", "transfer", "withdraw"][:rows],
            "channel": ["app", "web", "atm"][:rows],
            "merchant_present": [True] * rows,
        }
    )
    return pd.DataFrame(values)[list(OUTPUT_COLUMNS)]


def write_modeling_dataset(root: Path) -> None:
    for split in ("train", "validation", "test"):
        path = root / f"split={split}"
        path.mkdir(parents=True)
        modeling_frame(split, split).to_parquet(path / "part-00000.parquet")


def overwrite_split(root: Path, split: str, frame: pd.DataFrame) -> None:
    path = root / f"split={split}"
    path.mkdir(parents=True, exist_ok=True)
    for part in path.glob("*.parquet"):
        part.unlink()
    frame.to_parquet(path / "part-00000.parquet")


def feature_frame(rows: int = 60) -> tuple[pd.DataFrame, pd.Series]:
    y = pd.Series(([0, 1] * (rows // 2)) + ([0] if rows % 2 else []))
    index = np.arange(rows, dtype=float)
    values = {}
    for position, column in enumerate(MODEL_FEATURE_COLUMNS):
        if column not in {"type", "channel"}:
            values[column] = index * (position + 1) / max(rows, 1)
    values["amount"] = y.to_numpy(dtype=float) * 100.0 + index * 0.001
    values["merchant_present"] = (index % 3 != 0)
    values["type"] = np.where(index % 2 == 0, "payment", "transfer")
    values["channel"] = np.where(index % 3 == 0, "app", "web")
    return pd.DataFrame(values)[list(MODEL_FEATURE_COLUMNS)], y


def write_large_modeling_dataset(root: Path, rows: int = 60) -> None:
    scenarios = ("velocity", "amount_anomaly", "account_takeover", "merchant_burst")
    for split in ("train", "validation", "test"):
        X, y = feature_frame(rows)
        fraud_types = []
        scenario_index = 0
        for label in y:
            if label:
                fraud_types.append(scenarios[scenario_index % len(scenarios)])
                scenario_index += 1
            else:
                fraud_types.append(None)
        metadata = pd.DataFrame(
            {
                "transaction_id": [f"{split}-tx-{index}" for index in range(rows)],
                "event_timestamp": pd.date_range(
                    "2026-01-01", periods=rows, freq="s", tz="UTC"
                ),
                "user_id": [f"{split}-u-{index}" for index in range(rows)],
                "account_id": [f"{split}-a-{index}" for index in range(rows)],
                "device_id": [f"{split}-d-{index}" for index in range(rows)],
                "merchant_id": [f"{split}-m-{index}" for index in range(rows)],
                "label": y,
                "fraud_type": fraud_types,
                "split": split,
            }
        )
        frame = pd.concat([metadata, X], axis=1)[list(OUTPUT_COLUMNS)]
        overwrite_split(root, split, frame)
    (root / "manifest.json").write_text(
        '{"dataset": "synthetic-f8-test"}\n', encoding="utf-8"
    )


class FraudModelingSingleModelContractTest(unittest.TestCase):
    def test_checked_in_config_declares_only_random_forest_full(self):
        raw = yaml.safe_load(
            Path("ai_platform/ml/config/f8_baseline.yaml").read_text(
                encoding="utf-8"
            )
        )

        self.assertNotIn("candidates", raw)
        self.assertNotIn("pca_variance", raw)
        self.assertNotIn("selector_c", raw)
        self.assertEqual(
            {
                "name": "random_forest_full",
                "estimator": "random_forest",
                "strategy": "full",
                "params": {
                    "n_estimators": 200,
                    "max_depth": 12,
                    "min_samples_leaf": 2,
                    "n_jobs": 1,
                },
            },
            raw["model"],
        )


class FraudModelingConfigTest(unittest.TestCase):
    def test_load_config_resolves_paths_and_single_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "f8.yaml"
            config_path.write_text(
                """
dataset_path: data/trainable
output_path: runtime/f8
seed: 42
model:
  name: random_forest_full
  estimator: random_forest
  strategy: full
  params: {n_estimators: 50, max_depth: 8, min_samples_leaf: 2, n_jobs: 1}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path, repo_root=root)

            self.assertEqual(root / "data/trainable", config.dataset_path)
            self.assertEqual(root / "runtime/f8", config.output_path)
            self.assertEqual(42, config.seed)
            self.assertEqual("random_forest_full", config.model.name)
            self.assertEqual("random_forest", config.model.estimator)
            self.assertEqual("full", config.model.strategy)
            self.assertEqual(50, config.model.params["n_estimators"])

    def test_load_config_rejects_non_random_forest_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "f8.yaml"
            config_path.write_text(
                """
dataset_path: data/trainable
output_path: runtime/f8
seed: 42
model:
  name: logistic_full
  estimator: logistic
  strategy: full
  params: {C: 1.0}
""".lstrip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "random_forest"):
                load_config(config_path, repo_root=root)


class FraudModelingDataTest(unittest.TestCase):
    def test_loader_returns_exact_temporal_partitions_and_leakage_safe_X(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_modeling_dataset(root)

            dataset = load_dataset(root)
            splits = prepare_modeling_splits(dataset)

            self.assertEqual(3, len(dataset.train))
            self.assertEqual(3, len(dataset.validation))
            self.assertEqual(3, len(dataset.test))
            self.assertEqual(list(MODEL_FEATURE_COLUMNS), list(splits["train"].X))
            self.assertEqual([0, 1, 0], splits["train"].y.tolist())
            self.assertIn("fraud_type", splits["train"].diagnostics)
            self.assertIn("transaction_id", splits["train"].diagnostics)
            self.assertFalse(
                FORBIDDEN_MODEL_COLUMNS.intersection(splits["train"].X.columns)
            )

    def test_loader_rejects_missing_contract_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_modeling_dataset(root)
            overwrite_split(
                root, "train", modeling_frame("train", "train").drop(columns="amount")
            )
            with self.assertRaisesRegex(ValueError, "columns"):
                load_dataset(root)

    def test_loader_rejects_invalid_or_null_label(self):
        for bad_label in (2, None):
            with self.subTest(label=bad_label), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                write_modeling_dataset(root)
                frame = modeling_frame("train", "train")
                frame.loc[0, "label"] = bad_label
                overwrite_split(root, "train", frame)
                with self.assertRaisesRegex(ValueError, "label"):
                    load_dataset(root)

    def test_loader_rejects_empty_partition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_modeling_dataset(root)
            overwrite_split(root, "test", modeling_frame("test", "test").iloc[:0])
            with self.assertRaisesRegex(ValueError, "empty"):
                load_dataset(root)

    def test_loader_rejects_stored_split_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_modeling_dataset(root)
            frame = modeling_frame("validation", "validation")
            frame.loc[0, "split"] = "train"
            overwrite_split(root, "validation", frame)
            with self.assertRaisesRegex(ValueError, "split"):
                load_dataset(root)

    def test_loader_rejects_duplicate_transaction_inside_split(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_modeling_dataset(root)
            frame = modeling_frame("train", "train")
            frame.loc[1, "transaction_id"] = frame.loc[0, "transaction_id"]
            overwrite_split(root, "train", frame)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_dataset(root)

    def test_loader_rejects_cross_split_transaction_overlap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_modeling_dataset(root)
            frame = modeling_frame("validation", "validation")
            frame.loc[0, "transaction_id"] = "train-tx-0"
            overwrite_split(root, "validation", frame)
            with self.assertRaisesRegex(ValueError, "overlap"):
                load_dataset(root)

    def test_loader_rejects_split_with_one_target_class(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_modeling_dataset(root)
            overwrite_split(
                root,
                "validation",
                modeling_frame("validation", "validation", labels=(0, 0, 0)),
            )
            with self.assertRaisesRegex(ValueError, "classes"):
                load_dataset(root)


class FraudModelingFeatureTest(unittest.TestCase):
    def test_preprocessor_adds_missing_indicators_and_handles_unknown_categories(self):
        train = modeling_frame("train", "train")[list(MODEL_FEATURE_COLUMNS)]
        train.loc[0, "user_behavior__user_failed_rate_24h"] = np.nan
        validation = train.iloc[[0]].copy()
        validation.loc[:, "type"] = "cash_out"
        validation.loc[:, "channel"] = "new_channel"
        transformer = build_feature_transformer()
        transformed_train = transformer.fit_transform(train, pd.Series([0, 1, 0]))
        transformed_validation = transformer.transform(validation)

        self.assertEqual(3, transformed_train.shape[0])
        self.assertEqual(1, transformed_validation.shape[0])
        self.assertGreater(transformed_train.shape[1], len(MODEL_FEATURE_COLUMNS))
        self.assertTrue(np.isfinite(transformed_train).all())
        self.assertTrue(np.isfinite(transformed_validation).all())



class FraudModelingModelTest(unittest.TestCase):
    def test_build_model_returns_configured_deterministic_random_forest(self):
        from ai_platform.ml.modeling.models import build_model

        config = load_config(
            Path("ai_platform/ml/config/f8_baseline.yaml"), repo_root=Path.cwd()
        )
        pipeline = build_model(config.model, seed=config.seed)

        self.assertIsInstance(pipeline, Pipeline)
        estimator = pipeline.named_steps["estimator"]
        self.assertIsInstance(estimator, RandomForestClassifier)
        self.assertEqual(42, estimator.random_state)
        self.assertEqual(200, estimator.n_estimators)
        self.assertEqual(12, estimator.max_depth)


class FraudModelingEvaluationTest(unittest.TestCase):
    def test_threshold_maximizes_validation_f1(self):
        result = choose_threshold(
            np.array([0, 1, 1, 0]), np.array([0.1, 0.4, 0.8, 0.3])
        )
        self.assertEqual(0.4, result.threshold)
        self.assertEqual(1.0, result.f1)
        self.assertEqual(1.0, result.recall)

    def test_threshold_tie_prefers_recall_then_lower_threshold(self):
        result = choose_threshold(
            np.array([0, 1, 0, 0, 1]), np.array([0.1, 0.2, 0.3, 0.4, 0.5])
        )
        self.assertEqual(0.2, result.threshold)
        self.assertEqual(1.0, result.recall)

    def test_score_metrics_are_hand_checkable(self):
        report = evaluate_scores(
            np.array([0, 0, 1, 1]),
            np.array([0.1, 0.4, 0.35, 0.8]),
            threshold=0.35,
        )
        self.assertAlmostEqual(0.8333333333333333, report.pr_auc)
        self.assertAlmostEqual(0.75, report.roc_auc)
        self.assertAlmostEqual(2 / 3, report.precision)
        self.assertEqual(1.0, report.recall)
        self.assertAlmostEqual(0.8, report.f1)
        self.assertEqual([[1, 1], [0, 2]], report.confusion_matrix)

    def test_scenario_report_includes_zero_support_and_false_positives(self):
        report = scenario_report(
            y_true=np.array([0, 0, 1, 1, 1]),
            predictions=np.array([0, 1, 1, 0, 1]),
            fraud_types=pd.Series(
                [None, None, "velocity", "amount_anomaly", "velocity"]
            ),
        )
        self.assertEqual(2, report["velocity"]["support"])
        self.assertEqual(1.0, report["velocity"]["recall"])
        self.assertEqual(0.0, report["amount_anomaly"]["recall"])
        self.assertIsNone(report["account_takeover"]["recall"])
        self.assertEqual(1, report["normal"]["false_positives"])
        self.assertEqual(0.5, report["normal"]["false_positive_rate"])

class UnserializableArtifact:
    def __getstate__(self):
        raise TypeError("cannot serialize test artifact")


class FraudModelingArtifactTest(unittest.TestCase):
    def artifact_payload(self, model_pipeline) -> ArtifactPayload:
        return ArtifactPayload(
            model_pipeline=model_pipeline,
            validation_report={
                "model": "random_forest_full",
                "threshold": 0.4,
                "metrics": {"pr_auc": 0.8, "f1": 0.7},
            },
            test_report={"model": "random_forest_full", "pr_auc": 0.75},
            feature_summary={"input_feature_count": 41, "output_feature_count": 4},
            run_manifest={"input_manifest_sha256": "abc123", "seed": 42},
        )

    def test_publish_artifacts_creates_complete_reloadable_run(self):
        from ai_platform.ml.modeling.models import build_model

        X, y = feature_frame()
        config = load_config(
            Path("ai_platform/ml/config/f8_baseline.yaml"), repo_root=Path.cwd()
        )
        pipeline = build_model(config.model, seed=config.seed)
        pipeline.fit(X, y)
        expected = pipeline.predict_proba(X)[:, 1]

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "artifacts"
            published = publish_artifacts(self.artifact_payload(pipeline), output)

            self.assertEqual(output, published)
            self.assertEqual(
                {
                    "model.joblib",
                    "validation_report.json",
                    "test_report.json",
                    "feature_summary.json",
                    "run_manifest.json",
                },
                {path.name for path in output.iterdir()},
            )
            loaded = joblib.load(output / "model.joblib")
            np.testing.assert_allclose(expected, loaded.predict_proba(X)[:, 1])

    def test_publish_failure_leaves_no_completed_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "artifacts"
            with self.assertRaisesRegex(TypeError, "cannot serialize"):
                publish_artifacts(
                    self.artifact_payload(UnserializableArtifact()), output
                )
            self.assertFalse(output.exists())


class FraudModelingRunTest(unittest.TestCase):
    def test_run_training_fits_and_publishes_single_random_forest(self):
        from ai_platform.ml.modeling.run import run_training

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset"
            output = root / "artifacts"
            write_large_modeling_dataset(dataset)
            config_path = root / "f8.yaml"
            config_path.write_text(
                f"""
dataset_path: {dataset}
output_path: {output}
seed: 42
model:
  name: random_forest_full
  estimator: random_forest
  strategy: full
  params: {{n_estimators: 5, max_depth: 3, min_samples_leaf: 1, n_jobs: 1}}
""".lstrip(),
                encoding="utf-8",
            )
            config = load_config(config_path, repo_root=root)

            result = run_training(config)

            self.assertEqual("random_forest_full", result.validation_report["model"])
            self.assertEqual("random_forest_full", result.test_report["model"])
            self.assertGreater(result.validation_report["fit_seconds"], 0)
            self.assertGreater(result.validation_report["predict_seconds"], 0)
            self.assertGreater(result.total_seconds, 0)
            self.assertTrue((output / "model.joblib").is_file())
            model = joblib.load(output / "model.joblib")
            self.assertIsInstance(
                model.named_steps["estimator"], RandomForestClassifier
            )
            manifest = json.loads((output / "run_manifest.json").read_text())
            self.assertEqual("random_forest", manifest["configuration"]["model"]["estimator"])
            self.assertEqual(5, manifest["configuration"]["model"]["params"]["n_estimators"])

if __name__ == "__main__":
    unittest.main()
