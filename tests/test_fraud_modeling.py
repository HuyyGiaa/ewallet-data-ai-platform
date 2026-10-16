from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from ai_platform.ml.modeling.config import load_config
from ai_platform.ml.modeling.data import load_dataset, prepare_modeling_splits
from ai_platform.ml.modeling.features import (
    FeatureStrategy,
    build_feature_transformer,
    transformed_feature_summary,
)
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


class FraudModelingConfigTest(unittest.TestCase):
    def test_load_config_resolves_paths_and_six_ordered_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "f8.yaml"
            config_path.write_text(
                """
dataset_path: data/trainable
output_path: runtime/f8
seed: 42
pca_variance: 0.95
selector_c: 0.1
candidates:
  - name: dummy_full
    estimator: dummy
    strategy: full
    params: {strategy: prior}
  - name: logistic_full
    estimator: logistic
    strategy: full
    params: {C: 1.0, max_iter: 500}
  - name: logistic_selected
    estimator: logistic
    strategy: selected
    params: {C: 1.0, max_iter: 500}
  - name: logistic_pca
    estimator: logistic
    strategy: pca
    params: {C: 1.0, max_iter: 500}
  - name: random_forest_full
    estimator: random_forest
    strategy: full
    params: {n_estimators: 50, max_depth: 8, n_jobs: 1}
  - name: hist_gradient_boosting_full
    estimator: hist_gradient_boosting
    strategy: full
    params: {max_iter: 50, max_depth: 6, learning_rate: 0.1}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path, repo_root=root)

            self.assertEqual(root / "data/trainable", config.dataset_path)
            self.assertEqual(root / "runtime/f8", config.output_path)
            self.assertEqual(42, config.seed)
            self.assertEqual(0.95, config.pca_variance)
            self.assertEqual(0.1, config.selector_c)
            self.assertEqual(
                [
                    "dummy_full",
                    "logistic_full",
                    "logistic_selected",
                    "logistic_pca",
                    "random_forest_full",
                    "hist_gradient_boosting_full",
                ],
                [candidate.name for candidate in config.candidates],
            )


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
        strategy = FeatureStrategy(
            name="full", scale_numeric=True, selector_c=0.1, pca_variance=0.95
        )

        transformer = build_feature_transformer(strategy, seed=42)
        transformed_train = transformer.fit_transform(train, pd.Series([0, 1, 0]))
        transformed_validation = transformer.transform(validation)

        self.assertEqual(3, transformed_train.shape[0])
        self.assertEqual(1, transformed_validation.shape[0])
        self.assertGreater(transformed_train.shape[1], len(MODEL_FEATURE_COLUMNS))
        self.assertTrue(np.isfinite(transformed_train).all())
        self.assertTrue(np.isfinite(transformed_validation).all())

    def test_selected_strategy_records_retained_original_transformed_features(self):
        X, y = feature_frame()
        full = build_feature_transformer(
            FeatureStrategy("full", True, 0.1, 0.95), seed=42
        )
        selected = build_feature_transformer(
            FeatureStrategy("selected", True, 0.1, 0.95), seed=42
        )

        full.fit(X, y)
        selected.fit(X, y)
        full_summary = transformed_feature_summary(full)
        selected_summary = transformed_feature_summary(selected)

        self.assertGreater(selected_summary["output_feature_count"], 0)
        self.assertLess(
            selected_summary["output_feature_count"],
            full_summary["output_feature_count"],
        )
        self.assertEqual(
            selected_summary["output_feature_count"],
            len(selected_summary["selected_feature_names"]),
        )

    def test_pca_strategy_retains_requested_train_variance_and_categorical_signals(self):
        X, y = feature_frame()
        transformer = build_feature_transformer(
            FeatureStrategy("pca", True, 0.1, 0.95), seed=42
        )

        transformed = transformer.fit_transform(X, y)
        summary = transformed_feature_summary(transformer)

        self.assertEqual(len(X), transformed.shape[0])
        self.assertGreaterEqual(summary["pca_explained_variance"], 0.95)
        self.assertGreater(summary["pca_component_count"], 0)
        self.assertTrue(
            any("categorical__" in name for name in summary["output_feature_names"])
        )

if __name__ == "__main__":
    unittest.main()
