from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_platform.ml.modeling.config import load_config


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


if __name__ == "__main__":
    unittest.main()
